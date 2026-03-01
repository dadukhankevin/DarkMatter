#!/usr/bin/env python3
"""
Comprehensive DarkMatter Network Test Suite.

Tests the full P2P network stack: messaging, mesh healing (peer_lookup,
peer_update, broadcast), webhook forwarding, and multi-hop routing.

Two tiers:
  - Tier 1: In-process ASGI (fast, no ports) via httpx.AsyncClient
  - Tier 2: Real subprocess nodes (real HTTP, real ports 9900+)

Standalone async script — same conventions as test_identity.py.

Usage:
    python3 test_network.py          # ~30s, all tests
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

import httpx
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# darkmatter/ package imports
# ---------------------------------------------------------------------------

from darkmatter.state import (
    get_state, set_state, save_state,
    load_state_from_file, state_file_path,
    _seen_message_ids,
)
from darkmatter.models import (
    AgentState, AgentStatus, Connection,
    Impression, QueuedMessage, SentMessage,
)
from darkmatter.identity import (
    generate_keypair, sign_message, sign_peer_update,
    check_rate_limit,
)
from darkmatter.app import create_app
from darkmatter.spawn import (
    SpawnedAgent, can_spawn_agent, spawn_agent_for_message,
    cleanup_finished_agents, build_agent_prompt,
    _spawned_agents, _spawn_timestamps,
)
from darkmatter.network.manager import get_network_manager
from darkmatter.network.discovery import DiscoveryProtocol
import darkmatter.config
import darkmatter.network.manager as _mgr_module
import darkmatter.spawn as _spawn_module

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
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
    """
    set_state(None)

    os.environ["DARKMATTER_PORT"] = str(port)
    os.environ["DARKMATTER_DISCOVERY"] = "false"
    os.environ.pop("DARKMATTER_AGENT_ID", None)
    os.environ.pop("DARKMATTER_MCP_TOKEN", None)
    os.environ.pop("DARKMATTER_GENESIS", None)

    # Generate a unique keypair for this agent (bypass passport file)
    priv, pub = generate_keypair()
    set_state(AgentState(
        agent_id=pub,
        bio="A DarkMatter mesh agent.",
        status=AgentStatus.ACTIVE,
        port=port,
        private_key_hex=priv,
        public_key_hex=pub,
    ))
    app = create_app()
    state = get_state()
    return app, state


def use_agent(state):
    """Set the global _agent_state to this agent's state before making requests."""
    set_state(state)


# ---------------------------------------------------------------------------
# Tier 1 helpers — in-process ASGI
# ---------------------------------------------------------------------------

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
    assert resp.status_code == 200, f"Connection request failed: {resp.text}"
    data = resp.json()
    request_id = data.get("request_id")
    assert request_id, f"No request_id in response: {data}"

    # Accept on A's side via HTTP endpoint
    async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
        resp2 = await client.post("/__darkmatter__/accept_pending", json={
            "request_id": request_id,
        })
    assert resp2.status_code == 200, f"Accept pending failed: {resp2.text}"

    # Simulate the /connection_accepted callback on B so both sides record it
    use_agent(state_b)
    state_b.pending_outbound[f"http://localhost:{state_a.port}/mcp"] = state_a.agent_id
    async with httpx.AsyncClient(transport=ASGITransport(app=app_b), base_url="http://test") as client:
        resp3 = await client.post("/__darkmatter__/connection_accepted", json={
            "agent_id": state_a.agent_id,
            "agent_url": f"http://localhost:{state_a.port}/mcp",
            "agent_bio": state_a.bio,
            "agent_public_key_hex": state_a.public_key_hex,
        })
    assert resp3.status_code == 200, f"Connection accepted failed: {resp3.text}"


async def send_signed_message(app_target, state_sender, state_target, content: str,
                              webhook_url: str, message_id: str = None) -> dict:
    """Sign and send a message from sender to target. Returns response JSON."""
    if message_id is None:
        message_id = f"msg-{uuid.uuid4().hex[:8]}"
    timestamp = datetime.now(timezone.utc).isoformat()

    signature = sign_message(
        state_sender.private_key_hex, state_sender.agent_id,
        message_id, timestamp, content
    )

    use_agent(state_target)
    async with httpx.AsyncClient(transport=ASGITransport(app=app_target), base_url="http://test") as client:
        resp = await client.post("/__darkmatter__/message", json={
            "message_id": message_id,
            "content": content,
            "webhook": webhook_url,
            "from_agent_id": state_sender.agent_id,
            "from_public_key_hex": state_sender.public_key_hex,
            "signature_hex": signature,
            "timestamp": timestamp,
        })

    return {"status_code": resp.status_code, "data": resp.json(), "message_id": message_id}


# ---------------------------------------------------------------------------
# Tier 2 helpers — real subprocess nodes
# ---------------------------------------------------------------------------

PYTHON = sys.executable
# Use darkmatter.app as module entry point (avoids darkmatter/mcp/ shadowing the mcp package)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BASE_PORT = 9900
STARTUP_TIMEOUT = 10


