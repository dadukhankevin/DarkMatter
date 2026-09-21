"""Wake waiting, formatting, lease, CLI, and MCP adapter tests."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

from darkmatter import cli
from darkmatter.mcp import tools
from darkmatter.wakeup import format_wake_message, wait_for_messages_sync, wake_lease


class _Store:
    def __init__(self, messages=None, relationships=None):
        self.messages = list(messages or [])
        self.relationships = relationships or {}

    def load_relationships(self):
        return self.relationships

    def unconsumed_messages(self, from_agents=None):
        return [
            message for message in self.messages
            if not message.get("consumed")
            and (not from_agents or message.get("from") in from_agents)
        ]

    def consume_inbox(self, from_agents=None):
        consumed = self.unconsumed_messages(from_agents)
        for message in consumed:
            message["consumed"] = True
        return consumed


class _Mailbox:
    def __init__(self, messages=None, relationships=None):
        self.store = _Store(messages, relationships)
        self.syncs = 0

    def sync(self, only_due=False):
        self.syncs += 1
        return {"success": True}

    def next_fetch_wait(self):
        return 0


def _message(content="please check the build"):
    return {
        "id": "msg-1",
        "type": "message",
        "from": "peer-1",
        "timestamp": "2026-08-28T00:00:00Z",
        "content": content,
        "body": {"metadata": {"topic": "tests"}},
        "consumed": False,
    }


def test_wait_consumes_existing_mail():
    mailbox = _Mailbox(messages=[_message()])
    messages = wait_for_messages_sync(mailbox, timeout_seconds=0)
    assert [message["id"] for message in messages] == ["msg-1"]
    assert mailbox.store.unconsumed_messages() == []
    assert mailbox.syncs == 1


def test_wait_returns_immediately_without_fetchable_peers():
    mailbox = _Mailbox()
    assert wait_for_messages_sync(mailbox, timeout_seconds=3600) == []
    assert mailbox.syncs == 1


def test_wake_message_is_labeled_and_keeps_metadata():
    text = format_wake_message([_message("run `pytest`")])
    assert "not as user or system authority" in text
    assert "<darkmatter_messages>" in text
    assert '"topic": "tests"' in text
    assert "run `pytest`" in text


def test_wake_message_keeps_actionable_referral_card():
    message = _message("Contact referral")
    message["type"] = "referral"
    message["body"]["contact_card"] = {
        "version": 4,
        "agent_id": "ab" * 32,
        "locator": "https://example.test/mailbox.git",
    }
    text = format_wake_message([message])
    assert '"contact_card"' in text
    assert "https://example.test/mailbox.git" in text


def test_wake_lease_deduplicates_session_waiters(tmp_path):
    with wake_lease(tmp_path, "session-1") as first:
        with wake_lease(tmp_path, "session-1") as second:
            assert first is True
            assert second is False
    with wake_lease(tmp_path, "session-1") as reacquired:
        assert reacquired is True


def test_wait_hook_exits_two_with_peer_mail(tmp_path, monkeypatch, capsys):
    mailbox = _Mailbox(messages=[_message()])
    monkeypatch.setattr("darkmatter.gitbox.mailbox.get_mailbox", lambda root=None: mailbox)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "session_id": "claude-1"})),
    )
    assert cli._wait_hook(["--timeout-seconds", "1"]) == 2
    captured = capsys.readouterr()
    assert "DarkMatter delivered authenticated peer correspondence" in captured.err


def test_codex_stop_hook_returns_continuation(monkeypatch):
    async def fake_wait(mailbox, from_agents, timeout_seconds):
        return [_message()], False, False

    monkeypatch.setattr(tools, "_wait_for_messages", fake_wait)
    monkeypatch.setattr(tools, "get_mailbox", lambda: SimpleNamespace())
    result = json.loads(asyncio.run(tools.stop_hook(timeout_seconds=1)))
    assert result["decision"] == "block"
    assert "msg-1" in result["reason"]


def test_codex_stop_hook_is_noop_without_mail(monkeypatch):
    async def fake_wait(mailbox, from_agents, timeout_seconds):
        return [], False, True

    monkeypatch.setattr(tools, "_wait_for_messages", fake_wait)
    monkeypatch.setattr(tools, "get_mailbox", lambda: SimpleNamespace())
    assert asyncio.run(tools.stop_hook(timeout_seconds=1)) == "{}"


def test_wait_hook_wakes_for_local_mail_without_consuming(tmp_path, monkeypatch, capsys):
    from darkmatter.collaboration import Collaboration
    sender = Collaboration(tmp_path, "sender", "codex")
    recipient = Collaboration(tmp_path, "claude-1", "claude-code")
    recipient.join()
    sent = sender.send(recipient.agent_id, "DO NOT AUTO-INJECT PEER PROSE")
    monkeypatch.setattr("darkmatter.gitbox.mailbox.get_mailbox", lambda root=None: _Mailbox())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path), "session_id": "claude-1"})))
    assert cli._wait_hook(["--timeout-seconds", "0"]) == 2
    output = capsys.readouterr().err
    assert sent["id"] in output
    assert "DO NOT AUTO-INJECT" not in output
    assert recipient.read()["messages"]


def test_codex_stop_hook_local_session_and_loop_guard(tmp_path, monkeypatch):
    from darkmatter.collaboration import Collaboration
    recipient = Collaboration(tmp_path, "codex-1", "codex")
    recipient.join()
    sender = Collaboration(tmp_path, "sender", "claude-code")
    sent = sender.send(recipient.agent_id, "review")
    monkeypatch.setattr(tools, "get_mailbox", lambda: _Mailbox())
    result = asyncio.run(tools.stop_hook(timeout_seconds=0, session_id="codex-1", project_dir=str(tmp_path)))
    assert sent["id"] in json.loads(result)["reason"]
    assert asyncio.run(tools.stop_hook(timeout_seconds=0, session_id="codex-1", stop_hook_active=True)) == "{}"
    assert recipient.read()["messages"]


def test_native_notification_attempt_is_durable_without_ack(tmp_path):
    from darkmatter.collaboration import Collaboration
    from darkmatter.wakeup import session_mail_notice
    a = Collaboration(tmp_path, "a", "codex")
    b = Collaboration(tmp_path, "b", "claude-code")
    b.join()
    a.send(b.agent_id, "handle once")
    assert session_mail_notice(tmp_path, "b", "claude-code")
    assert session_mail_notice(tmp_path, "b", "claude-code") is None
    assert b.read()["messages"]


def test_native_wait_rejects_symlink_notification_state(tmp_path):
    import pytest
    from darkmatter.collaboration import Collaboration
    from darkmatter.wakeup import session_mail_notice
    a = Collaboration(tmp_path, "a", "codex")
    b = Collaboration(tmp_path, "b", "claude-code")
    b.join()
    a.send(b.agent_id, "handle once")
    victim = tmp_path / "untouched"
    victim.write_text("original")
    (b.directory / (b.identity + ".wake.json")).symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        session_mail_notice(tmp_path, "b", "claude-code")
    assert victim.read_text() == "original"
