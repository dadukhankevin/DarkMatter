#!/usr/bin/env python3
"""
Integration tests for DarkMatter Ed25519 cryptographic identity.

Standalone async script — no test framework, no real ports.
Uses httpx.AsyncClient with ASGITransport for in-process requests.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

import httpx
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def report(name: str, passed: bool, detail: str = "") -> None:
    mark = f"{GREEN}✓{RESET}" if passed else f"{RED}✗{RESET}"
    print(f"  {mark} {name}")
    if detail and not passed:
        print(f"      {detail}")
    results.append((name, passed, detail))


def make_state_file() -> str:
    """Return a path to a fresh temp state file (caller cleans up)."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="dm_test_")
    os.close(fd)
    os.unlink(path)  # start with no file
    return path


def create_agent(state_path: str, *, port: int = 9900) -> tuple:
    """Create a fresh DarkMatter app + state, isolated from other agents.

    Each agent gets a unique keypair so they have distinct identities,
    bypassing the passport file (which would give all agents the same
    identity since they share a working directory).

    Returns (app, state) tuple.
    """
    import server

    server._agent_state = None

    os.environ["DARKMATTER_PORT"] = str(port)
    os.environ["DARKMATTER_DISCOVERY"] = "false"
    os.environ.pop("DARKMATTER_AGENT_ID", None)
    os.environ.pop("DARKMATTER_MCP_TOKEN", None)
    os.environ.pop("DARKMATTER_GENESIS", None)

    # Generate a unique keypair for this agent (bypass passport file)
    priv, pub = server._generate_keypair()
    server._agent_state = server.AgentState(
        agent_id=pub,
        bio="A DarkMatter mesh agent.",
        status=server.AgentStatus.ACTIVE,
        port=port,
        private_key_hex=priv,
        public_key_hex=pub,
    )
    app = server.create_app()
    state = server._agent_state
    return app, state


def use_agent(state):
    """Set the global _agent_state to this agent's state before making requests."""
    import server
    server._agent_state = state


