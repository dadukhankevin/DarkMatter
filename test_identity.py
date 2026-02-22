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


def create_agent(state_path: str, *, genesis: bool = True, port: int = 9900) -> tuple:
    """Create a fresh DarkMatter app + state, isolated from other agents.

    Returns (app, state) tuple.
    """
    import server

    # Reset the global so create_app() starts fresh
    server._agent_state = None

    os.environ["DARKMATTER_STATE_FILE"] = state_path
    os.environ["DARKMATTER_PORT"] = str(port)
    os.environ["DARKMATTER_GENESIS"] = "true" if genesis else "false"
    os.environ["DARKMATTER_DISCOVERY"] = "false"
    os.environ.pop("DARKMATTER_AGENT_ID", None)
    os.environ.pop("DARKMATTER_MCP_TOKEN", None)

    app = server.create_app()
    state = server._agent_state
    return app, state


def use_agent(state):
    """Set the global _agent_state to this agent's state before making requests."""
    import server
    server._agent_state = state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_fresh_agent_gets_keypair() -> None:
    """Fresh agent gets UUID + Ed25519 keypair."""
    import server

    path = make_state_file()
    try:
        app, state = create_agent(path)

        # agent_id is a valid UUID
        try:
            uuid.UUID(state.agent_id)
            id_ok = True
        except ValueError:
            id_ok = False
        report("agent_id is UUID", id_ok, f"got: {state.agent_id}")

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

        # Persisted to state file
        with open(path) as f:
            saved = json.load(f)
        report("keypair persisted to state file",
               saved.get("private_key_hex") == state.private_key_hex
               and saved.get("public_key_hex") == state.public_key_hex)
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_state_migration() -> None:
    """Legacy state (no crypto fields) gets keypair auto-generated."""
    import server

    path = make_state_file()
    try:
        # Write a legacy state file without crypto fields
        legacy = {
            "agent_id": "legacy-agent-123",
            "bio": "Old agent",
            "status": "active",
            "port": 9900,
            "is_genesis": True,
            "created_at": "2024-01-01T00:00:00+00:00",
            "messages_handled": 5,
            "connections": {},
            "sent_messages": {},
            "impressions": {},
        }
        with open(path, "w") as f:
            json.dump(legacy, f)

        os.environ["DARKMATTER_STATE_FILE"] = path
        server._agent_state = None
        state = server.load_state()

        report("agent_id preserved", state.agent_id == "legacy-agent-123")
        report("private_key_hex generated",
               isinstance(state.private_key_hex, str) and len(state.private_key_hex) == 64)
        report("public_key_hex generated",
               isinstance(state.public_key_hex, str) and len(state.public_key_hex) == 64)
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_connection_handshake_exchanges_keys() -> None:
    """Connection request to genesis agent exchanges public keys."""
    import server

    path_a = make_state_file()
    path_b = make_state_file()
    try:
        # Agent A: genesis
        app_a, state_a = create_agent(path_a, genesis=True, port=9900)
        # Stash A's state
        stashed_a = server._agent_state

        # Agent B: non-genesis
        app_b, state_b = create_agent(path_b, genesis=False, port=9901)

        # B sends connection_request to A — restore A's state for the handler
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/connection_request", json={
                "from_agent_id": state_b.agent_id,
                "from_agent_url": "http://localhost:9901/mcp",
                "from_agent_bio": "Test agent B",
                "from_agent_public_key_hex": state_b.public_key_hex,
            })

        data = resp.json()
        report("connection auto-accepted (genesis)", data.get("auto_accepted") is True)
        report("response includes agent_public_key_hex",
               isinstance(data.get("agent_public_key_hex"), str)
               and len(data["agent_public_key_hex"]) == 64)

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
        app_a, state_a = create_agent(path_a, genesis=True, port=9900)
        stashed_a = server._agent_state

        app_b, state_b = create_agent(path_b, genesis=False, port=9901)

        # Connect B → A (restore A's state for the handler)
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            await client.post("/__darkmatter__/connection_request", json={
                "from_agent_id": state_b.agent_id,
                "from_agent_url": "http://localhost:9901/mcp",
                "from_agent_bio": "Test agent B",
                "from_agent_public_key_hex": state_b.public_key_hex,
            })

        # B signs and sends a message to A
        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = "2025-01-01T00:00:00Z"
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
        app_a, state_a = create_agent(path_a, genesis=True, port=9900)
        stashed_a = server._agent_state

        app_b, state_b = create_agent(path_b, genesis=False, port=9901)

        # Connect B → A
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            await client.post("/__darkmatter__/connection_request", json={
                "from_agent_id": state_b.agent_id,
                "from_agent_url": "http://localhost:9901/mcp",
                "from_agent_bio": "Test agent B",
                "from_agent_public_key_hex": state_b.public_key_hex,
            })

        # Generate a different keypair (impersonator)
        fake_priv, fake_pub = server._generate_keypair()

        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = "2025-01-01T00:00:00Z"
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
        app_a, state_a = create_agent(path_a, genesis=True, port=9900)
        stashed_a = server._agent_state

        app_b, state_b = create_agent(path_b, genesis=False, port=9901)

        # Connect B → A
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            await client.post("/__darkmatter__/connection_request", json={
                "from_agent_id": state_b.agent_id,
                "from_agent_url": "http://localhost:9901/mcp",
                "from_agent_bio": "Test agent B",
                "from_agent_public_key_hex": state_b.public_key_hex,
            })

        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = "2025-01-01T00:00:00Z"
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


async def test_unknown_sender_rejected() -> None:
    """Message from unknown agent_id (not in connections) → 403."""
    import server

    path_a = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, genesis=True, port=9900)
        stashed_a = server._agent_state

        # Send message from an agent that's NOT connected
        unknown_priv, unknown_pub = server._generate_keypair()
        unknown_id = "unknown-agent-999"
        message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = "2025-01-01T00:00:00Z"
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

        report("unknown sender rejected (403)", resp.status_code == 403)
        report("error mentions 'Not connected'",
               "Not connected" in resp.json().get("error", ""))

        # Message should NOT be queued
        queued = [m for m in stashed_a.message_queue if m.message_id == message_id]
        report("message not queued", len(queued) == 0)
    finally:
        if os.path.exists(path_a):
            os.unlink(path_a)


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
        ("7. Unknown sender rejected", test_unknown_sender_rejected),
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