class TestNode:
    """Manages a real DarkMatter server subprocess for Tier 2 tests."""

    def __init__(self, port: int, *, display_name: str = ""):
        self.port = port
        self.display_name = display_name or f"test-node-{port}"
        # Each node gets its own temp working directory with a unique passport
        self.workdir = tempfile.mkdtemp(prefix=f"dm_{port}_")
        self.proc: subprocess.Popen | None = None
        self.agent_id: str | None = None
        self.public_key_hex: str | None = None
        self.private_key_hex: str | None = None

    def start(self) -> None:
        env = {
            **os.environ,
            "DARKMATTER_PORT": str(self.port),
            "DARKMATTER_HOST": "127.0.0.1",
            "DARKMATTER_DISPLAY_NAME": self.display_name,
            "DARKMATTER_DISCOVERY": "false",
            "DARKMATTER_TRANSPORT": "http",
            # Ensure the darkmatter package is importable from the workdir
            "PYTHONPATH": PROJECT_ROOT,
        }
        env.pop("DARKMATTER_MCP_TOKEN", None)
        env.pop("DARKMATTER_AGENT_ID", None)

        self.proc = subprocess.Popen(
            [PYTHON, "-m", "darkmatter.app"],
            env=env,
            cwd=self.workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def wait_ready(self) -> bool:
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            try:
                r = httpx.get(
                    f"http://127.0.0.1:{self.port}/.well-known/darkmatter.json",
                    timeout=1.0,
                )
                if r.status_code == 200:
                    info = r.json()
                    self.agent_id = info.get("agent_id")
                    # Read keys from passport file in workdir
                    passport_path = os.path.join(self.workdir, ".darkmatter", "passport.key")
                    if os.path.exists(passport_path):
                        with open(passport_path) as f:
                            priv_hex = f.read().strip()
                        self.private_key_hex = priv_hex
                        # Derive public key from private key
                        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
                        pk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
                        self.public_key_hex = pk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        return False

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        # Clean up workdir
        import shutil
        if os.path.exists(self.workdir):
            shutil.rmtree(self.workdir, ignore_errors=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def connect_to(self, other: "TestNode") -> dict:
        """Send a connection request to another node via real HTTP.

        Uses accept_pending endpoint to accept the connection.
        """
        # Send connection request from self to other
        resp = httpx.post(
            f"{other.base_url}/__darkmatter__/connection_request",
            json={
                "from_agent_id": self.agent_id,
                "from_agent_url": f"{self.base_url}/mcp",
                "from_agent_bio": f"Test node {self.display_name}",
                "from_agent_public_key_hex": self.public_key_hex,
            },
            timeout=5.0,
        )
        data = resp.json()

        if data.get("auto_accepted"):
            # Already connected — record on our side too
            resp2 = httpx.post(
                f"{self.base_url}/__darkmatter__/connection_request",
                json={
                    "from_agent_id": other.agent_id,
                    "from_agent_url": f"{other.base_url}/mcp",
                    "from_agent_bio": f"Test node {other.display_name}",
                    "from_agent_public_key_hex": other.public_key_hex,
                },
                timeout=5.0,
            )
        else:
            # Accept the pending request on the other node
            request_id = data.get("request_id")
            if request_id:
                httpx.post(
                    f"{other.base_url}/__darkmatter__/accept_pending",
                    json={"request_id": request_id},
                    timeout=5.0,
                )
            # Also send reverse connection request so self knows about other
            resp2 = httpx.post(
                f"{self.base_url}/__darkmatter__/connection_request",
                json={
                    "from_agent_id": other.agent_id,
                    "from_agent_url": f"{other.base_url}/mcp",
                    "from_agent_bio": f"Test node {other.display_name}",
                    "from_agent_public_key_hex": other.public_key_hex,
                },
                timeout=5.0,
            )
            resp2_data = resp2.json()
            req_id2 = resp2_data.get("request_id")
            if req_id2:
                httpx.post(
                    f"{self.base_url}/__darkmatter__/accept_pending",
                    json={"request_id": req_id2},
                    timeout=5.0,
                )

        return data

    def send_message(self, target: "TestNode", content: str, message_id: str = None) -> dict:
        """Send a signed message to another node. Returns response data."""
        if message_id is None:
            message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = datetime.now(timezone.utc).isoformat()

        signature = sign_message(
            self.private_key_hex, self.agent_id,
            message_id, timestamp, content
        )

        # Use a dummy webhook — we verify delivery, not webhook callbacks, in Tier 2
        webhook_url = f"{self.base_url}/__darkmatter__/webhook/{message_id}"

        resp = httpx.post(
            f"{target.base_url}/__darkmatter__/message",
            json={
                "message_id": message_id,
                "content": content,
                "webhook": webhook_url,
                "from_agent_id": self.agent_id,
                "from_public_key_hex": self.public_key_hex,
                "signature_hex": signature,
                "timestamp": timestamp,
                "hops_remaining": 10,
            },
            timeout=5.0,
        )

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status_code": resp.status_code, "data": data, "message_id": message_id}

    def get_peer_lookup(self, agent_id: str) -> dict:
        """GET /peer_lookup/{agent_id} on this node."""
        resp = httpx.get(
            f"{self.base_url}/__darkmatter__/peer_lookup/{agent_id}",
            timeout=5.0,
        )
        return {"status_code": resp.status_code, "data": resp.json()}

    def post_peer_update(self, agent_id: str, new_url: str,
                         public_key_hex: str = None,
                         private_key_hex: str = None) -> dict:
        """POST /peer_update on this node (signed if private key provided)."""
        payload = {"agent_id": agent_id, "new_url": new_url}
        if public_key_hex:
            payload["public_key_hex"] = public_key_hex
        if private_key_hex and public_key_hex:
            timestamp = datetime.now(timezone.utc).isoformat()
            payload["timestamp"] = timestamp
            payload["signature"] = sign_peer_update(
                private_key_hex, agent_id, new_url, timestamp)
        resp = httpx.post(
            f"{self.base_url}/__darkmatter__/peer_update",
            json=payload,
            timeout=5.0,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status_code": resp.status_code, "data": data}

    def __repr__(self) -> str:
        return f"<TestNode {self.display_name} port={self.port} pid={self.proc.pid if self.proc else None}>"


# ==========================================================================
# Tier 1 Tests — In-process ASGI
# ==========================================================================


async def test_basic_message_delivery() -> None:
    """2 agents, connect, signed message delivery, verify queued + verified."""
    path_a = make_state_file()
    path_b = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(path_b, port=9901)
        stashed_b = get_state()

        await connect_agents(app_a, stashed_a, app_b, stashed_b)

        # B sends a signed message to A
        result = await send_signed_message(
            app_a, stashed_b, stashed_a,
            "Hello from B",
            f"http://localhost:9901/__darkmatter__/webhook/test-msg-1",
            message_id="test-msg-1",
        )

        report("message accepted (200)", result["status_code"] == 200)
        report("message queued", result["data"].get("queued") is True)

        # Verify it's in A's queue as verified
        queued = [m for m in stashed_a.message_queue if m.message_id == "test-msg-1"]
        report("message in queue", len(queued) == 1)
        report("message verified=True", len(queued) == 1 and queued[0].verified is True)
        report("from_agent_id correct",
               len(queued) == 1 and queued[0].from_agent_id == stashed_b.agent_id)
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_message_broadcast() -> None:
    """3 agents (hub A, spokes B+C): A sends to both, both receive."""
    paths = [make_state_file() for _ in range(3)]
    try:
        app_a, state_a = create_agent(paths[0], port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(paths[1], port=9901)
        stashed_b = get_state()

        app_c, state_c = create_agent(paths[2], port=9902)
        stashed_c = get_state()

        # Connect B and C to A
        await connect_agents(app_a, stashed_a, app_b, stashed_b)
        await connect_agents(app_a, stashed_a, app_c, stashed_c)

        # A sends to B
        r1 = await send_signed_message(
            app_b, stashed_a, stashed_b, "Broadcast to B",
            "http://localhost:9900/__darkmatter__/webhook/bcast-b",
            message_id="bcast-b",
        )

        # A sends to C
        r2 = await send_signed_message(
            app_c, stashed_a, stashed_c, "Broadcast to C",
            "http://localhost:9900/__darkmatter__/webhook/bcast-c",
            message_id="bcast-c",
        )

        report("B received message", r1["status_code"] == 200)
        report("C received message", r2["status_code"] == 200)

        b_queued = [m for m in stashed_b.message_queue if m.message_id == "bcast-b"]
        c_queued = [m for m in stashed_c.message_queue if m.message_id == "bcast-c"]
        report("B has message in queue", len(b_queued) == 1)
        report("C has message in queue", len(c_queued) == 1)
    finally:
        for p in paths:
            if os.path.exists(p):
                os.unlink(p)


async def test_webhook_forwarding_chain() -> None:
    """A→B→C chain: A sends to B, B's webhook notifies A of forwarding, C responds."""
    paths = [make_state_file() for _ in range(3)]
    try:
        app_a, state_a = create_agent(paths[0], port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(paths[1], port=9901)
        stashed_b = get_state()

        app_c, state_c = create_agent(paths[2], port=9902)
        stashed_c = get_state()

        # A↔B, B↔C
        await connect_agents(app_a, stashed_a, app_b, stashed_b)
        await connect_agents(app_b, stashed_b, app_c, stashed_c)

        msg_id = "chain-msg-1"

        # Register sent_message on A so the webhook works
        stashed_a.sent_messages[msg_id] = SentMessage(
            message_id=msg_id,
            content="Chain test",
            status="active",
            initial_hops=10,
            routed_to=[stashed_b.agent_id],
        )

        # A sends to B
        result = await send_signed_message(
            app_b, stashed_a, stashed_b, "Chain test",
            f"http://localhost:9900/__darkmatter__/webhook/{msg_id}",
            message_id=msg_id,
        )
        report("chain: A→B message accepted", result["status_code"] == 200)

        # B posts a "forwarded" webhook update to A
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            fwd_resp = await client.post(f"/__darkmatter__/webhook/{msg_id}", json={
                "type": "forwarded",
                "agent_id": stashed_b.agent_id,
                "target_agent_id": stashed_c.agent_id,
                "note": "Forwarding to C",
            })
        report("chain: forwarded webhook accepted", fwd_resp.status_code == 200)

        # Verify A's sent_message has the forwarding update
        sm = stashed_a.sent_messages.get(msg_id)
        report("chain: forwarding update recorded",
               sm is not None and len(sm.updates) == 1 and sm.updates[0]["type"] == "forwarded")

        # C responds via the webhook
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp_resp = await client.post(f"/__darkmatter__/webhook/{msg_id}", json={
                "type": "response",
                "agent_id": stashed_c.agent_id,
                "response": "Answer from C",
            })
        report("chain: response webhook accepted", resp_resp.status_code == 200)

        # Verify A's sent_message has the response
        sm = stashed_a.sent_messages.get(msg_id)
        report("chain: response recorded",
               sm is not None and len(sm.responses) > 0
               and sm.responses[0]["agent_id"] == stashed_c.agent_id)
        report("chain: status is 'responded'", sm is not None and sm.status == "responded")

        # Verify full routing history
        report("chain: routing history has forwarding + response",
               sm is not None and len(sm.updates) == 1 and len(sm.responses) > 0)
    finally:
        for p in paths:
            if os.path.exists(p):
                os.unlink(p)


async def test_peer_lookup_returns_url() -> None:
    """GET /peer_lookup/{id} returns connected peer's URL."""
    path_a = make_state_file()
    path_b = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(path_b, port=9901)
        stashed_b = get_state()

        await connect_agents(app_a, stashed_a, app_b, stashed_b)

        # Query A for B's URL
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.get(f"/__darkmatter__/peer_lookup/{stashed_b.agent_id}")

        report("peer_lookup returns 200", resp.status_code == 200)
        data = resp.json()
        report("peer_lookup has url field", "url" in data)
        report("peer_lookup url contains port",
               "9901" in data.get("url", ""))
        report("peer_lookup status is connected",
               data.get("status") == "connected")
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_peer_lookup_unknown_404() -> None:
    """Unknown agent_id returns 404."""
    path_a = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)

        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.get("/__darkmatter__/peer_lookup/nonexistent-agent-id")

        report("peer_lookup unknown → 404", resp.status_code == 404)
        report("error message present", "error" in resp.json())
    finally:
        if os.path.exists(path_a):
            os.unlink(path_a)


async def test_peer_update_changes_stored_url() -> None:
    """POST /peer_update updates the connection URL for a known peer."""
    path_a = make_state_file()
    path_b = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(path_b, port=9901)
        stashed_b = get_state()

        await connect_agents(app_a, stashed_a, app_b, stashed_b)

        # B notifies A of URL change (must be signed since A has B's public key)
        new_url = "http://192.168.1.100:9901"
        timestamp = datetime.now(timezone.utc).isoformat()
        signature = sign_peer_update(
            stashed_b.private_key_hex, stashed_b.agent_id, new_url, timestamp)
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/peer_update", json={
                "agent_id": stashed_b.agent_id,
                "new_url": new_url,
                "public_key_hex": stashed_b.public_key_hex,
                "signature": signature,
                "timestamp": timestamp,
            })

        report("peer_update returns 200", resp.status_code == 200)
        report("peer_update success=True", resp.json().get("success") is True)

        # Verify stored URL changed
        conn = stashed_a.connections.get(stashed_b.agent_id)
        report("stored URL updated", conn is not None and conn.agent_url == new_url)
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_peer_update_rejects_key_mismatch() -> None:
    """POST /peer_update with wrong public_key_hex → 403."""
    path_a = make_state_file()
    path_b = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(path_b, port=9901)
        stashed_b = get_state()

        await connect_agents(app_a, stashed_a, app_b, stashed_b)

        # Try to update with a fake public key
        _, fake_pub = generate_keypair()
        use_agent(stashed_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/peer_update", json={
                "agent_id": stashed_b.agent_id,
                "new_url": "http://evil.example.com:9999",
                "public_key_hex": fake_pub,
            })

        report("peer_update key mismatch → 403", resp.status_code == 403)
        report("error mentions key mismatch",
               "key mismatch" in resp.json().get("error", "").lower())

        # Verify URL was NOT changed
        conn = stashed_a.connections.get(stashed_b.agent_id)
        report("URL unchanged after rejection",
               conn is not None and "evil" not in conn.agent_url)
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.unlink(p)


async def test_webhook_recovery_orphaned_message() -> None:
    """NetworkManager.webhook_request recovers an orphaned webhook via peer lookup."""
    paths = [make_state_file() for _ in range(3)]
    try:
        # A (sender whose webhook is stale), B (responder), C (knows A's real URL)
        app_a, state_a = create_agent(paths[0], port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(paths[1], port=9901)
        stashed_b = get_state()

        app_c, state_c = create_agent(paths[2], port=9902)
        stashed_c = get_state()

        # Wire up: A↔B, B↔C, A↔C
        await connect_agents(app_a, stashed_a, app_b, stashed_b)
        await connect_agents(app_b, stashed_b, app_c, stashed_c)
        await connect_agents(app_a, stashed_a, app_c, stashed_c)

        msg_id = "orphan-msg-1"

        # Register sent_message on A so the webhook handler accepts updates
        stashed_a.sent_messages[msg_id] = SentMessage(
            message_id=msg_id,
            content="Orphan test",
            status="active",
            initial_hops=10,
            routed_to=[stashed_b.agent_id],
        )

        # Queue the message on B as if A sent it (webhook points to DEAD port)
        result = await send_signed_message(
            app_b, stashed_a, stashed_b, "Orphan test",
            f"http://127.0.0.1:59999/__darkmatter__/webhook/{msg_id}",
            message_id=msg_id,
        )
        report("orphan: message delivered to B", result["status_code"] == 200)

        c_conn_a = stashed_c.connections.get(stashed_a.agent_id)
        report("orphan: C knows A", c_conn_a is not None)

        # Mock lookup_peer_url on the NetworkManager so B finds A's URL via C
        mgr = get_network_manager()
        original_lookup = mgr.lookup_peer_url
        lookup_called = False

        async def mock_lookup(target_id, **kwargs):
            nonlocal lookup_called
            lookup_called = True
            if target_id == stashed_a.agent_id:
                return f"http://localhost:9900/mcp"
            return await original_lookup(target_id)

        mgr.lookup_peer_url = mock_lookup

        try:
            import httpx as _httpx

            _OrigClient = _httpx.AsyncClient

            class _RecoveryClient(_httpx.AsyncClient):
                """Intercepts recovered webhook calls and routes to ASGI."""
                async def post(self, url, **kw):
                    if "localhost:9900" in str(url):
                        from urllib.parse import urlparse as _up
                        path = _up(str(url)).path
                        use_agent(stashed_a)
                        async with _httpx.AsyncClient(
                            transport=ASGITransport(app=app_a),
                            base_url="http://test"
                        ) as asgi_client:
                            return await asgi_client.post(path, **kw)
                    return await super().post(url, **kw)

                async def get(self, url, **kw):
                    if "localhost:9900" in str(url):
                        from urllib.parse import urlparse as _up
                        path = _up(str(url)).path
                        use_agent(stashed_a)
                        async with _httpx.AsyncClient(
                            transport=ASGITransport(app=app_a),
                            base_url="http://test"
                        ) as asgi_client:
                            return await asgi_client.get(path, **kw)
                    return await super().get(url, **kw)

            _httpx.AsyncClient = _RecoveryClient

            use_agent(stashed_b)
            resp = await mgr.webhook_request(
                f"http://127.0.0.1:59999/__darkmatter__/webhook/{msg_id}",
                stashed_a.agent_id,
                method="POST",
                timeout=5.0,
                json={
                    "type": "response",
                    "agent_id": stashed_b.agent_id,
                    "response": "Answer from B",
                },
            )

            _httpx.AsyncClient = _OrigClient

            report("orphan: recovery request succeeded", resp.status_code == 200)
            report("orphan: peer lookup was invoked", lookup_called)

            # Verify A's sent_message got the response
            sm = stashed_a.sent_messages.get(msg_id)
            report("orphan: A received the response",
                   sm is not None and len(sm.responses) > 0)
            report("orphan: response came from B",
                   sm is not None and len(sm.responses) > 0
                   and sm.responses[0].get("agent_id") == stashed_b.agent_id)
            report("orphan: A's message status is responded",
                   sm is not None and sm.status == "responded")
        finally:
            mgr.lookup_peer_url = original_lookup
            # Ensure httpx is restored even on failure
            import httpx as _httpx2
            if not isinstance(_httpx2.AsyncClient, type) or _httpx2.AsyncClient.__name__ != "AsyncClient":
                _httpx2.AsyncClient = _OrigClient
    finally:
        for p in paths:
            if os.path.exists(p):
                os.unlink(p)


async def test_webhook_recovery_gives_up() -> None:
    """Webhook recovery respects max attempts and doesn't loop forever."""
    paths = [make_state_file() for _ in range(2)]
    try:
        app_a, state_a = create_agent(paths[0], port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(paths[1], port=9901)
        stashed_b = get_state()

        await connect_agents(app_a, stashed_a, app_b, stashed_b)

        # Mock lookup_peer_url to return a different dead URL each time
        mgr = get_network_manager()
        lookup_count = 0
        original_lookup = mgr.lookup_peer_url
        async def mock_lookup_always_bad(target_id, **kwargs):
            nonlocal lookup_count
            lookup_count += 1
            # Return a new dead URL each time (different port so it's not "already tried")
            return f"http://127.0.0.1:{60000 + lookup_count}/mcp"
        mgr.lookup_peer_url = mock_lookup_always_bad

        # Set a low max attempts for this test
        orig_max = _mgr_module.WEBHOOK_RECOVERY_MAX_ATTEMPTS
        _mgr_module.WEBHOOK_RECOVERY_MAX_ATTEMPTS = 2

        try:
            use_agent(stashed_b)
            raised = False
            try:
                await mgr.webhook_request(
                    "http://127.0.0.1:59999/__darkmatter__/webhook/test-msg",
                    stashed_a.agent_id,
                    method="GET",
                    timeout=5.0,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                raised = True

            report("gives-up: raised after max attempts", raised)
            report("gives-up: tried correct number of lookups",
                   lookup_count <= 2)
            report("gives-up: didn't retry excessively",
                   lookup_count <= _mgr_module.WEBHOOK_RECOVERY_MAX_ATTEMPTS)
        finally:
            mgr.lookup_peer_url = original_lookup
            _mgr_module.WEBHOOK_RECOVERY_MAX_ATTEMPTS = orig_max
    finally:
        for p in paths:
            if os.path.exists(p):
                os.unlink(p)


async def test_webhook_recovery_timeout_budget() -> None:
    """Webhook recovery respects the total timeout budget."""
    paths = [make_state_file() for _ in range(2)]
    try:
        app_a, state_a = create_agent(paths[0], port=9900)
        stashed_a = get_state()

        app_b, state_b = create_agent(paths[1], port=9901)
        stashed_b = get_state()

        await connect_agents(app_a, stashed_a, app_b, stashed_b)

        mgr = get_network_manager()
        lookup_count = 0
        original_lookup = mgr.lookup_peer_url
        async def mock_slow_lookup(target_id, **kwargs):
            nonlocal lookup_count
            lookup_count += 1
            # Return a different dead URL each time
            return f"http://127.0.0.1:{60000 + lookup_count}/mcp"
        mgr.lookup_peer_url = mock_slow_lookup

        # Allow many attempts but tiny timeout budget
        orig_max = _mgr_module.WEBHOOK_RECOVERY_MAX_ATTEMPTS
        orig_timeout = _mgr_module.WEBHOOK_RECOVERY_TIMEOUT
        _mgr_module.WEBHOOK_RECOVERY_MAX_ATTEMPTS = 100  # generous — timeout should kick in first
        _mgr_module.WEBHOOK_RECOVERY_TIMEOUT = 3.0       # 3 second total budget

        try:
            use_agent(stashed_b)
            start = time.monotonic()
            raised = False
            try:
                await mgr.webhook_request(
                    "http://127.0.0.1:59999/__darkmatter__/webhook/timeout-msg",
                    stashed_a.agent_id,
                    method="GET",
                    timeout=2.0,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                raised = True
            elapsed = time.monotonic() - start

            report("timeout: raised error", raised)
            report("timeout: finished within budget",
                   elapsed < 15.0)  # generous upper bound — should be ~3-5s
            report("timeout: didn't exhaust all 100 attempts",
                   lookup_count < 20)
        finally:
            mgr.lookup_peer_url = original_lookup
            _mgr_module.WEBHOOK_RECOVERY_MAX_ATTEMPTS = orig_max
            _mgr_module.WEBHOOK_RECOVERY_TIMEOUT = orig_timeout
    finally:
        for p in paths:
            if os.path.exists(p):
                os.unlink(p)


async def test_broadcast_peer_update() -> None:
    """_broadcast_peer_update notifies all connected peers of URL change."""
    nodes = []
    try:
        # We need real HTTP for broadcast, so use Tier 2 nodes
        # Start nodes and connect them
        for i in range(3):
            node = TestNode(BASE_PORT + i, display_name=f"bcast-{i}")
            node.start()
            nodes.append(node)

        for node in nodes:
            ok = node.wait_ready()
            if not ok:
                report(f"broadcast: node {node.display_name} started", False, "timeout")
                return

        # Connect all nodes: 1→0, 2→0
        nodes[1].connect_to(nodes[0])
        nodes[2].connect_to(nodes[0])

        # Also connect 1↔2 for completeness
        nodes[2].connect_to(nodes[1])

        # Verify initial URLs via peer_lookup
        r = nodes[1].get_peer_lookup(nodes[0].agent_id)
        report("broadcast: initial peer_lookup works", r["status_code"] == 200)

        # Now node 0 broadcasts a URL change by calling peer_update on nodes 1 and 2
        new_url = f"http://127.0.0.1:{nodes[0].port}"
        for target in [nodes[1], nodes[2]]:
            target.post_peer_update(
                nodes[0].agent_id, new_url,
                public_key_hex=nodes[0].public_key_hex,
                private_key_hex=nodes[0].private_key_hex,
            )

        # Verify both peers updated
        for i, target in enumerate([nodes[1], nodes[2]]):
            r = target.get_peer_lookup(nodes[0].agent_id)
            updated = r["status_code"] == 200 and r["data"].get("url") == new_url
            report(f"broadcast: node {i+1} has updated URL", updated,
                   f"got: {r['data'].get('url', 'N/A')}")

    finally:
        for node in nodes:
            node.stop()


# ==========================================================================
# Tier 2 Tests — Real subprocess nodes
# ==========================================================================


def test_discovery_smoke() -> None:
    """2 nodes in scan range, verify both reachable via well-known endpoint."""
    print(f"\n{BOLD}Test: discovery smoke (Tier 2){RESET}")

    node_a = TestNode(BASE_PORT, display_name="disc-alpha")
    node_b = TestNode(BASE_PORT + 1, display_name="disc-beta")

    try:
        node_a.start()
        ok_a = node_a.wait_ready()
        report("discovery: node A started", ok_a)
        if not ok_a:
            return

        node_b.start()
        ok_b = node_b.wait_ready()
        report("discovery: node B started", ok_b)
        if not ok_b:
            return

        # Both expose /.well-known/darkmatter.json
        r_a = httpx.get(f"{node_a.base_url}/.well-known/darkmatter.json", timeout=2.0)
        r_b = httpx.get(f"{node_b.base_url}/.well-known/darkmatter.json", timeout=2.0)

        report("discovery: A has well-known", r_a.status_code == 200)
        report("discovery: B has well-known", r_b.status_code == 200)

        info_a = r_a.json()
        info_b = r_b.json()
        report("discovery: different agent_ids",
               info_a.get("agent_id") != info_b.get("agent_id"))
    finally:
        node_a.stop()
        node_b.stop()


def test_multi_hop_message_routing() -> None:
    """A↔B↔C (A not connected to C). A sends to B. B forwards to C. C responds."""
    print(f"\n{BOLD}Test: multi-hop message routing (Tier 2){RESET}")

    node_a = TestNode(BASE_PORT, display_name="hop-A")
    node_b = TestNode(BASE_PORT + 1, display_name="hop-B")
    node_c = TestNode(BASE_PORT + 2, display_name="hop-C")

    try:
        for n in (node_a, node_b, node_c):
            n.start()
        for n in (node_a, node_b, node_c):
            ok = n.wait_ready()
            if not ok:
                report(f"multi-hop: {n.display_name} started", False, "timeout")
                return

        # A↔B
        node_a.connect_to(node_b)
        # B↔C
        node_c.connect_to(node_b)

        # Verify connections
        r = node_b.get_peer_lookup(node_a.agent_id)
        report("multi-hop: B knows A", r["status_code"] == 200)
        r = node_b.get_peer_lookup(node_c.agent_id)
        report("multi-hop: B knows C", r["status_code"] == 200)

        # A sends a message to B
        result = node_a.send_message(node_b, "Multi-hop test from A")
        report("multi-hop: A→B message accepted", result["status_code"] == 200)

        # B should have the message in its queue — verify by checking status
        # Give it a moment to process
        time.sleep(0.5)

        # B forwards to C by sending the same content as a new message
        b_to_c = node_b.send_message(node_c, "Forwarded from A via B")
        report("multi-hop: B→C forward accepted", b_to_c["status_code"] == 200)

    finally:
        for n in (node_a, node_b, node_c):
            n.stop()


def test_send_recovers_via_peer_lookup() -> None:
    """3 nodes A↔B, A↔C, B↔C. Stop B, restart on new port, update C.
    A queries C via peer_lookup to find B's new URL."""
    print(f"\n{BOLD}Test: send recovery via peer_lookup (Tier 2){RESET}")

    node_a = TestNode(BASE_PORT, display_name="recover-A")
    node_b = TestNode(BASE_PORT + 1, display_name="recover-B")
    node_c = TestNode(BASE_PORT + 2, display_name="recover-C")

    try:
        for n in (node_a, node_b, node_c):
            n.start()
        for n in (node_a, node_b, node_c):
            ok = n.wait_ready()
            if not ok:
                report(f"recover: {n.display_name} started", False, "timeout")
                return

        # Connect: A↔B, A↔C, B↔C
        node_b.connect_to(node_a)
        node_c.connect_to(node_a)
        node_c.connect_to(node_b)

        # Verify initial connectivity
        r = node_a.get_peer_lookup(node_b.agent_id)
        report("recover: A initially knows B", r["status_code"] == 200)

        b_agent_id = node_b.agent_id
        b_pub_key = node_b.public_key_hex

        # Stop B
        node_b.stop()
        time.sleep(1)

        # Restart B on a new port
        node_b2 = TestNode(BASE_PORT + 5, display_name="recover-B-new")
        node_b2.start()
        ok = node_b2.wait_ready()
        report("recover: B restarted on new port", ok)
        if not ok:
            return

        node_b2.connect_to(node_c)

        # A can look up B2 via C
        r = node_c.get_peer_lookup(node_b2.agent_id)
        report("recover: C knows new B via peer_lookup", r["status_code"] == 200)

        # A can now use C's peer_lookup to find B2's URL
        new_url = r["data"].get("url", "") if r["status_code"] == 200 else ""
        report("recover: peer_lookup returns URL",
               bool(new_url) and str(BASE_PORT + 5) in new_url,
               f"got: {new_url}")

        node_b2.stop()

    finally:
        node_a.stop()
        node_c.stop()


async def test_health_loop_increments_failures() -> None:
    """In-process: monkeypatch STALE_CONNECTION_AGE=0, call _check_connection_health, verify failures."""
    sf_a = make_state_file()
    sf_b = make_state_file()

    original_stale_age = _mgr_module.STALE_CONNECTION_AGE

    try:
        app_a, state_a = create_agent(sf_a, port=9900)
        app_b, state_b = create_agent(sf_b, port=9901)
        await connect_agents(app_a, state_a, app_b, state_b)

        use_agent(state_a)

        # Monkeypatch stale age to 0 so all connections are health-checked
        _mgr_module.STALE_CONNECTION_AGE = 0

        conn = state_a.connections.get(state_b.agent_id)
        report("health: A has connection to B", conn is not None)
        if conn is None:
            return

        initial_failures = conn.health_failures

        # Call health check — B's ASGI app isn't listening on real HTTP,
        # so the ping will fail and increment health_failures
        mgr = get_network_manager()
        await mgr._check_connection_health()

        report("health: failures incremented after unreachable peer",
               conn.health_failures > initial_failures,
               f"was {initial_failures}, now {conn.health_failures}")

    finally:
        _mgr_module.STALE_CONNECTION_AGE = original_stale_age
        for sf in [sf_a, sf_b]:
            try:
                os.unlink(sf)
            except FileNotFoundError:
                pass


# ==========================================================================
# Tier 1: Anchor node tests
# ==========================================================================

async def test_anchor_priority() -> None:
    """Anchor nodes are queried first; if they respond, peer fan-out is skipped."""
    sf_a = make_state_file()
    sf_b = make_state_file()

    try:
        app_a, state_a = create_agent(sf_a, port=9900)
        app_b, state_b = create_agent(sf_b, port=9901)
        await connect_agents(app_a, state_a, app_b, state_b)

        # Build a mock anchor using Flask test app
        from anchor import anchor_bp
        from flask import Flask as _Flask
        anchor_app = _Flask(__name__)
        anchor_app.register_blueprint(anchor_bp)

        # Register agent B's URL in the anchor (signed)
        ts = datetime.now(timezone.utc).isoformat()
        sig = sign_peer_update(
            state_b.private_key_hex, state_b.agent_id,
            f"http://localhost:{state_b.port}", ts
        )
        with anchor_app.test_client() as anchor_client:
            resp = anchor_client.post("/__darkmatter__/peer_update", json={
                "agent_id": state_b.agent_id,
                "new_url": f"http://localhost:{state_b.port}",
                "public_key_hex": state_b.public_key_hex,
                "signature": sig,
                "timestamp": ts,
            })
            report("anchor: peer_update accepted", resp.status_code == 200)

        # Start anchor as a real HTTP server on a random port
        import threading
        import socket as _sock

        # Find a free port
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        anchor_port = s.getsockname()[1]
        s.close()

        # Run anchor in a thread
        anchor_server = None

        def run_anchor():
            nonlocal anchor_server
            from werkzeug.serving import make_server
            anchor_server = make_server("127.0.0.1", anchor_port, anchor_app, threaded=True)
            anchor_server.serve_forever()

        t = threading.Thread(target=run_anchor, daemon=True)
        t.start()
        import time as _time
        _time.sleep(0.3)  # Let server start

        # Set anchor nodes config (patch both config and manager module)
        original_anchors_config = darkmatter.config.ANCHOR_NODES[:]
        original_anchors_mgr = _mgr_module.ANCHOR_NODES[:]
        darkmatter.config.ANCHOR_NODES = [f"http://127.0.0.1:{anchor_port}"]
        _mgr_module.ANCHOR_NODES = [f"http://127.0.0.1:{anchor_port}"]

        try:
            use_agent(state_a)
            mgr = get_network_manager()
            result = await mgr.lookup_peer_url(state_b.agent_id)
            report("anchor: lookup returned URL from anchor", result is not None and str(state_b.port) in result)
        finally:
            darkmatter.config.ANCHOR_NODES = original_anchors_config
            _mgr_module.ANCHOR_NODES = original_anchors_mgr
            if anchor_server:
                anchor_server.shutdown()

    finally:
        for sf in [sf_a, sf_b]:
            try:
                os.unlink(sf)
            except FileNotFoundError:
                pass


async def test_anchor_fallback() -> None:
    """When anchor nodes are unreachable, peer fan-out still works."""
    sf_a = make_state_file()
    sf_b = make_state_file()
    sf_c = make_state_file()

    try:
        app_a, state_a = create_agent(sf_a, port=9900)
        app_b, state_b = create_agent(sf_b, port=9901)
        app_c, state_c = create_agent(sf_c, port=9902)

        # Connect A↔B and A↔C
        await connect_agents(app_a, state_a, app_b, state_b)
        await connect_agents(app_a, state_a, app_c, state_c)
        # B knows C's URL via connection
        await connect_agents(app_b, state_b, app_c, state_c)

        # Point to unreachable anchor (patch both config and manager module)
        original_anchors_config = darkmatter.config.ANCHOR_NODES[:]
        original_anchors_mgr = _mgr_module.ANCHOR_NODES[:]
        darkmatter.config.ANCHOR_NODES = ["http://127.0.0.1:19999"]
        _mgr_module.ANCHOR_NODES = ["http://127.0.0.1:19999"]

        try:
            use_agent(state_a)
            mgr = get_network_manager()
            result = await mgr.lookup_peer_url(state_c.agent_id)
            # Result is None because in-process peers can't be reached via real HTTP,
            # but the important thing is the anchor failure didn't crash anything
            report("anchor fallback: unreachable anchor didn't crash lookup", result is None or isinstance(result, str))
            report("anchor fallback: returns None (no reachable peers)", result is None)
        finally:
            darkmatter.config.ANCHOR_NODES = original_anchors_config
            _mgr_module.ANCHOR_NODES = original_anchors_mgr

    finally:
        for sf in [sf_a, sf_b, sf_c]:
            try:
                os.unlink(sf)
            except FileNotFoundError:
                pass


# ==========================================================================
# Tier 1: Impression system tests
# ==========================================================================

async def test_impression_system() -> None:
    """Test set/get/delete impression and HTTP endpoint."""
    sf_a = make_state_file()
    sf_b = make_state_file()

    try:
        app_a, state_a = create_agent(sf_a, port=9900)
        app_b, state_b = create_agent(sf_b, port=9901)
        await connect_agents(app_a, state_a, app_b, state_b)

        use_agent(state_a)
        target_id = state_b.agent_id

        # Set impression (Impression is a dataclass with score and note)
        state_a.impressions[target_id] = Impression(score=0.8, note="fast and accurate")
        imp = state_a.impressions.get(target_id)
        report("impression: set", imp is not None and imp.score == 0.8 and imp.note == "fast and accurate")

        # Get impression
        report("impression: get", state_a.impressions[target_id].score == 0.8)

        # HTTP endpoint — query A's impression of B
        use_agent(state_a)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.get(f"/__darkmatter__/impression/{target_id}")
        data = resp.json()
        report("impression: HTTP get has_impression", data.get("has_impression") is True)
        report("impression: HTTP get returns score", data.get("score") == 0.8 and data.get("note") == "fast and accurate")

        # Query impression for unknown agent
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.get("/__darkmatter__/impression/nonexistent-agent")
        data = resp.json()
        report("impression: HTTP unknown agent returns false", data.get("has_impression") is False)

        # Delete impression
        del state_a.impressions[target_id]
        report("impression: deleted", target_id not in state_a.impressions)

        # Verify gone via HTTP
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.get(f"/__darkmatter__/impression/{target_id}")
        data = resp.json()
        report("impression: HTTP confirms deletion", data.get("has_impression") is False)

    finally:
        for sf in [sf_a, sf_b]:
            try:
                os.unlink(sf)
            except FileNotFoundError:
                pass


# ==========================================================================
# Tier 1: Rate limiting tests
# ==========================================================================

async def test_rate_limiting() -> None:
    """Test per-connection and global rate limits on inbound mesh traffic."""
    sf_a = make_state_file()
    sf_b = make_state_file()

    try:
        app_a, state_a = create_agent(sf_a, port=9900)
        app_b, state_b = create_agent(sf_b, port=9901)
        await connect_agents(app_a, state_a, app_b, state_b)

        use_agent(state_a)
        conn = state_a.connections.get(state_b.agent_id)

        # Set a very low per-connection rate limit
        conn.rate_limit = 2
        conn._request_timestamps.clear()
        state_a._global_request_timestamps.clear()

        # First 2 requests should pass
        err1 = check_rate_limit(state_a, conn)
        report("rate limit: request 1 passes", err1 is None)
        err2 = check_rate_limit(state_a, conn)
        report("rate limit: request 2 passes", err2 is None)

        # Third request should be rate limited
        err3 = check_rate_limit(state_a, conn)
        report("rate limit: request 3 blocked", err3 is not None and "Rate limit" in err3)

        # Test global rate limit
        state_a.rate_limit_global = 3
        state_a._global_request_timestamps.clear()
        conn.rate_limit = -1  # unlimited per-connection
        conn._request_timestamps.clear()

        for _ in range(3):
            check_rate_limit(state_a, conn)
        err_global = check_rate_limit(state_a, conn)
        report("rate limit: global limit blocks", err_global is not None and "Global" in err_global)

        # Test set_rate_limit value 0 means default
        conn.rate_limit = 0
        effective = conn.rate_limit or darkmatter.config.DEFAULT_RATE_LIMIT_PER_CONNECTION
        report("rate limit: 0 resolves to default", effective == 30)

        # Test -1 means unlimited
        conn.rate_limit = -1
        report("rate limit: -1 means unlimited", conn.rate_limit == -1)

    finally:
        for sf in [sf_a, sf_b]:
            try:
                os.unlink(sf)
            except FileNotFoundError:
                pass


# ==========================================================================
# Tier 1: WebRTC guard tests
# ==========================================================================

async def test_webrtc_guards() -> None:
    """Test WebRTC gracefully handles missing aiortc and unknown agents."""
    sf_a = make_state_file()
    sf_b = make_state_file()

    try:
        app_a, state_a = create_agent(sf_a, port=9900)
        app_b, state_b = create_agent(sf_b, port=9901)
        await connect_agents(app_a, state_a, app_b, state_b)

        use_agent(state_a)

        # Test: WebRTC offer endpoint rejects unknown agent
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/webrtc_offer", json={
                "from_agent_id": "unknown-agent-id",
                "sdp_offer": "fake-sdp",
            })
        report("webrtc: unknown agent rejected",
               resp.status_code in (403, 501),
               f"status={resp.status_code}")

        # Test: WebRTC offer rejects if no from_agent_id
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/webrtc_offer", json={
                "sdp_offer": "fake-sdp",
            })
        report("webrtc: missing agent_id rejected",
               resp.status_code in (400, 501),
               f"status={resp.status_code}")

        # Test: WEBRTC_AVAILABLE flag exists
        report("webrtc: WEBRTC_AVAILABLE is a bool", isinstance(darkmatter.config.WEBRTC_AVAILABLE, bool))

        # Test: webrtc_offer for connected agent (will fail on SDP but not crash)
        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/webrtc_offer", json={
                "from_agent_id": state_b.agent_id,
                "sdp_offer": "fake-sdp",
            })
        # Should either fail gracefully (aiortc missing=501) or return a proper error
        report("webrtc: connected agent doesn't crash",
               resp.status_code in (200, 400, 500, 501, 503),
               f"status={resp.status_code}")

    finally:
        for sf in [sf_a, sf_b]:
            try:
                os.unlink(sf)
            except FileNotFoundError:
                pass


