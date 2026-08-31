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
