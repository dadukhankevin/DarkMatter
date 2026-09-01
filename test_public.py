"""Public GitHub publishing, discovery knocks, and verified invitations."""

import json

import pytest

import darkmatter.public as public
from darkmatter.gitbox.mailbox import Mailbox


def _public(mailbox: Mailbox, repo: str) -> None:
    mailbox.store.save_settings(
        visibility="internet",
        origin=f"https://github.com/{repo}.git",
    )


def test_github_locator_accepts_human_forms_and_rejects_non_repositories():
    assert public.github_locator("owner/repo") == "https://github.com/owner/repo.git"
    assert public.github_repo("https://github.com/owner/repo.git") == "owner/repo"
    assert public.github_repo("git@github.com:owner/repo.git") == "owner/repo"
    assert public.github_repo("https://example.com/owner/repo.git") is None
    with pytest.raises(public.PublicSurfaceError):
        public.github_locator("not a repository")


def test_signed_knock_is_machine_readable_but_not_trusted(tmp_path):
    sender = Mailbox(tmp_path / "sender")
    receiver = Mailbox(tmp_path / "receiver")
    card = sender.contact_card("https://github.com/example/sender.git")
    payload = public._knock_payload(card, receiver.agent_id, "a" * 32)
    body = public._knock_body(payload)

    parsed = public.parse_knock(body)
    assert parsed["contact_card"]["agent_id"] == sender.agent_id
    assert parsed["target_agent_id"] == receiver.agent_id

    tampered = json.loads(json.dumps(payload))
    tampered["contact_card"]["locator"] = "https://github.com/attacker/repo.git"
    with pytest.raises(ValueError, match="signature"):
        public.parse_knock(public._knock_body(tampered))


def test_discovery_verifies_topic_repository_contact_cards(tmp_path, monkeypatch):
    seeker = Mailbox(tmp_path / "seeker")
    found = Mailbox(tmp_path / "found")
    card = found.contact_card("https://github.com/example/found.git")

    def fake_gh(*args, timeout=60.0):
        if args[:2] == ("search", "repos"):
            return json.dumps([{
                "fullName": "example/found",
                "url": "https://github.com/example/found",
                "description": "A public agent",
                "updatedAt": "2026-08-31T00:00:00Z",
                "visibility": "PUBLIC",
            }])
        if args[:2] == ("api", "repos/example/found/contents/agent.json"):
            return json.dumps({"agent_id": found.agent_id, "contact_card": card})
        raise AssertionError(args)

    monkeypatch.setattr(public, "_gh", fake_gh)
    result = public.discover_public_agents(seeker, "helpful")
    assert result["success"]
    assert result["count"] == 1
    assert result["agents"][0]["agent_id"] == found.agent_id
    assert result["agents"][0]["repository"] == "example/found"