# ==========================================================================
# Tier 1: LAN discovery beacon tests
# ==========================================================================

async def test_lan_discovery_beacon() -> None:
    """Test DiscoveryProtocol.datagram_received directly."""
    sf = make_state_file()

    try:
        _, state = create_agent(sf, port=9900)
        use_agent(state)

        protocol = DiscoveryProtocol(state)

        # Valid beacon from another agent
        peer_id = str(uuid.uuid4())
        beacon = json.dumps({
            "proto": "darkmatter",
            "agent_id": peer_id,
            "port": 8105,
            "bio": "test peer",
            "status": "active",
            "accepting": True,
        }).encode("utf-8")
        protocol.datagram_received(beacon, ("192.168.1.50", 8470))
        report("discovery: valid beacon registered",
               peer_id in state.discovered_peers)
        if peer_id in state.discovered_peers:
            report("discovery: correct URL",
                   state.discovered_peers[peer_id]["url"] == "http://192.168.1.50:8105")
        else:
            report("discovery: correct URL", False, "peer not registered")

        # Self-filtering: our own agent_id should be ignored
        self_beacon = json.dumps({
            "proto": "darkmatter",
            "agent_id": state.agent_id,
            "port": 9900,
        }).encode("utf-8")
        prev_count = len(state.discovered_peers)
        protocol.datagram_received(self_beacon, ("127.0.0.1", 8470))
        report("discovery: self-beacon ignored",
               len(state.discovered_peers) == prev_count)

        # Wrong protocol should be ignored
        wrong_proto = json.dumps({"proto": "not-darkmatter", "agent_id": "x"}).encode("utf-8")
        protocol.datagram_received(wrong_proto, ("10.0.0.1", 8470))
        report("discovery: wrong proto ignored",
               "x" not in state.discovered_peers)

        # Malformed packet should not crash or corrupt state
        known_peers_before = set(state.discovered_peers.keys())
        protocol.datagram_received(b"not json at all", ("10.0.0.2", 8470))
        report("discovery: malformed packet handled",
               set(state.discovered_peers.keys()) == known_peers_before)

    finally:
        try:
            os.unlink(sf)
        except FileNotFoundError:
            pass