async def connect_agents(app_a, state_a, app_b, state_b) -> None:
    """Connect agent B to agent A via accept_pending + connection_accepted.

    After this call, both sides have the connection recorded.
    """
    # B sends connection_request to A
    use_agent(state_a)
    async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
        resp = await client.post("/__darkmatter__/connection_request", json={
            "from_agent_id": state_b.agent_id,
            "from_agent_url": f"http://localhost:{state_b.port}/mcp",
            "from_agent_bio": f"Agent {state_b.agent_id[:8]}",
            "from_agent_public_key_hex": state_b.public_key_hex,
        })
    data = resp.json()
    request_id = data.get("request_id")

    # Accept on A's side
    async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
        await client.post("/__darkmatter__/accept_pending", json={"request_id": request_id})

    # Record connection on B's side (simulate connection_accepted callback)
    use_agent(state_b)
    state_b.pending_outbound[f"http://localhost:{state_a.port}/mcp"] = state_a.agent_id
    async with httpx.AsyncClient(transport=ASGITransport(app=app_b), base_url="http://test") as client:
        await client.post("/__darkmatter__/connection_accepted", json={
            "agent_id": state_a.agent_id,
            "agent_url": f"http://localhost:{state_a.port}/mcp",
            "agent_bio": state_a.bio,
            "agent_public_key_hex": state_a.public_key_hex,
        })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_fresh_agent_gets_keypair() -> None:
    """Fresh agent gets Ed25519 keypair (agent_id = public_key_hex)."""
    import server

    path = make_state_file()
    try:
        app, state = create_agent(path)

        # agent_id is 64-char hex (Ed25519 public key)
        id_ok = (
            isinstance(state.agent_id, str)
            and len(state.agent_id) == 64
            and all(c in "0123456789abcdef" for c in state.agent_id)
        )
        report("agent_id is 64 hex chars (public key)", id_ok, f"got: {state.agent_id}")

        # agent_id == public_key_hex (passport identity)
        report("agent_id equals public_key_hex",
               state.agent_id == state.public_key_hex)

        # Keys are 64-char hex (32 bytes)
        priv_ok = (
            isinstance(state.private_key_hex, str)
            and len(state.private_key_hex) == 64
            and all(c in "0123456789abcdef" for c in state.private_key_hex)
        )
        report("private_key_hex is 64 hex chars", priv_ok,
               f"len={len(state.private_key_hex) if state.private_key_hex else 'None'}")

        pub_ok = (
            isinstance(state.public_key_hex, str)
            and len(state.public_key_hex) == 64
            and all(c in "0123456789abcdef" for c in state.public_key_hex)
        )
        report("public_key_hex is 64 hex chars", pub_ok,
               f"len={len(state.public_key_hex) if state.public_key_hex else 'None'}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_state_migration() -> None:
    """Legacy state (no crypto fields) loads; keys come from passport, not state file."""
    import server

    path = make_state_file()
    try:
        # Write a legacy state file without crypto fields
        legacy = {
            "agent_id": "legacy-agent-123",
            "bio": "Old agent",
            "status": "active",
            "port": 9900,
            "created_at": "2024-01-01T00:00:00+00:00",
            "messages_handled": 5,
            "connections": {},
            "sent_messages": {},
            "impressions": {},
        }
        with open(path, "w") as f:
            json.dump(legacy, f)

        state = server._load_state_from_file(path)

        report("state loaded", state is not None)
        if state:
            report("agent_id preserved", state.agent_id == "legacy-agent-123")
            # Keys are empty from state file — passport system provides them at init time
            report("private_key_hex empty (set by passport later)",
                   not state.private_key_hex)
            report("messages_handled preserved", state.messages_handled == 5)
        else:
            report("agent_id preserved", False, "state failed to load")
            report("private_key_hex is None (set by passport later)", False, "state failed to load")
            report("messages_handled preserved", False, "state failed to load")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_connection_handshake_exchanges_keys() -> None:
    """Connection request with manual accept exchanges public keys."""
    import server

    path_a = make_state_file()
    path_b = make_state_file()
    try:
        # Agent A
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = server._agent_state

        # Agent B
        app_b, state_b = create_agent(path_b, port=9901)

        # B sends connection_request to A
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/connection_request", json={
                "from_agent_id": state_b.agent_id,
                "from_agent_url": "http://localhost:9901/mcp",
                "from_agent_bio": "Test agent B",
                "from_agent_public_key_hex": state_b.public_key_hex,
            })

        data = resp.json()
        report("connection request queued (not auto-accepted)", data.get("auto_accepted") is False)
        request_id = data.get("request_id")
        report("response includes request_id", request_id is not None)

        # Accept via HTTP endpoint
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp2 = await client.post("/__darkmatter__/accept_pending", json={
                "request_id": request_id,
            })

        accept_data = resp2.json()
        report("accept_pending succeeded", accept_data.get("success") is True)

        # Agent A should have B's public key stored in its connection
        conn_b = stashed_a.connections.get(state_b.agent_id)
        report("Agent A stores B's public key",
               conn_b is not None and conn_b.agent_public_key_hex == state_b.public_key_hex)
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_signed_message_verified() -> None:
    """Properly signed message from connected peer → verified: true."""
    import server

    path_a = make_state_file()
    path_b = make_state_file()
    try:
        # Set up two connected agents
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = server._agent_state

        app_b, state_b = create_agent(path_b, port=9901)

        # Connect B → A
        await connect_agents(app_a, stashed_a, app_b, state_b)

        # B signs and sends a message to A
        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = datetime.now(timezone.utc).isoformat()
        content = "Hello from B"

        signature = server._sign_message(
            state_b.private_key_hex, state_b.agent_id, message_id, timestamp, content
        )

        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/message", json={
                "message_id": message_id,
                "content": content,
                "webhook": "http://localhost:9901/__darkmatter__/webhook/" + message_id,
                "from_agent_id": state_b.agent_id,
                "from_public_key_hex": state_b.public_key_hex,
                "signature_hex": signature,
                "timestamp": timestamp,
            })

        report("signed message accepted (200)", resp.status_code == 200)

        # Check queued message is verified
        queued = [m for m in stashed_a.message_queue if m.message_id == message_id]
        report("message queued with verified=True",
               len(queued) == 1 and queued[0].verified is True)
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_key_mismatch_rejected() -> None:
    """Message with wrong public key → 403 'Public key mismatch'."""
    import server

    path_a = make_state_file()
    path_b = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = server._agent_state

        app_b, state_b = create_agent(path_b, port=9901)

        # Connect B → A
        await connect_agents(app_a, stashed_a, app_b, state_b)

        # Generate a different keypair (impersonator)
        fake_priv, fake_pub = server._generate_keypair()

        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = datetime.now(timezone.utc).isoformat()
        content = "Spoofed message"

        signature = server._sign_message(fake_priv, state_b.agent_id, message_id, timestamp, content)

        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/message", json={
                "message_id": message_id,
                "content": content,
                "webhook": "http://localhost:9901/__darkmatter__/webhook/" + message_id,
                "from_agent_id": state_b.agent_id,
                "from_public_key_hex": fake_pub,  # wrong key!
                "signature_hex": signature,
                "timestamp": timestamp,
            })

        report("key mismatch → 403", resp.status_code == 403)
        report("error mentions 'Public key mismatch'",
               "Public key mismatch" in resp.json().get("error", ""))
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_invalid_signature_rejected() -> None:
    """Correct public key but garbage signature → 403."""
    import server

    path_a = make_state_file()
    path_b = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = server._agent_state

        app_b, state_b = create_agent(path_b, port=9901)

        # Connect B → A
        await connect_agents(app_a, stashed_a, app_b, state_b)

        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = datetime.now(timezone.utc).isoformat()
        content = "Tampered message"

        # Garbage signature (valid hex, wrong bytes)
        garbage_sig = "ab" * 64

        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/message", json={
                "message_id": message_id,
                "content": content,
                "webhook": "http://localhost:9901/__darkmatter__/webhook/" + message_id,
                "from_agent_id": state_b.agent_id,
                "from_public_key_hex": state_b.public_key_hex,
                "signature_hex": garbage_sig,
                "timestamp": timestamp,
            })

        report("invalid signature → 403", resp.status_code == 403)
        report("error mentions 'Invalid signature'",
               "Invalid signature" in resp.json().get("error", ""))
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_signed_unknown_sender_accepted() -> None:
    """Signed message from non-connected agent → accepted (signatures prove identity)."""
    import server

    path_a = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = server._agent_state

        # Send signed message from an agent that's NOT connected
        unknown_priv, unknown_pub = server._generate_keypair()
        unknown_id = "unknown-agent-999"
        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = datetime.now(timezone.utc).isoformat()
        content = "Who am I?"

        signature = server._sign_message(unknown_priv, unknown_id, message_id, timestamp, content)

        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/message", json={
                "message_id": message_id,
                "content": content,
                "webhook": "http://localhost:9900/__darkmatter__/webhook/" + message_id,
                "from_agent_id": unknown_id,
                "from_public_key_hex": unknown_pub,
                "signature_hex": signature,
                "timestamp": timestamp,
            })

        report("signed unknown sender accepted (200)", resp.status_code == 200)

        # Message should be queued and verified
        queued = [m for m in stashed_a.message_queue if m.message_id == message_id]
        report("message queued with verified=True",
               len(queued) == 1 and queued[0].verified is True)
    finally:
        if os.path.exists(path_a):
            os.unlink(path_a)


