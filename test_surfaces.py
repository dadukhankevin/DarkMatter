"""Visibility, LAN git-HTTP, due-time fetch, policy, and hints."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from darkmatter.gitbox.gitutil import clone_or_update, init_repo
from darkmatter.gitbox.mailbox import Mailbox, reset_mailbox


@pytest.fixture(autouse=True)
def _reset():
    reset_mailbox()
    yield
    reset_mailbox()


@pytest.fixture
def make_box(tmp_path):
    boxes = []

    def _make(name: str, **cfg) -> Mailbox:
        mb = Mailbox(tmp_path / name)
        boxes.append(mb)
        if cfg:
            out = mb.configure(**cfg)
            assert out["success"], out
        return mb

    yield _make
    for mb in boxes:
        mb.shutdown()


def _pair(a: Mailbox, b: Mailbox) -> None:
    assert a.introduce(b.remote)["success"]
    assert b.introduce(a.remote)["success"]
    b.sync()
    assert b.accept(a.agent_id)["success"]
    a.sync()
    assert a.store.get_relationship(b.agent_id).state == "active"
    assert b.store.get_relationship(a.agent_id).state == "active"


def test_visibility_local_is_disk_path(make_box):
    a = make_box("a")
    loc = a.locators()
    assert loc["visibility"] == "local"
    assert loc["primary"] == loc["local"]
    assert loc["lan"] == ""
    assert Path(loc["primary"]).exists()


def test_visibility_internet_requires_origin(make_box):
    a = make_box("a")
    out = a.configure(visibility="internet")
    assert out["success"] is False
    assert "origin" in out["error"]


def test_visibility_internet_pushes_origin(make_box, tmp_path):
    origin = tmp_path / "public.git"
    init_repo(origin, bare=True)
    a = make_box("a")
    b = make_box("b")
    _pair(a, b)
    assert a.configure(visibility="internet", origin=str(origin.resolve()))["success"]
    sent = a.send(b.agent_id, "via origin")
    assert sent["success"]
    assert not sent.get("publish_errors")
    mirror = clone_or_update(str(origin.resolve()), tmp_path / "mirror")
    assert (mirror / "outbox" / f"{sent['envelope_id']}.json").exists()
    assert a.locators()["primary"] == str(origin.resolve())


def test_publish_errors_are_returned(make_box, tmp_path):
    a = make_box("a")
    b = make_box("b")
    _pair(a, b)
    bad = str((tmp_path / "missing.git").resolve())
    a.configure(visibility="internet", origin=bad)
    sent = a.send(b.agent_id, "will fail push")
    assert sent["success"]
    assert sent.get("publish_errors")


def test_lan_git_http_clone_and_mail(make_box):
    a = make_box("a", visibility="lan", lan_bind="127.0.0.1", lan_port=0)
    assert a.lan_url.startswith("http://127.0.0.1:")
    b = make_box("b")
    intro = b.introduce(a.lan_url)
    assert intro["success"]
    assert intro["peer_id"] == a.agent_id
    a.introduce(b.remote)
    a.sync()
    a.accept(b.agent_id)
    b.sync()
    sent = a.send(b.agent_id, "over lan")
    assert sent["success"]
    assert not sent.get("publish_errors")
    inbox = b.sync()["inbox"]
    assert any(m["content"] == "over lan" for m in inbox)


def test_due_fetch_skips_until_interval(make_box):
    a = make_box("a")
    b = make_box("b")
    _pair(a, b)
    b.configure(peer_id=a.agent_id, fetch_every=3600)
    b.sync()
    a.send(b.agent_id, "later")
    skipped = b.sync(only_due=True)
    assert skipped["ingested"] == []
    assert b.store.unconsumed_messages() == []
    forced = b.sync()
    assert any(i["type"] == "message" for i in forced["ingested"])


def test_policy_fetch_interval_and_no_hints(make_box, tmp_path):
    a = make_box("a")
    b = make_box("b")
    c = make_box("c")
    (b.root / ".darkmatter" / "policy.py").write_text(
        "def fetch_interval(relationship):\n"
        "    return 2\n"
        "def should_hint(to_relationship, about_relationship):\n"
        "    return False\n"
    )
    _pair(a, b)
    _pair(b, c)
    a.send(b.agent_id, "x")
    synced = b.sync()
    assert synced["hints"] == 0


def test_hint_schedules_third_peer(make_box):
    a = make_box("a")
    b = make_box("b")
    c = make_box("c")
    _pair(a, b)
    _pair(b, c)
    c.store.upsert_relationship(
        a.agent_id,
        peer_locator=a.remote,
        state="active",
        fetch_every=3600,
        last_fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    c.store.upsert_relationship(b.agent_id, last_fetched_at="")
    a.store.upsert_relationship(c.agent_id, peer_locator=c.remote, state="active")
    a.send(c.agent_id, "for-c")
    b_sync = b.sync()
    assert b_sync["hints"] >= 1
    c.sync(only_due=True)
    assert c.store.get_relationship(a.agent_id).last_fetched_at == ""
    hints = [i for i in c.store.load_inbox() if i.get("type") == "hint"]
    assert hints
    assert hints[0]["body"]["agent_id"] == a.agent_id
    c.sync(only_due=True)
    assert c.store.get_relationship(a.agent_id).last_fetched_at

    # Receipt and hint commits are control traffic and must not generate hints.
    for _ in range(3):
        assert sum(box.sync()["hints"] for box in (a, b, c)) == 0