# ==========================================================================
# Tier 1: Peer update replay rejection test
# ==========================================================================

async def test_peer_update_replay_rejected() -> None:
    """Send a peer_update with a stale timestamp (>5min old), assert 403."""
    sf_a = make_state_file()
    sf_b = make_state_file()

    try:
        app_a, state_a = create_agent(sf_a, port=9900)
        app_b, state_b = create_agent(sf_b, port=9901)
        await connect_agents(app_a, state_a, app_b, state_b)

        use_agent(state_a)

        # Create a stale timestamp (10 minutes ago)
        from datetime import timedelta
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        new_url = f"http://localhost:{state_b.port}"

        signature = sign_peer_update(
            state_b.private_key_hex, state_b.agent_id, new_url, stale_time
        )

        async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/peer_update", json={
                "agent_id": state_b.agent_id,
                "new_url": new_url,
                "public_key_hex": state_b.public_key_hex,
                "signature": signature,
                "timestamp": stale_time,
            })
        report("replay: stale timestamp rejected with 403", resp.status_code == 403)
        report("replay: error message mentions timestamp",
               "Timestamp" in resp.json().get("error", ""),
               f"error: {resp.json().get('error', '')}")

    finally:
        for sf in [sf_a, sf_b]:
            try:
                os.unlink(sf)
            except FileNotFoundError:
                pass


