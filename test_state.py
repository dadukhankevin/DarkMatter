"""
State persistence regression tests.

The daemon is the single writer of the state file (2.0): saves are plain
atomic serialize-and-replace under an exclusive lock, and everything durable
— including pending connection requests — must survive a round-trip.
"""

import json
import os

import pytest

from darkmatter.identity import generate_keypair
from darkmatter.models import (
    AgentState,
    AgentStatus,
    Connection,
    PendingConnectionRequest,
    QueuedMessage,
)
from darkmatter.state import (
    _reset_for_tests,
    load_state_from_file,
    record_message_seen,
    is_message_replay,
    save_state,
    set_state,
)


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


def test_save_state_round_trips_core_fields():
    state = make_state()
    path = os.environ["DARKMATTER_STATE_FILE"]

    state.connections["peer-1"] = Connection(
        agent_id="peer-1",
        agent_url="https://peer.example",
        agent_bio="peer",
    )
    state.message_queue.append(QueuedMessage(
        message_id="msg-1",
        content="hello",
        hops_remaining=5,
        metadata={"k": "v"},
    ))

    save_state()
    restored = load_state_from_file(path)

    assert restored is not None
    assert set(restored.connections) == {"peer-1"}
    assert [m.message_id for m in restored.message_queue] == ["msg-1"]
    assert restored.agent_id == state.agent_id


def test_pending_requests_survive_restart():
    state = make_state()
    path = os.environ["DARKMATTER_STATE_FILE"]

    state.pending_requests["req-1"] = PendingConnectionRequest(
        request_id="req-1",
        from_agent_id="remote-agent",
        from_agent_url="https://remote.example",
        from_agent_bio="remote",
        challenge_id="chal-1",
        challenge_hex="aa" * 16,
        identity_verified=True,
    )

    save_state()
    restored = load_state_from_file(path)

    assert restored is not None
    assert set(restored.pending_requests) == {"req-1"}
    req = restored.pending_requests["req-1"]
    assert req.from_agent_id == "remote-agent"
    assert req.challenge_id == "chal-1"
    assert req.identity_verified is True


def test_replay_window_persists_and_marks_after_record():
    state = make_state()
    path = os.environ["DARKMATTER_STATE_FILE"]

    assert not is_message_replay("msg-x")  # checking does NOT record
    assert not is_message_replay("msg-x")
    record_message_seen("msg-x")
    assert is_message_replay("msg-x")

    save_state()
    _reset_for_tests()
    set_state(state)
    restored = load_state_from_file(path)
    assert restored is not None
    assert is_message_replay("msg-x")  # restored from disk


def test_save_is_plain_overwrite_no_resurrection():
    """Consumed messages must NOT reappear after a save (no disk merge)."""
    state = make_state()
    path = os.environ["DARKMATTER_STATE_FILE"]

    state.message_queue.append(QueuedMessage(
        message_id="msg-gone", content="x", hops_remaining=1, metadata={},
    ))
    save_state()

    state.message_queue = []  # consumed
    save_state()

    with open(path) as f:
        data = json.load(f)
    assert data["message_queue"] == []

    restored = load_state_from_file(path)
    assert restored.message_queue == []


def test_conversation_log_total_round_trips():
    from darkmatter.context import log_conversation

    state = make_state()
    path = os.environ["DARKMATTER_STATE_FILE"]

    for i in range(3):
        log_conversation(state, f"m{i}", "hi", from_id="x", to_ids=[state.agent_id],
                         entry_type="direct", direction="inbound")
    assert state.conversation_log_total == 3

    save_state()
    restored = load_state_from_file(path)
    assert restored.conversation_log_total == 3
