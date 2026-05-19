"""
Current network-core tests.

These avoid subprocesses and real sockets. Discovery e2e coverage remains in
test_discovery.py; this file covers mesh processing and routing boundaries.
"""

import asyncio
import json
import os
from datetime import datetime, timezone

import pytest
from starlette.requests import Request

from darkmatter.identity import generate_keypair, sign_message, sign_peer_update
from darkmatter.models import (
    AgentState,
    AgentStatus,
    Connection,
    Impression,
    QueuedMessage,
    RoutingRule,
    RouterAction,
)
from darkmatter.network.access import check_access
from darkmatter.network.discovery import handle_well_known
from darkmatter.network.mesh import (
    _compute_chain_trust,
    _process_get_peers,
    _process_incoming_message,
    _process_peer_update,
)
from darkmatter.router import execute_routing
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


def make_request(client_ip: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": (client_ip, 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    })


def test_access_policy_allows_local_and_connected_peer():
    state = make_state()
    state.connections["peer"] = Connection(
        agent_id="peer",
        agent_url="https://203.0.113.10",
        agent_bio="peer",
    )

    assert check_access(make_request("127.0.0.1"), "network_info", state) is None
    assert check_access(make_request("203.0.113.10"), "network_info", state) is None
    denied = check_access(make_request("203.0.113.11"), "network_info", state)
    assert denied is not None
    assert denied.status_code == 403


def test_peer_update_requires_known_key_signature():
    state = make_state()
    peer_priv, peer_pub = generate_keypair()
    state.connections["peer"] = Connection(
        agent_id="peer",
        agent_url="https://old.example",
        agent_bio="peer",
        agent_public_key_hex=peer_pub,
    )
    timestamp = datetime.now(timezone.utc).isoformat()
    signature = sign_peer_update(peer_priv, "peer", "https://new.example", timestamp)

    result, status = asyncio.run(_process_peer_update(state, {
        "agent_id": "peer",
        "new_url": "https://new.example",
        "public_key_hex": peer_pub,
        "signature": signature,
        "timestamp": timestamp,
    }))

    assert status == 200
    assert result["updated"] is True
    assert state.connections["peer"].agent_url == "https://new.example"


def test_peer_update_rejects_wrong_public_key():
    state = make_state()
    _, peer_pub = generate_keypair()
    fake_priv, fake_pub = generate_keypair()
    timestamp = datetime.now(timezone.utc).isoformat()
    signature = sign_peer_update(fake_priv, "peer", "https://new.example", timestamp)
    state.connections["peer"] = Connection(
        agent_id="peer",
        agent_url="https://old.example",
        agent_bio="peer",
        agent_public_key_hex=peer_pub,
    )

    result, status = asyncio.run(_process_peer_update(state, {
        "agent_id": "peer",
        "new_url": "https://new.example",
        "public_key_hex": fake_pub,
        "signature": signature,
        "timestamp": timestamp,
    }))

    assert status == 403
    assert "mismatch" in result["error"].lower()


def test_get_peers_sorts_by_trust():
    state = make_state()
    for agent_id, score in [("low", 0.1), ("high", 0.9), ("mid", 0.5)]:
        state.connections[agent_id] = Connection(
            agent_id=agent_id,
            agent_url=f"https://{agent_id}.example",
            agent_bio=agent_id,
        )
        state.impressions[agent_id] = Impression(score=score)

    result, status = _process_get_peers(state, {"n": 2})

    assert status == 200
    assert [p["agent_id"] for p in result["peers"]] == ["high", "mid"]


def test_rule_router_forward_rule_executes_without_missing_enum():
    state = make_state()
    state.router_mode = "rules_only"
    state.routing_rules = [
        RoutingRule(rule_id="forward-help", action="forward", keyword="help", forward_to=["peer"])
    ]
    msg = QueuedMessage(
        message_id="msg-1",
        content="please help",
        hops_remaining=5,
        metadata={},
        from_agent_id="sender",
    )
    decisions = []

    async def record_decision(_state, _msg, decision):
        decisions.append(decision)

    decision = asyncio.run(execute_routing(state, msg, execute_decision_fn=record_decision))

    assert decision.action == RouterAction.FORWARD
    assert decision.forward_to == ["peer"]
    assert decisions == [decision]


def test_signed_message_replay_is_rejected():
    state = make_state()
    sender_priv, sender_pub = generate_keypair()
    timestamp = datetime.now(timezone.utc).isoformat()
    signature = sign_message(sender_priv, sender_pub, "msg-1", timestamp, "hello")
    payload = {
        "message_id": "msg-1",
        "content": "hello",
        "from_agent_id": sender_pub,
        "from_public_key_hex": sender_pub,
        "signature_hex": signature,
        "timestamp": timestamp,
    }

    first, first_status = asyncio.run(_process_incoming_message(state, payload))
    second, second_status = asyncio.run(_process_incoming_message(state, payload))

    assert first_status == 200
    assert first["status"] == "received"
    assert second_status == 409
    assert "duplicate" in second["error"].lower()


def test_chain_trust_multiplies_positive_scores_and_floors_negative():
    assert _compute_chain_trust([
        {"trust_to_next": 0.5},
        {"trust_to_next": 0.2},
    ]) == 0.1
    assert _compute_chain_trust([
        {"trust_to_next": 0.5},
        {"trust_to_next": -0.2},
    ]) == 0.0


def test_well_known_prunes_dead_active_sessions():
    state = make_state()
    state.active_sessions = [
        {"pid": os.getpid(), "cwd": "/live"},
        {"pid": 99999999, "cwd": "/dead"},
    ]

    response = asyncio.run(handle_well_known(make_request("127.0.0.1")))
    data = json.loads(response.body)

    assert data["agents"][0]["active_sessions"] == [{"pid": os.getpid(), "cwd": "/live"}]