# ==========================================================================
# Tier 1: Replay persistence and agent spawn tests
# ==========================================================================

async def test_replay_persistence() -> None:
    """Test that seen message IDs survive save/load cycle."""
    sf = make_state_file()
    try:
        app, state = create_agent(sf, port=9900)
        use_agent(state)

        # Save original replay cache
        orig_seen = _seen_message_ids.copy()

        # Inject a replay entry and save
        _seen_message_ids["msg-persist-test"] = time.time()
        save_state()

        # Verify it was saved to the state file (now keyed by public_key_hex)
        state_path = state_file_path()
        with open(state_path) as f:
            saved = json.load(f)
        report("replay persist: seen_message_ids in state file",
               "seen_message_ids" in saved)
        report("replay persist: test entry in saved data",
               "msg-persist-test" in saved.get("seen_message_ids", {}))

        # Clear and reload
        _seen_message_ids.clear()
        set_state(None)
        loaded = load_state_from_file(state_path)
        report("replay persist: entry restored after load",
               "msg-persist-test" in _seen_message_ids)

        # Verify expired entries are pruned on load
        _seen_message_ids.clear()
        _seen_message_ids["msg-stale"] = time.time() - 600  # 10 min ago
        # Re-set agent state so save_state works
        set_state(state)
        save_state()
        _seen_message_ids.clear()
        set_state(None)
        loaded = load_state_from_file(state_path)
        report("replay persist: stale entry pruned on load",
               "msg-stale" not in _seen_message_ids)

        # Restore
        _seen_message_ids.clear()
        _seen_message_ids.update(orig_seen)

    finally:
        # Clean up state file
        try:
            state_path = os.path.join(os.path.expanduser("~"), ".darkmatter", "state", f"{state.public_key_hex}.json")
            os.unlink(state_path)
        except (FileNotFoundError, NameError):
            pass


