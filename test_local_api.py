"""
Daemon local-API tests — inbox long-poll, context feed, daemon-side
connection accept/reject, and send_message dispatch.

These exercise the loopback API that MCP stdio sessions proxy through;
the daemon is the single owner of state.
"""

import asyncio
import json

import pytest

from darkmatter.identity import generate_keypair
from darkmatter.models import (
    AgentState,
    AgentStatus,
    Connection,
    PendingConnectionRequest,
    QueuedMessage,
)
from darkmatter.network.local_api import (
    handle_inbox_wait,
    handle_local_context,
    process_respond_pending,
    process_send_message,
)
from darkmatter.network.manager import set_network_manager
from darkmatter.network.transport import SendResult
from darkmatter.state import _reset_for_tests, set_state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKMATTER_STATE_FILE", str(tmp_path / "state.json"))
    _reset_for_tests()
    yield
    _reset_for_tests()


def make_state() -> AgentState:
    priv, pub = generate_keypair()
    state = AgentState(
        agent_id=pub,
        bio="test",
        status=AgentStatus.ACTIVE,
        port=9900,
        private_key_hex=priv,
        public_key_hex=pub,
    )
    set_state(state)
    return state


class FakeRequest:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query_params = query or {}

    async def json(self):
        return self._body


class StubManager:
    """NetworkManager stand-in recording sends and always succeeding."""

    def __init__(self):
        self.sent = []

    async def send(self, agent_id, path, payload):
        self.sent.append((agent_id, path, payload))
        return SendResult(success=True, transport_name="stub", response={"status": "received"})

    def get_public_url(self):
        return "http://127.0.0.1:9900"

    def get_transport(self, name):
        return None

    async def broadcast_peer_update(self):
        pass


def queue_msg(state, mid, content="hello", sender="peer-1"):
    state.message_queue.append(QueuedMessage(
        message_id=mid, content=content, hops_remaining=5, metadata={},
        from_agent_id=sender,
    ))


# =============================================================================
# Inbox long-poll
# =============================================================================

def test_inbox_wait_consume_drains_queue():
    state = make_state()
    queue_msg(state, "m1")
    queue_msg(state, "m2")

    resp = asyncio.run(handle_inbox_wait(FakeRequest({"timeout_seconds": 0.1})))
    data = json.loads(resp.body)

    assert [m["message_id"] for m in data["messages"]] == ["m1", "m2"]
    assert state.message_queue == []


def test_inbox_wait_peek_leaves_queue_and_respects_exclude():
    state = make_state()
    queue_msg(state, "m1")

    # Peek: message returned, queue untouched
    resp = asyncio.run(handle_inbox_wait(FakeRequest(
        {"timeout_seconds": 0.1, "consume": False})))
    data = json.loads(resp.body)
    assert [m["message_id"] for m in data["messages"]] == ["m1"]
    assert len(state.message_queue) == 1

    # Peek again with the seen ID excluded: nothing new, times out
    resp = asyncio.run(handle_inbox_wait(FakeRequest(
        {"timeout_seconds": 0.1, "consume": False, "exclude_ids": ["m1"]})))
    data = json.loads(resp.body)
    assert data["messages"] == []
    assert data.get("timed_out") is True
    assert len(state.message_queue) == 1


def test_inbox_wait_sender_filter():
    state = make_state()
    queue_msg(state, "m1", sender="alice")
    queue_msg(state, "m2", sender="bob")

    resp = asyncio.run(handle_inbox_wait(FakeRequest(
        {"timeout_seconds": 0.1, "from_agents": ["bob"]})))
    data = json.loads(resp.body)

    assert [m["message_id"] for m in data["messages"]] == ["m2"]
    assert [m.message_id for m in state.message_queue] == ["m1"]


def test_inbox_wait_wakes_on_new_message():
    state = make_state()

    async def scenario():
        wait_task = asyncio.create_task(
            handle_inbox_wait(FakeRequest({"timeout_seconds": 5})))
        await asyncio.sleep(0.05)
        queue_msg(state, "m-live")
        for evt in state._inbox_events:
            evt.set()
        state._inbox_events.clear()
        return await asyncio.wait_for(wait_task, timeout=2)

    resp = asyncio.run(scenario())
    data = json.loads(resp.body)
    assert [m["message_id"] for m in data["messages"]] == ["m-live"]
    assert data["waited"] is True


# =============================================================================
# Context feed — monotonic high-water mark
# =============================================================================