async def test_unsigned_unknown_sender_rejected() -> None:
    """Unsigned message from non-connected agent → 403."""
    import server

    path_a = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = server._agent_state

        message_id = f"msg-{uuid.uuid4().hex[:8]}"

        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/message", json={
                "message_id": message_id,
                "content": "No signature, no connection",
                "webhook": "http://localhost:9900/__darkmatter__/webhook/" + message_id,
                "from_agent_id": "random-agent-456",
            })

        report("unsigned unknown sender rejected (403)", resp.status_code == 403)
        report("error mentions 'unsigned'",
               "unsigned" in resp.json().get("error", "").lower()
               or "not connected" in resp.json().get("error", "").lower())

        queued = [m for m in stashed_a.message_queue if m.message_id == message_id]
        report("message not queued", len(queued) == 0)
    finally:
        if os.path.exists(path_a):
            os.unlink(path_a)


async def test_connection_accepted_url_mismatch() -> None:
    """connection_accepted with different URL (public IP) matches by agent_id."""
    import server

    path_a = make_state_file()
    path_b = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = server._agent_state

        app_b, state_b = create_agent(path_b, port=9901)

        # A sends a connection request to B (records pending_outbound with localhost URL)
        use_agent(stashed_a)
        stashed_a.pending_outbound["http://localhost:9901"] = state_b.agent_id

        # B accepts — but calls back with a DIFFERENT URL (simulating public IP)
        public_ip_url = "http://192.168.1.100:9901/mcp"
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/connection_accepted", json={
                "agent_id": state_b.agent_id,
                "agent_url": public_ip_url,
                "agent_bio": state_b.bio,
                "agent_public_key_hex": state_b.public_key_hex,
            })

        report("connection_accepted with URL mismatch → 200", resp.status_code == 200)

        # Connection should be recorded
        conn = stashed_a.connections.get(state_b.agent_id)
        report("connection stored with agent_id",
               conn is not None and conn.agent_id == state_b.agent_id)

        # pending_outbound should be cleared
        report("pending_outbound cleared", len(stashed_a.pending_outbound) == 0)
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_router_mode_null_in_state() -> None:
    """State file with router_mode: null loads as 'spawn' (not None)."""
    import server

    path = make_state_file()
    try:
        # Write a state file with explicit null router_mode (legacy)
        priv, pub = server._generate_keypair()
        state_data = {
            "agent_id": pub,
            "bio": "Test agent",
            "status": "active",
            "port": 9900,
            "created_at": "2026-01-01T00:00:00+00:00",
            "messages_handled": 0,
            "private_key_hex": priv,
            "public_key_hex": pub,
            "connections": {},
            "sent_messages": {},
            "impressions": {},
            "router_mode": None,  # explicit null
            "routing_rules": [],
        }
        with open(path, "w") as f:
            json.dump(state_data, f)

        state = server._load_state_from_file(path)

        report("state loaded successfully", state is not None)
        if state:
            report("router_mode is 'spawn' (not None)",
                   state.router_mode == "spawn",
                   f"got: {state.router_mode!r}")
        else:
            report("router_mode is 'spawn' (not None)", False, "state failed to load")
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    print(f"\n{BOLD}DarkMatter Crypto Integration Tests{RESET}\n")

    tests = [
        ("1. Fresh agent keypair", test_fresh_agent_gets_keypair),
        ("2. State migration", test_state_migration),
        ("3. Connection handshake", test_connection_handshake_exchanges_keys),
        ("4. Signed message verified", test_signed_message_verified),
        ("5. Key mismatch rejected", test_key_mismatch_rejected),
        ("6. Invalid signature rejected", test_invalid_signature_rejected),
        ("7. Signed unknown sender accepted", test_signed_unknown_sender_accepted),
        ("8. Unsigned unknown sender rejected", test_unsigned_unknown_sender_rejected),
        ("9. Connection accepted URL mismatch", test_connection_accepted_url_mismatch),
        ("10. Router mode null in state", test_router_mode_null_in_state),
    ]

    for label, test_fn in tests:
        print(f"\n{BOLD}{label}{RESET}")
        try:
            await test_fn()
        except Exception as e:
            report(label, False, f"EXCEPTION: {e}")

    # Summary
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'─' * 40}")
    if passed == total:
        print(f"{GREEN}{BOLD}All {total} checks passed.{RESET}")
    else:
        failed = total - passed
        print(f"{RED}{BOLD}{failed}/{total} checks failed.{RESET}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