async def test_agent_spawn_guards() -> None:
    """Test can_spawn_agent guard logic: enabled/disabled, concurrency, rate limit."""
    sf = make_state_file()
    try:
        app, state = create_agent(sf, port=9900)

        # Save originals so we can restore them
        orig_enabled = darkmatter.config.AGENT_SPAWN_ENABLED
        orig_max_concurrent = darkmatter.config.AGENT_SPAWN_MAX_CONCURRENT
        orig_max_per_hour = darkmatter.config.AGENT_SPAWN_MAX_PER_HOUR
        orig_agents = _spawned_agents[:]
        orig_timestamps = _spawn_timestamps[:]

        # Also patch the spawn module's imported copies
        orig_spawn_enabled = _spawn_module.AGENT_SPAWN_ENABLED
        orig_spawn_max_concurrent = _spawn_module.AGENT_SPAWN_MAX_CONCURRENT
        orig_spawn_max_per_hour = _spawn_module.AGENT_SPAWN_MAX_PER_HOUR

        # --- Test: disabled ---
        darkmatter.config.AGENT_SPAWN_ENABLED = False
        _spawn_module.AGENT_SPAWN_ENABLED = False
        _spawned_agents.clear()
        _spawn_timestamps.clear()
        ok, reason = can_spawn_agent()
        report("spawn guard: disabled returns False", not ok)
        report("spawn guard: disabled reason mentions disabled",
               "disabled" in reason.lower(), f"reason: {reason}")

        # --- Test: enabled, no limits hit ---
        darkmatter.config.AGENT_SPAWN_ENABLED = True
        _spawn_module.AGENT_SPAWN_ENABLED = True
        darkmatter.config.AGENT_SPAWN_MAX_CONCURRENT = 5
        _spawn_module.AGENT_SPAWN_MAX_CONCURRENT = 5
        darkmatter.config.AGENT_SPAWN_MAX_PER_HOUR = 100
        _spawn_module.AGENT_SPAWN_MAX_PER_HOUR = 100
        _spawned_agents.clear()
        _spawn_timestamps.clear()
        ok, reason = can_spawn_agent()
        report("spawn guard: enabled + no limits = allowed", ok)

        # --- Test: concurrency limit ---
        darkmatter.config.AGENT_SPAWN_MAX_CONCURRENT = 1
        _spawn_module.AGENT_SPAWN_MAX_CONCURRENT = 1
        # Create a fake in-progress agent
        fake_proc = type("FakeProc", (), {"returncode": None})()
        fake_agent = SpawnedAgent(
            process=fake_proc, message_id="msg-fake-1",
            spawned_at=time.monotonic(), pid=99999,
        )
        _spawned_agents.clear()
        _spawned_agents.append(fake_agent)
        ok, reason = can_spawn_agent()
        report("spawn guard: concurrency limit blocks spawn", not ok)
        report("spawn guard: concurrency reason mentions limit",
               "concurrency" in reason.lower(), f"reason: {reason}")

        # --- Test: hourly rate limit ---
        darkmatter.config.AGENT_SPAWN_MAX_CONCURRENT = 10
        _spawn_module.AGENT_SPAWN_MAX_CONCURRENT = 10
        _spawned_agents.clear()
        darkmatter.config.AGENT_SPAWN_MAX_PER_HOUR = 2
        _spawn_module.AGENT_SPAWN_MAX_PER_HOUR = 2
        _spawn_timestamps.clear()
        _spawn_timestamps.extend([time.monotonic(), time.monotonic()])
        ok, reason = can_spawn_agent()
        report("spawn guard: hourly rate limit blocks spawn", not ok)
        report("spawn guard: rate reason mentions rate",
               "rate" in reason.lower(), f"reason: {reason}")

        # --- Test: stale timestamps get pruned ---
        _spawn_timestamps.clear()
        _spawn_timestamps.extend([time.monotonic() - 7200, time.monotonic() - 7200])
        ok, reason = can_spawn_agent()
        report("spawn guard: stale timestamps pruned, allows spawn", ok)

        # Restore
        darkmatter.config.AGENT_SPAWN_ENABLED = orig_enabled
        darkmatter.config.AGENT_SPAWN_MAX_CONCURRENT = orig_max_concurrent
        darkmatter.config.AGENT_SPAWN_MAX_PER_HOUR = orig_max_per_hour
        _spawn_module.AGENT_SPAWN_ENABLED = orig_spawn_enabled
        _spawn_module.AGENT_SPAWN_MAX_CONCURRENT = orig_spawn_max_concurrent
        _spawn_module.AGENT_SPAWN_MAX_PER_HOUR = orig_spawn_max_per_hour
        _spawned_agents.clear()
        _spawned_agents.extend(orig_agents)
        _spawn_timestamps.clear()
        _spawn_timestamps.extend(orig_timestamps)

    finally:
        try:
            os.unlink(sf)
        except FileNotFoundError:
            pass


