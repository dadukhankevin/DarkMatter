"""
State persistence regression tests.

The HTTP daemon and stdio MCP session share JSON state, so save_state() must
merge rather than clobber queue, connection, and session data from disk.
"""

import json
import os

import pytest

from darkmatter.identity import generate_keypair
from darkmatter.models import AgentState, AgentStatus, Connection, QueuedMessage
from darkmatter.state import (
    _mcp_added_connections,
    _mcp_removed_connections,
    _reset_for_tests,
    load_state_from_file,
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


def test_save_state_merges_disk_queue_and_mcp_connection_changes(monkeypatch):
    state = make_state()
    path = os.environ["DARKMATTER_STATE_FILE"]
    disk_peer = "disk-peer"
    mcp_peer = "mcp-peer"
    removed_peer = "removed-peer"

    disk_data = {
        "agent_id": state.agent_id,
        "bio": state.bio,
        "status": "active",
        "port": state.port,
        "created_at": state.created_at,
        "messages_handled": 0,
        "public_key_hex": state.public_key_hex,
        "connections": {
            disk_peer: {
                "agent_id": disk_peer,
                "agent_url": "https://disk.example",
                "agent_bio": "disk",
            },
            removed_peer: {
                "agent_id": removed_peer,
                "agent_url": "https://removed.example",
                "agent_bio": "removed",
            },
        },
        "impressions": {},
        "message_queue": [{
            "message_id": "disk-msg",
            "content": "from daemon",
            "hops_remaining": 5,
            "metadata": {},
            "received_at": "2026-01-01T00:00:00+00:00",
            "from_agent_id": disk_peer,
            "verified": True,
        }],
        "consumed_message_ids": ["old-consumed"],
    }
    with open(path, "w") as f:
        json.dump(disk_data, f)

    state.connections[mcp_peer] = Connection(
        agent_id=mcp_peer,
        agent_url="https://mcp.example",
        agent_bio="mcp",
    )
    state.message_queue.append(QueuedMessage(
        message_id="memory-msg",
        content="from memory",
        hops_remaining=5,
        metadata={},
    ))
    _mcp_added_connections.add(mcp_peer)
    _mcp_removed_connections.add(removed_peer)

    save_state()
    restored = load_state_from_file(path)

    assert restored is not None
    assert set(restored.connections) == {disk_peer, mcp_peer}
    assert {m.message_id for m in restored.message_queue} == {"disk-msg", "memory-msg"}


def test_http_daemon_save_persists_live_connection(monkeypatch):
    monkeypatch.setenv("DARKMATTER_TRANSPORT", "http")
    state = make_state()
    path = os.environ["DARKMATTER_STATE_FILE"]

    save_state()
    state.connections["live-peer"] = Connection(
        agent_id="live-peer",
        agent_url="https://live.example",
        agent_bio="live",
    )

    save_state()
    restored = load_state_from_file(path)

    assert restored is not None
    assert set(restored.connections) == {"live-peer"}
