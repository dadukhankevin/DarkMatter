"""Two-agent gitbox: introduce, accept, send, receipt → readbox, expiry."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from darkmatter.gitbox.mailbox import Mailbox, reset_mailbox


@pytest.fixture(autouse=True)
def _reset():
    reset_mailbox()
    yield
    reset_mailbox()


def _agent(root: Path) -> Mailbox:
    return Mailbox(root)


def test_introduce_accept_send_ack(tmp_path):
    a = _agent(tmp_path / "a")
    b = _agent(tmp_path / "b")

    intro = a.introduce(b.remote)
    assert intro["success"]
    assert intro["peer_id"] == b.agent_id
    assert (a.work / "outbox" / f"{intro['envelope_id']}.json").exists()

    b.introduce(a.remote)
    synced = b.sync()
    types = {i["type"] for i in synced["ingested"]}
    assert "introduce" in types

    acc = b.accept(a.agent_id)
    assert acc["state"] == "active"

    a.sync()
    assert a.store.get_relationship(b.agent_id).state == "active"

    sent = a.send(b.agent_id, "hello from a")
    assert sent["success"]

    b.sync()
    inbox = b.store.unconsumed_messages()
    assert any(m["content"] == "hello from a" for m in inbox)
    consumed = b.store.consume_inbox()
    assert consumed[0]["content"] == "hello from a"

    a.sync()
    outbox = list((a.work / "outbox").glob("*.json"))
    readbox = list((a.work / "readbox").glob("*.json"))
    assert any(p.stem == sent["envelope_id"] for p in readbox)
    assert not any(p.stem == sent["envelope_id"] for p in outbox)


def test_send_requires_active_relationship(tmp_path):
    a = _agent(tmp_path / "a")
    b = _agent(tmp_path / "b")
    a.introduce(b.remote)
    result = a.send(b.agent_id, "nope")
    assert result["success"] is False


def test_message_metadata_cannot_override_content(tmp_path):
    a = _agent(tmp_path / "a")
    b = _agent(tmp_path / "b")
    a.introduce(b.remote)
    b.introduce(a.remote)
    b.sync()
    b.accept(a.agent_id)
    a.sync()

    assert a.send(b.agent_id, "real", extra={"content": "spoof", "kind": "note"})["success"]
    b.sync()
    message = b.store.unconsumed_messages()[0]
    assert message["content"] == "real"
    assert message["body"]["metadata"] == {"content": "spoof", "kind": "note"}


def test_explicit_forward_preserves_provenance_and_does_not_consume(tmp_path):
    a = _agent(tmp_path / "a")
    b = _agent(tmp_path / "b")
    c = _agent(tmp_path / "c")
    for left, right in ((a, b), (b, c)):
        left.introduce(right.remote)
        right.introduce(left.remote)
        right.sync()
        right.accept(left.agent_id)
        left.sync()

    sent = a.send(b.agent_id, "useful original", extra={"topic": "routing"})
    assert sent["success"]
    b.sync()
    original = next(item for item in b.store.unconsumed_messages() if item["id"] == sent["envelope_id"])
    assert original["forwardable"] is True

    forwarded = b.forward(
        sent["envelope_id"],
        c.agent_id,
        note="This belongs with C",
        max_hops=1,
    )
    assert forwarded["success"]
    assert forwarded["hops_remaining"] == 0
    assert any(item["id"] == sent["envelope_id"] for item in b.store.unconsumed_messages())

    c.sync()
    received = next(
        item for item in c.store.unconsumed_messages()
        if item["id"] == forwarded["envelope_id"]
    )
    assert received["type"] == "forward"
    assert received["forwardable"] is False
    assert received["body"]["forward"]["original_envelope"] == original["envelope"]
    assert received["body"]["forward"]["message"]["content"] == "useful original"
    assert received["body"]["forward"]["path"][0]["from"] == b.agent_id
    assert received["body"]["forward"]["path"][0]["to"] == c.agent_id
    assert "This belongs with C" in received["content"]

    blocked = c.forward(received["id"], b.agent_id, note="one hop too far")
    assert blocked["success"] is False
    assert "hop limit" in blocked["error"]


def test_explicit_contact_referral_preserves_card_and_never_auto_connects(tmp_path):
    a = _agent(tmp_path / "referrer")
    b = _agent(tmp_path / "recipient")
    c = _agent(tmp_path / "referred")
    for left, right in ((a, b), (a, c)):
        left.introduce(right.remote)
        right.introduce(left.remote)
        right.sync()
        right.accept(left.agent_id)
        left.sync()

    referred = a.refer_contact(b.agent_id, c.contact_card(), "You should meet C")
    assert referred["success"]
    b.sync()
    item = next(entry for entry in b.store.unconsumed_messages() if entry["type"] == "referral")
    assert item["body"]["contact_card"] == c.contact_card()
    assert "You should meet C" in item["content"]
    assert b.store.get_relationship(c.agent_id) is None


def test_ignore_closes_relationship(tmp_path):
    a = _agent(tmp_path / "a")
    b = _agent(tmp_path / "b")
    a.introduce(b.remote)
    b.introduce(a.remote)
    b.sync()
    b.ignore(a.agent_id)
    a.sync()
    assert a.store.get_relationship(b.agent_id).state == "closed"


def test_origin_is_per_relationship(tmp_path):
    from darkmatter.gitbox.gitutil import clone_or_update, init_repo

    a = _agent(tmp_path / "a")
    b = _agent(tmp_path / "b")
    public = tmp_path / "a-public.git"
    init_repo(public, bare=True)

    intro = a.introduce(b.remote, advertised_locator=str(public.resolve()))
    assert intro["advertised_locator"] == str(public.resolve())
    assert a.store.get_relationship(b.agent_id).advertised_locator == str(public.resolve())
    assert a.store.get_relationship(b.agent_id).peer_locator == b.remote

    mirror = clone_or_update(str(public.resolve()), tmp_path / "mirror")
    assert (mirror / "outbox" / f"{intro['envelope_id']}.json").exists()

    b.introduce(str(public.resolve()))
    b.sync()
    assert b.store.get_relationship(a.agent_id).peer_locator == str(public.resolve())


def test_one_sided_introduction_can_be_accepted_from_contact_card(tmp_path):
    a = _agent(tmp_path / "a")
    b = _agent(tmp_path / "b")

    request = a.introduce_contact(b.contact_card())
    assert request["success"]
    assert b.store.get_relationship(a.agent_id) is None

    accepted = b.accept(contact_card=request["contact_card"])
    assert accepted["success"]
    assert b.store.get_relationship(a.agent_id).state == "active"

    a.sync()
    assert a.store.get_relationship(b.agent_id).state == "active"
    assert a.send(b.agent_id, "connected through cards")["success"]
    b.sync()
    assert b.store.unconsumed_messages()[0]["content"] == "connected through cards"


def test_expiry_prunes_outbox(tmp_path):
    a = _agent(tmp_path / "a")
    b = _agent(tmp_path / "b")
    a.introduce(b.remote)
    b.introduce(a.remote)
    b.sync()
    b.accept(a.agent_id)
    a.sync()

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    sent = a.send(b.agent_id, "old", expires_at=past)
    assert (a.work / "outbox" / f"{sent['envelope_id']}.json").exists()
    removed = a.expire()
    assert removed >= 1
    assert not (a.work / "outbox" / f"{sent['envelope_id']}.json").exists()
