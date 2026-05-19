"""
Current identity and handshake tests for DarkMatter.

These are pytest-compatible unit/integration tests for the passport identity
model and current connection handshake. They avoid real sockets.
"""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport

from darkmatter.app import create_app
from darkmatter.identity import generate_keypair, sign_message
from darkmatter.models import AgentState, AgentStatus, PendingConnectionRequest
from darkmatter.network.mesh import (
    _process_incoming_message,
    process_accept_pending,
    process_connection_request,
)
from darkmatter.security import create_challenge, prove_identity
from darkmatter.state import _reset_for_tests, load_state_from_file, set_state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKMATTER_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("DARKMATTER_DISCOVERY", "false")
    monkeypatch.setenv("DARKMATTER_BOOTSTRAP_PEERS", "")
    _reset_for_tests()
    yield
    _reset_for_tests()


def make_state(port: int = 9900) -> AgentState:
    priv, pub = generate_keypair()
    state = AgentState(
        agent_id=pub,
        bio="A DarkMatter mesh agent.",
        status=AgentStatus.ACTIVE,
        port=port,
        private_key_hex=priv,
        public_key_hex=pub,
        display_name=f"agent-{port}",
    )
    set_state(state)
    return state


def test_agent_id_is_passport_public_key():
    state = make_state()

    assert state.agent_id == state.public_key_hex
    assert len(state.agent_id) == 64
    assert len(state.private_key_hex) == 64
    assert all(c in "0123456789abcdef" for c in state.agent_id)


def test_load_legacy_state_defaults_router_mode(tmp_path):
    priv, pub = generate_keypair()
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({
        "agent_id": pub,
        "bio": "legacy",
        "status": "active",
        "port": 9900,
        "created_at": "2026-01-01T00:00:00+00:00",
        "messages_handled": 5,
        "private_key_hex": priv,
        "public_key_hex": pub,
        "connections": {},
        "impressions": {},
        "router_mode": None,
    }))

    state = load_state_from_file(str(path))

    assert state is not None
    assert state.agent_id == pub
    assert state.messages_handled == 5
    assert state.router_mode == "spawn"


def test_local_connection_request_auto_accepts():
    target = make_state(port=9900)
    _, peer_pub = generate_keypair()

    result, status = asyncio.run(process_connection_request(target, {
        "from_agent_id": peer_pub,
        "from_agent_url": "http://127.0.0.1:9901",
        "from_agent_bio": "local peer",
        "from_agent_public_key_hex": peer_pub,
        "from_agent_display_name": "peer",
    }, "http://127.0.0.1:9900", client_ip="127.0.0.1"))

    assert status == 200
    assert result["auto_accepted"] is True
    assert peer_pub in target.connections
    assert target.connections[peer_pub].identity_verified is True


def test_connection_proof_marks_pending_request_verified():
    state = make_state()
    peer_priv, peer_pub = generate_keypair()
    challenge_id, challenge_hex = create_challenge(peer_pub)
    request_id = "req-test"
    state.pending_requests[request_id] = PendingConnectionRequest(
        request_id=request_id,
        from_agent_id=peer_pub,
        from_agent_url="https://example.com",
        from_agent_bio="manual peer",
        from_agent_public_key_hex=peer_pub,
        challenge_id=challenge_id,
        challenge_hex=challenge_hex,
    )
    app = create_app()
    proof_hex = prove_identity(challenge_hex, peer_priv)

    async def run_request():
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.post("/__darkmatter__/connection_proof", json={
                "challenge_id": challenge_id,
                "proof_hex": proof_hex,
                "agent_id": peer_pub,
                "public_key_hex": peer_pub,
            })

    response = asyncio.run(run_request())

    assert response.status_code == 200
    assert state.pending_requests[request_id].identity_verified is True


def test_accept_pending_uses_actual_proof_status():
    state = make_state()
    peer_priv, peer_pub = generate_keypair()
    request_id = "req-test"
    state.pending_requests[request_id] = PendingConnectionRequest(
        request_id=request_id,
        from_agent_id=peer_pub,
        from_agent_url="https://example.com",
        from_agent_bio="manual peer",
        from_agent_public_key_hex=peer_pub,
        challenge_id="ch-test",
        challenge_hex="00" * 32,
        identity_verified=False,
    )

    result, status, _ = process_accept_pending(state, request_id, "https://me.example")

    assert status == 200
    assert result["accepted"] is True
    assert state.connections[peer_pub].identity_verified is False


def test_signed_message_from_unknown_sender_is_queued():
    state = make_state()
    sender_priv, sender_pub = generate_keypair()
    message_id = "msg-test"
    timestamp = datetime.now(timezone.utc).isoformat()
    content = "hello"
    signature = sign_message(sender_priv, sender_pub, message_id, timestamp, content)

    result, status = asyncio.run(_process_incoming_message(state, {
        "message_id": message_id,
        "content": content,
        "from_agent_id": sender_pub,
        "from_public_key_hex": sender_pub,
        "signature_hex": signature,
        "timestamp": timestamp,
    }))

    assert status == 200
    assert result["status"] == "received"
    assert len(state.message_queue) == 1
    assert state.message_queue[0].verified is True
