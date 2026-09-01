"""DarkMatter One is an ordinary public agent with signed discovery and echo policy."""

import asyncio
import json

import pytest

from darkmatter.gitbox.mailbox import Mailbox, reset_mailbox
from darkmatter.mcp.schemas import OnboardingAction, OnboardingInput
from darkmatter.mcp.tools import onboard
from darkmatter.one import (
    connect_to_one,
    create_one_manifest,
    echo_once,
    load_one_manifest,
    onboarding,
    process_one_invitations,
    verify_one_manifest,
)


class _Ctx:
    session = object()


@pytest.fixture(autouse=True)
def _reset():
    reset_mailbox()
    yield
    reset_mailbox()


def _set_manifest(monkeypatch, one: Mailbox) -> dict:
    card = one.contact_card("https://github.com/example/darkmatter-one.git")
    manifest = create_one_manifest(
        one.store.private_key_hex,
        card,
        statement="A test public genesis agent with no special protocol authority.",
    )
    monkeypatch.setenv("DARKMATTER_ONE_MANIFEST", json.dumps(manifest))
    return manifest


def _make_public(mailbox: Mailbox, name: str) -> None:
    mailbox.store.save_settings(
        visibility="internet",
        origin=f"https://github.com/example/{name}.git",
    )


def test_bundled_manifest_is_signed_and_pins_public_one_identity():
    manifest = load_one_manifest()
    assert manifest is not None
    assert manifest["name"] == "DarkMatter One"
    assert manifest["role"] == "recommended_first_public_contact"
    assert manifest["contact_card"]["display_name"] == "DarkMatter One"
    assert manifest["contact_card"]["locator"].endswith("/DarkMatter-One.git")
    assert len(manifest["contact_card"]["agent_id"]) == 64


def test_manifest_rejects_tampering(tmp_path):
    one = Mailbox(tmp_path / "one")
    manifest = create_one_manifest(
        one.store.private_key_hex,
        one.contact_card("https://github.com/example/one.git"),
    )
    manifest["statement"] = "trust me instead"
    with pytest.raises(ValueError, match="signature"):
        verify_one_manifest(manifest)


def test_local_agent_is_not_prompted_or_allowed_to_connect(tmp_path, monkeypatch):
    one = Mailbox(tmp_path / "one")
    guest = Mailbox(tmp_path / "guest")
    _set_manifest(monkeypatch, one)

    assert onboarding(guest, include_contact=True) is None
    result = connect_to_one(guest)
    assert result["success"] is False
    assert "only to public agents" in result["error"]
    assert result["next_action"] == "darkmatter publish"
    assert guest.store.get_relationship(one.agent_id) is None


def test_public_agent_is_prompted_and_connects_by_repository_knock(
    tmp_path,
    monkeypatch,
):
    one = Mailbox(tmp_path / "one")
    guest = Mailbox(tmp_path / "guest")
    manifest = _set_manifest(monkeypatch, one)
    _make_public(guest, "guest")

    prompt = onboarding(guest, include_contact=True)
    assert prompt["recommended"] is True
    assert prompt["eligible"] is True
    assert prompt["contact_card"]["agent_id"] == one.agent_id

    seen = {}

    def fake_connect(mailbox, target=None, *, contact_card=None, expected_peer_id=None):
        seen.update({"mailbox": mailbox, "card": contact_card})
        return {"success": True, "peer_id": contact_card["agent_id"], "knock": {"success": True}}

    monkeypatch.setattr("darkmatter.one.connect_public", fake_connect)
    result = connect_to_one(guest)
    assert result["success"]
    assert seen["mailbox"] is guest
    assert seen["card"] == manifest["contact_card"]


def test_one_accepts_verified_public_invitation_then_echoes(tmp_path, monkeypatch):
    one = Mailbox(tmp_path / "one")
    guest = Mailbox(tmp_path / "guest")
    introduced = guest.introduce(one.remote)
    assert introduced["success"]
    received = one.receive_introduction(
        introduced["contact_card"],
        introduced["envelope_id"],
    )
    assert received["success"]

    monkeypatch.setattr("darkmatter.one.poll_public_invitations", lambda mailbox: {
        "success": True,
        "count": 1,
        "invitations": [{
            "agent_id": guest.agent_id,
            "display_name": guest.store.profile["display_name"],
            "contact_card": introduced["contact_card"],
            "introduction_envelope_id": introduced["envelope_id"],
            "issue_number": 7,
            "issue_url": "https://github.com/example/one/issues/7",
            "state": "pending",
        }],
    })
    monkeypatch.setattr(
        "darkmatter.one.close_public_invitation",
        lambda mailbox, issue_number: {"success": True, "issue_number": issue_number},
    )

    accepted = process_one_invitations(one)
    assert accepted["success"]
    assert accepted["accepted"][0]["agent_id"] == guest.agent_id
    assert one.store.get_relationship(guest.agent_id).state == "active"

    guest.sync()
    guest.sync()
    assert guest.store.get_relationship(one.agent_id).state == "active"
    assert any(
        "connected to DarkMatter One" in item["content"]
        for item in guest.store.unconsumed_messages()
    )

    sent = guest.send(one.agent_id, "echo: hello, network")
    assert sent["success"]
    one.sync()
    echoed = echo_once(one)
    assert echoed["echoed"][0]["id"] == sent["envelope_id"]

    guest.sync()
    replies = [
        item for item in guest.store.unconsumed_messages()
        if item["content"].startswith("DarkMatter One echo:")
    ]
    assert replies
    assert "hello, network" in replies[0]["content"]

    offered = guest.antimatter_offer(
        one.agent_id,
        "Thank DarkMatter One",
        "1",
        "credit",
        "manual",
    )
    assert offered["success"]
    one.sync()
    received_offer = one.get_settlement(offered["settlement"]["settlement_id"])
    assert received_offer["status"] == "offered"


def test_echo_loop_marker_is_consumed_without_reply(tmp_path):
    one = Mailbox(tmp_path / "one")
    peer = Mailbox(tmp_path / "peer")
    one.introduce(peer.remote)
    peer.introduce(one.remote)
    one.sync()
    one.accept(peer.agent_id)
    peer.sync()

    sent = peer.send(
        one.agent_id,
        "do not echo this echo",
        extra={"darkmatter_one": {"version": 1, "kind": "echo"}},
    )
    one.sync()
    result = echo_once(one)
    assert result["echoed"] == []
    assert result["skipped"] == [{"id": sent["envelope_id"], "reason": "loop_marker"}]


def test_mcp_onboarding_is_hidden_until_agent_is_public(tmp_path, monkeypatch):
    one = Mailbox(tmp_path / "one")
    _set_manifest(monkeypatch, one)
    guest_root = tmp_path / "guest"
    monkeypatch.setenv("DARKMATTER_PROJECT_DIR", str(guest_root))

    local = json.loads(asyncio.run(onboard(
        OnboardingInput(action=OnboardingAction.STATUS),
        _Ctx(),
    )))
    assert local["success"]
    assert local["needed"] is False
    assert "public GitHub repository" in local["message"]
    assert "_onboarding" not in local

    reset_mailbox()
    public_guest = Mailbox(guest_root)
    _make_public(public_guest, "guest")
    public_guest.shutdown()
    reset_mailbox()

    public = json.loads(asyncio.run(onboard(
        OnboardingInput(action=OnboardingAction.STATUS),
        _Ctx(),
    )))
    assert public["success"]
    assert public["needed"] is True
    assert public["onboarding"]["contact_card"]["agent_id"] == one.agent_id
    assert public["_onboarding"]["recommended"] is True