def test_context_survives_log_cap(monkeypatch):
    """The per-session HWM must keep delivering new entries after the
    conversation log hits its cap (regression: index-based HWM died here)."""
    import darkmatter.context as ctx_mod
    monkeypatch.setattr(ctx_mod, "CONVERSATION_LOG_MAX", 10)

    state = make_state()
    sid = "session-x"

    # Fill to cap and beyond, polling in between
    for i in range(10):
        ctx_mod.log_conversation(state, f"m{i}", f"msg {i}", from_id="peer",
                                 to_ids=[state.agent_id], entry_type="direct",
                                 direction="inbound")
    first = ctx_mod.get_context(state, session_id=sid)
    assert "msg 9" in first

    # Log past the cap — log length stays 10, total keeps climbing
    for i in range(10, 15):
        ctx_mod.log_conversation(state, f"m{i}", f"msg {i}", from_id="peer",
                                 to_ids=[state.agent_id], entry_type="direct",
                                 direction="inbound")
    assert len(state.conversation_log) == 10
    second = ctx_mod.get_context(state, session_id=sid)
    assert "msg 14" in second
    assert "msg 9" not in second  # already delivered

    # Nothing new → empty
    assert ctx_mod.get_context(state, session_id=sid) == ""


def test_context_endpoint_returns_feed_and_hint():
    from darkmatter.context import log_conversation

    state = make_state()
    log_conversation(state, "m1", "hello there", from_id="peer",
                     to_ids=[state.agent_id], entry_type="direct", direction="inbound")

    resp = asyncio.run(handle_local_context(FakeRequest(query={"session_id": "s1"})))
    data = json.loads(resp.body)
    assert "hello there" in data["context"]
    assert data["hint"]


# =============================================================================
# Daemon-side accept/reject (regression: was broken cross-process pre-2.0)
# =============================================================================

def _pending(state, request_id="req-1"):
    _, peer_pub = generate_keypair()
    state.pending_requests[request_id] = PendingConnectionRequest(
        request_id=request_id,
        from_agent_id=peer_pub,
        from_agent_url="http://127.0.0.1:9901",
        from_agent_bio="peer",
        from_agent_public_key_hex=peer_pub,
    )
    return peer_pub


def test_respond_pending_accept_creates_connection():
    state = make_state()
    set_network_manager(StubManager())
    peer_pub = _pending(state)

    result = asyncio.run(process_respond_pending(state, "req-1", accept=True))

    assert result["success"] is True
    assert result["accepted"] is True
    assert peer_pub in state.connections
    assert "req-1" not in state.pending_requests


def test_respond_pending_reject_removes_request():
    state = make_state()
    peer_pub = _pending(state)

    result = asyncio.run(process_respond_pending(state, "req-1", accept=False))

    assert result["success"] is True
    assert result["accepted"] is False
    assert peer_pub not in state.connections
    assert "req-1" not in state.pending_requests


def test_respond_pending_unknown_id():
    state = make_state()
    result = asyncio.run(process_respond_pending(state, "nope", accept=True))
    assert result["success"] is False


# =============================================================================
# send_message dispatch
# =============================================================================

def _connect_peer(state, name):
    _, pub = generate_keypair()
    state.connections[pub] = Connection(
        agent_id=pub, agent_url=f"http://127.0.0.1:99{name}", agent_bio=name,
        agent_public_key_hex=pub,
    )
    return pub


def test_send_message_direct_target():
    state = make_state()
    mgr = StubManager()
    set_network_manager(mgr)
    peer = _connect_peer(state, "01")

    result = asyncio.run(process_send_message(state, {
        "content": "hi", "target_agent_id": peer,
    }))

    assert result["success"] is True
    assert result["routed_to"] == [peer]
    assert len(mgr.sent) == 1
    agent_id, path, payload = mgr.sent[0]
    assert path == "/__darkmatter__/message"
    assert payload["signature_hex"]  # signed envelope
    assert payload["from_agent_id"] == state.agent_id


def test_send_message_broadcast_hits_status_broadcast_path():
    state = make_state()
    mgr = StubManager()
    set_network_manager(mgr)
    _connect_peer(state, "01")
    _connect_peer(state, "02")

    result = asyncio.run(process_send_message(state, {
        "content": "fyi", "broadcast": True,
    }))

    assert result["success"] is True
    assert result["broadcast"] is True
    assert {p for _, p, _ in mgr.sent} == {"/__darkmatter__/status_broadcast"}
    assert len(mgr.sent) == 2


def test_send_message_unknown_target_errors():
    state = make_state()
    set_network_manager(StubManager())

    result = asyncio.run(process_send_message(state, {
        "content": "hi", "target_agent_id": "nobody",
    }))
    assert result["success"] is False


def test_send_message_forward_consumes_queue():
    state = make_state()
    mgr = StubManager()
    set_network_manager(mgr)
    peer = _connect_peer(state, "01")
    queue_msg(state, "fwd-1", content="original mail", sender="someone")

    result = asyncio.run(process_send_message(state, {
        "content": "forwarding this",
        "target_agent_id": peer,
        "forward_message_ids": ["fwd-1"],
    }))

    assert result["success"] is True
    assert result["forwarded_count"] == 1
    assert state.message_queue == []
    _, _, payload = mgr.sent[0]
    assert "original mail" in payload["content"]