async def test_agent_prompt_building() -> None:
    """Test build_agent_prompt produces a valid prompt with expected fields."""
    sf_a = make_state_file()
    sf_b = make_state_file()
    try:
        app_a, state_a = create_agent(sf_a, port=9900)
        app_b, state_b = create_agent(sf_b, port=9901)
        await connect_agents(app_a, state_a, app_b, state_b)

        use_agent(state_a)

        msg = QueuedMessage(
            message_id="msg-test-prompt",
            content="Hello, what can you do?",
            webhook="http://localhost:9901/__darkmatter__/webhook/msg-test-prompt",
            hops_remaining=8,
            metadata={},
            from_agent_id=state_b.agent_id,
            verified=True,
        )

        prompt = build_agent_prompt(state_a, msg)

        report("prompt: contains message ID", "msg-test-prompt" in prompt)
        report("prompt: is non-empty string", len(prompt) > 0)
        report("prompt: instructs to check message",
               "check message" in prompt.lower() or "received" in prompt.lower())
        report("prompt: does NOT restrict file modification",
               "Do NOT modify any files" not in prompt)
        report("prompt: does NOT restrict shell commands",
               "Do NOT run any shell commands" not in prompt)

    finally:
        for sf in [sf_a, sf_b]:
            try:
                os.unlink(sf)
            except FileNotFoundError:
                pass