def test_publish_creates_public_repo_configures_origin_and_pushes(
    tmp_path,
    monkeypatch,
):
    mailbox = Mailbox(tmp_path / "project")
    commands = []
    created = False

    def fake_gh(*args, timeout=60.0):
        nonlocal created
        commands.append(args)
        if args[:2] == ("api", "user"):
            return "example"
        if args[:2] == ("repo", "view"):
            if not created:
                raise public.PublicSurfaceError("not found")
            return json.dumps({
                "nameWithOwner": "example/project-agent",
                "url": "https://github.com/example/project-agent",
                "visibility": "PUBLIC",
                "hasIssuesEnabled": True,
            })
        if args[:2] == ("repo", "create"):
            created = True
            return "https://github.com/example/project-agent"
        if args[:2] == ("repo", "edit"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(public, "_gh", fake_gh)
    monkeypatch.setattr(
        mailbox,
        "retry_publication",
        lambda: {"success": True, "publish_errors": []},
    )
    result = public.publish_github(mailbox, "example/project-agent")
    assert result["success"]
    assert result["created"] is True
    assert mailbox.locators()["visibility"] == "internet"
    assert mailbox.locators()["internet"] == "https://github.com/example/project-agent.git"
    assert any(command[:2] == ("repo", "create") for command in commands)
    assert any(command[:2] == ("repo", "edit") for command in commands)


def test_public_connect_publishes_intro_then_knocks(tmp_path, monkeypatch):
    sender = Mailbox(tmp_path / "sender")
    receiver = Mailbox(tmp_path / "receiver")
    _public(sender, "example/sender")
    target = "https://github.com/example/receiver.git"
    seen = {}

    monkeypatch.setattr(
        sender,
        "peek_remote",
        lambda locator, expected=None: {"agent_id": receiver.agent_id},
    )

    def fake_introduce(locator, advertised, expected=None):
        seen.update({"locator": locator, "advertised": advertised, "expected": expected})
        return {
            "success": True,
            "peer_id": receiver.agent_id,
            "envelope_id": "b" * 32,
            "state": "pending",
        }

    monkeypatch.setattr(sender, "introduce", fake_introduce)
    monkeypatch.setattr(
        public,
        "_notify_connection",
        lambda mailbox, repo, result: {
            "success": True,
            "issue_url": f"https://github.com/{repo}/issues/1",
        },
    )

    result = public.connect_public(sender, target)
    assert result["success"]
    assert result["knock"]["success"]
    assert seen["locator"] == target
    assert seen["advertised"] == "https://github.com/example/sender.git"


def test_poll_fetches_only_verified_public_introductions(tmp_path, monkeypatch):
    sender = Mailbox(tmp_path / "sender")
    receiver = Mailbox(tmp_path / "receiver")
    _public(receiver, "example/receiver")
    card = sender.contact_card("https://github.com/example/sender.git")
    payload = public._knock_payload(card, receiver.agent_id, "c" * 32)
    issue = {
        "number": 9,
        "title": "[DarkMatter] Connection request",
        "body": public._knock_body(payload),
        "url": "https://github.com/example/receiver/issues/9",
        "state": "OPEN",
    }
    monkeypatch.setattr(public, "_issues", lambda repository, state="open": [issue])
    seen = {}

    def fake_receive(contact_card, envelope_id=None):
        seen.update({"card": contact_card, "envelope_id": envelope_id})
        return {"success": True, "peer_id": sender.agent_id, "state": "pending"}

    monkeypatch.setattr(receiver, "receive_introduction", fake_receive)
    result = public.poll_public_invitations(receiver)
    assert result["success"]
    assert result["count"] == 1
    assert result["invitations"][0]["agent_id"] == sender.agent_id
    assert seen["envelope_id"] == "c" * 32


def test_receive_introduction_fetches_before_accepting(tmp_path):
    sender = Mailbox(tmp_path / "sender")
    receiver = Mailbox(tmp_path / "receiver")
    introduced = sender.introduce(receiver.remote)
    result = receiver.receive_introduction(
        introduced["contact_card"],
        introduced["envelope_id"],
    )
    assert result["success"]
    assert receiver.store.get_relationship(sender.agent_id).state == "pending"
    assert receiver.accept(sender.agent_id)["success"]
    assert receiver.store.get_relationship(sender.agent_id).state == "active"
    repeated = receiver.receive_introduction(
        introduced["contact_card"],
        introduced["envelope_id"],
    )
    assert repeated["success"]
    assert repeated["state"] == "active"
    assert receiver.store.get_relationship(sender.agent_id).state == "active"


def _knock_issue(sender, receiver, number, envelope_id):
    card = sender.contact_card("https://github.com/example/sender.git")
    payload = public._knock_payload(card, receiver.agent_id, envelope_id)
    return {
        "number": number,
        "title": "[DarkMatter] Connection request",
        "body": public._knock_body(payload),
        "url": f"https://github.com/example/receiver/issues/{number}",
        "state": "OPEN",
    }


def test_poll_limits_fetches_and_remembers_rejected_knocks(tmp_path, monkeypatch):
    sender = Mailbox(tmp_path / "sender")
    receiver = Mailbox(tmp_path / "receiver")
    _public(receiver, "example/receiver")
    issues = [_knock_issue(sender, receiver, n, f"{n:032x}") for n in range(1, 5)]
    monkeypatch.setattr(public, "_issues", lambda repository, state="open": issues)
    calls = []

    def fake_receive(contact_card, envelope_id=None):
        calls.append(envelope_id)
        if envelope_id == f"{1:032x}":
            return {"success": False, "error": "no introduction"}
        return {"success": True, "peer_id": sender.agent_id, "state": "pending"}

    monkeypatch.setattr(receiver, "receive_introduction", fake_receive)
    result = public.poll_public_invitations(receiver, fetch_budget=2)
    assert result["success"]
    assert len(calls) == 2
    assert result["deferred"] == 2
    assert result["count"] == 1
    assert result["invalid"][0]["issue_number"] == 1

    calls.clear()
    result = public.poll_public_invitations(receiver, fetch_budget=2)
    assert f"{1:032x}" not in calls
    assert result["deferred"] == 1


def test_maintain_once_survives_invitation_polling_failure(tmp_path, monkeypatch):
    mailbox = Mailbox(tmp_path / "agent")
    _public(mailbox, "example/agent")

    def broken(_mailbox, fetch_budget=None):
        raise RuntimeError("gh is missing")

    monkeypatch.setattr(public, "poll_public_invitations", broken)
    result = mailbox.maintain_once()
    assert result["invitations"]["success"] is False
    assert result["warnings"]
    assert result["success"] == bool(
        result["sync"]["success"] and result["recovery"]["success"] and result["publication"]["success"]
    )