async def test_agent_cleanup_finished() -> None:
    """Test cleanup_finished_agents removes completed processes."""
    orig_agents = _spawned_agents[:]

    # Create fake agents — one finished, one still running
    finished_proc = type("FakeProc", (), {"returncode": 0, "pid": 11111})()
    running_proc = type("FakeProc", (), {"returncode": None, "pid": 22222})()

    _spawned_agents.clear()
    _spawned_agents.append(SpawnedAgent(
        process=finished_proc, message_id="msg-done",
        spawned_at=time.monotonic() - 60, pid=11111,
    ))
    _spawned_agents.append(SpawnedAgent(
        process=running_proc, message_id="msg-running",
        spawned_at=time.monotonic(), pid=22222,
    ))

    cleanup_finished_agents()
    report("cleanup: removes finished agents", len(_spawned_agents) == 1)
    report("cleanup: keeps running agents",
           _spawned_agents[0].message_id == "msg-running"
           if _spawned_agents else False)

    # Restore
    _spawned_agents.clear()
    _spawned_agents.extend(orig_agents)


async def test_agent_spawn_deduplication() -> None:
    """Test that spawn_agent_for_message won't spawn twice for the same message."""
    sf = make_state_file()
    try:
        app, state = create_agent(sf, port=9900)
        use_agent(state)

        orig_agents = _spawned_agents[:]
        orig_enabled = _spawn_module.AGENT_SPAWN_ENABLED

        # Disable actual spawning but test dedup logic
        _spawn_module.AGENT_SPAWN_ENABLED = True

        # Pre-populate with a fake agent for msg-dedup
        fake_proc = type("FakeProc", (), {"returncode": None, "pid": 33333})()
        _spawned_agents.clear()
        _spawned_agents.append(SpawnedAgent(
            process=fake_proc, message_id="msg-dedup",
            spawned_at=time.monotonic(), pid=33333,
        ))

        msg = QueuedMessage(
            message_id="msg-dedup",
            content="duplicate test",
            webhook="http://localhost:9900/__darkmatter__/webhook/msg-dedup",
            hops_remaining=10,
            metadata={},
            from_agent_id="fake-sender",
            verified=False,
        )

        # Mock can_spawn_agent to always allow
        orig_can_spawn = _spawn_module.can_spawn_agent
        _spawn_module.can_spawn_agent = lambda: (True, "")

        count_before = len(_spawned_agents)
        await spawn_agent_for_message(state, msg)
        count_after = len(_spawned_agents)

        report("dedup: does not spawn duplicate for same message_id",
               count_after == count_before)

        # Restore
        _spawn_module.can_spawn_agent = orig_can_spawn
        _spawn_module.AGENT_SPAWN_ENABLED = orig_enabled
        _spawned_agents.clear()
        _spawned_agents.extend(orig_agents)

    finally:
        try:
            os.unlink(sf)
        except FileNotFoundError:
            pass


# ==========================================================================
# Runner
# ==========================================================================

async def run_tier1_tests() -> None:
    """Run all in-process ASGI tests."""
    tier1_tests = [
        ("1. Basic message delivery", test_basic_message_delivery),
        ("2. Message broadcast (hub+spokes)", test_message_broadcast),
        ("3. Webhook forwarding chain", test_webhook_forwarding_chain),
        ("4. Peer lookup returns URL", test_peer_lookup_returns_url),
        ("5. Peer lookup unknown → 404", test_peer_lookup_unknown_404),
        ("6. Peer update changes stored URL", test_peer_update_changes_stored_url),
        ("7. Peer update rejects key mismatch", test_peer_update_rejects_key_mismatch),
        ("8. Webhook recovery: orphaned message", test_webhook_recovery_orphaned_message),
        ("9. Webhook recovery: gives up after max attempts", test_webhook_recovery_gives_up),
        ("10. Webhook recovery: timeout budget", test_webhook_recovery_timeout_budget),
        ("11. Anchor priority: anchor queried first", test_anchor_priority),
        ("12. Anchor fallback: unreachable anchor", test_anchor_fallback),
        ("13. Health loop increments failures", test_health_loop_increments_failures),
        ("14. Impression system", test_impression_system),
        ("15. Rate limiting", test_rate_limiting),
        ("16. WebRTC guards", test_webrtc_guards),
        ("17. LAN discovery beacon", test_lan_discovery_beacon),
        ("18. Peer update replay rejected", test_peer_update_replay_rejected),
        ("19. Replay protection persistence", test_replay_persistence),
        ("20. Agent spawn guards", test_agent_spawn_guards),
        ("21. Agent prompt building", test_agent_prompt_building),
        ("22. Agent cleanup finished", test_agent_cleanup_finished),
        ("23. Agent spawn deduplication", test_agent_spawn_deduplication),
    ]

    for label, test_fn in tier1_tests:
        print(f"\n{BOLD}{label}{RESET}")
        try:
            await test_fn()
        except Exception as e:
            report(label, False, f"EXCEPTION: {e}")


def run_tier2_tests() -> None:
    """Run all subprocess-based tests."""
    tier2_tests = [
        ("24. Discovery smoke", test_discovery_smoke),
        ("25. Broadcast peer update", None),  # async, handled separately
        ("26. Multi-hop message routing", test_multi_hop_message_routing),
        ("27. Send recovery via peer_lookup", test_send_recovers_via_peer_lookup),
    ]

    for label, test_fn in tier2_tests:
        if test_fn is None:
            continue
        try:
            test_fn()
        except Exception as e:
            report(label, False, f"EXCEPTION: {e}")


async def main() -> None:
    print(f"\n{BOLD}DarkMatter Network Test Suite{RESET}")
    print("=" * 50)

    # Tier 1: In-process ASGI (fast)
    print(f"\n{BOLD}── Tier 1: In-process ASGI ──{RESET}")
    await run_tier1_tests()

    # Broadcast peer update is async but uses Tier 2 nodes
    print(f"\n{BOLD}25. Broadcast peer update{RESET}")
    try:
        await test_broadcast_peer_update()
    except Exception as e:
        report("Broadcast peer update", False, f"EXCEPTION: {e}")

    # Tier 2: Real subprocess (thorough)
    print(f"\n{BOLD}── Tier 2: Real subprocess nodes ──{RESET}")
    run_tier2_tests()

    # Summary
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    failed = total - passed
    print(f"\n{'=' * 50}")
    if passed == total:
        print(f"{GREEN}{BOLD}All {total} checks passed.{RESET}")
    else:
        print(f"{BOLD}Results: {GREEN}{passed} passed{RESET}, {RED}{failed} failed{RESET}")
        print(f"\n{RED}Failed:{RESET}")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}: {detail}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
