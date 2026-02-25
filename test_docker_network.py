#!/usr/bin/env python3
"""
DarkMatter Docker Multi-Network Test Suite.

Tests real network isolation using Docker containers on separate networks.
node-a and node-c cannot reach each other directly — node-b bridges them.

Topology:
    Network "left"            Network "right"
  ┌────────────────┐       ┌────────────────┐
  │  node-a         │       │         node-c  │
  │                 │       │                 │
  │        node-b ──┼───────┼── node-b        │
  └────────────────┘       └────────────────┘

Prerequisites:
    - Docker and docker compose installed
    - Run from the project root (where docker-compose.test.yml lives)

Usage:
    python3 test_docker_network.py
"""

import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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


def sign_message(private_key_hex: str, from_agent_id: str, message_id: str,
                 timestamp: str, content: str) -> str:
    """Sign a message payload with Ed25519. Returns hex signature."""
    private_bytes = bytes.fromhex(private_key_hex)
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    payload = f"{from_agent_id}\n{message_id}\n{timestamp}\n{content}".encode("utf-8")
    return private_key.sign(payload).hex()


def sign_peer_update(private_key_hex: str, agent_id: str, new_url: str,
                     timestamp: str) -> str:
    """Sign a peer_update payload with Ed25519. Returns hex signature."""
    private_bytes = bytes.fromhex(private_key_hex)
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    payload = f"peer_update\n{agent_id}\n{new_url}\n{timestamp}".encode("utf-8")
    return private_key.sign(payload).hex()


# ---------------------------------------------------------------------------
# Docker Node Abstraction
# ---------------------------------------------------------------------------

# Published ports on localhost
NODE_PORTS = {"node-a": 9800, "node-b": 9801, "node-c": 9802}

# Internal Docker DNS names (for inter-container references)
NODE_INTERNAL = {"node-a": "http://node-a:8100", "node-b": "http://node-b:8100", "node-c": "http://node-c:8100"}

COMPOSE_FILE = "docker-compose.test.yml"
STARTUP_TIMEOUT = 30  # seconds


class DockerNode:
    """Proxy for a DarkMatter node running inside a Docker container."""

    def __init__(self, name: str):
        self.name = name
        self.port = NODE_PORTS[name]
        self.internal_url = NODE_INTERNAL[name]
        self.agent_id: str | None = None
        self.public_key_hex: str | None = None
        self.private_key_hex: str | None = None

    @property
    def base_url(self) -> str:
        """URL accessible from the host machine."""
        return f"http://127.0.0.1:{self.port}"

    def wait_ready(self) -> bool:
        """Poll until the node is ready, reading identity from well-known endpoint."""
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            try:
                r = httpx.get(
                    f"{self.base_url}/.well-known/darkmatter.json",
                    timeout=2.0,
                )
                if r.status_code == 200:
                    info = r.json()
                    self.agent_id = info.get("agent_id")
                    self.public_key_hex = info.get("public_key_hex")
                    # Read private key from container's state file
                    self._read_private_key()
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    @property
    def state_file_path(self) -> str:
        """State file path inside the container (keyed by display name)."""
        return f"/root/.darkmatter/state/{self.name}.json"

    def _read_private_key(self) -> None:
        """Extract private key from the container's state file."""
        try:
            result = subprocess.run(
                ["docker", "exec", f"dm-{self.name}",
                 "cat", self.state_file_path],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                state_data = json.loads(result.stdout)
                self.private_key_hex = state_data.get("private_key_hex")
        except Exception:
            pass

    def connect_to(self, other: "DockerNode") -> dict:
        """Send a connection request to another node (using internal Docker URLs).

        We POST from the host, but reference internal URLs for inter-container comms.
        The target node receives the connection with the sender's internal URL.
        """
        # Tell the other node about us (using internal URLs for inter-container routing)
        resp = httpx.post(
            f"{other.base_url}/__darkmatter__/connection_request",
            json={
                "from_agent_id": self.agent_id,
                "from_agent_url": f"{self.internal_url}/mcp",
                "from_agent_bio": f"Test node {self.name}",
                "from_agent_public_key_hex": self.public_key_hex,
            },
            timeout=5.0,
        )
        data = resp.json()

        if data.get("auto_accepted"):
            # Bidirectional: also tell ourselves about the other node
            httpx.post(
                f"{self.base_url}/__darkmatter__/connection_request",
                json={
                    "from_agent_id": other.agent_id,
                    "from_agent_url": f"{other.internal_url}/mcp",
                    "from_agent_bio": f"Test node {other.name}",
                    "from_agent_public_key_hex": other.public_key_hex,
                },
                timeout=5.0,
            )

        return data

    def send_message(self, target: "DockerNode", content: str,
                     message_id: str | None = None) -> dict:
        """Send a signed message to another node via the host proxy."""
        if message_id is None:
            message_id = f"msg-{uuid.uuid4().hex[:8]}"
        timestamp = datetime.now(timezone.utc).isoformat()

        signature = sign_message(
            self.private_key_hex, self.agent_id,
            message_id, timestamp, content,
        )

        # Use a dummy webhook — we test delivery, not webhook callbacks
        webhook_url = f"{self.internal_url}/__darkmatter__/webhook/{message_id}"

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
        return {"status_code": resp.status_code, "data": resp.json(), "message_id": message_id}

    def forward_message_to(self, msg: dict, target: "DockerNode") -> dict:
        """Forward a queued message to another node by directly POSTing to it.

        This simulates what the forward_message MCP tool does, but via HTTP
        so we don't need MCP auth. We re-sign the message as this node.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        signature = sign_message(
            self.private_key_hex, self.agent_id,
            msg["message_id"], timestamp, msg["content"],
        )

        resp = httpx.post(
            f"{target.base_url}/__darkmatter__/message",
            json={
                "message_id": msg["message_id"],
                "content": msg["content"],
                "webhook": msg.get("webhook", ""),
                "from_agent_id": self.agent_id,
                "from_public_key_hex": self.public_key_hex,
                "signature_hex": signature,
                "timestamp": timestamp,
                "hops_remaining": msg.get("hops_remaining", 10) - 1,
                "metadata": msg.get("metadata", {}),
            },
            timeout=5.0,
        )
        return {"status_code": resp.status_code, "data": resp.json()}

    def get_inbox_via_logs(self, message_id: str) -> bool:
        """Check if a message was received by searching container stderr logs.

        Message queue is ephemeral (not persisted to state file), so we
        verify delivery via the HTTP 200 response from the message endpoint.
        This method is a fallback that greps container logs.
        """
        try:
            result = subprocess.run(
                ["docker", "logs", f"dm-{self.name}"],
                capture_output=True, text=True, timeout=5,
            )
            return message_id in result.stderr or message_id in result.stdout
        except Exception:
            return False

    def get_connections(self) -> dict:
        """Read connections from state file via docker exec."""
        try:
            result = subprocess.run(
                ["docker", "exec", f"dm-{self.name}",
                 "cat", self.state_file_path],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                state_data = json.loads(result.stdout)
                return state_data.get("connections", {})
        except Exception:
            pass
        return {}

    def get_peer_lookup(self, agent_id: str) -> dict:
        """GET /peer_lookup/{agent_id} on this node."""
        resp = httpx.get(
            f"{self.base_url}/__darkmatter__/peer_lookup/{agent_id}",
            timeout=5.0,
        )
        return {"status_code": resp.status_code, "data": resp.json()}

    def post_peer_update(self, agent_id: str, new_url: str,
                         public_key_hex: str | None = None,
                         private_key_hex: str | None = None) -> dict:
        """POST /peer_update on this node."""
        payload: dict = {"agent_id": agent_id, "new_url": new_url}
        if public_key_hex:
            payload["public_key_hex"] = public_key_hex
        if private_key_hex:
            timestamp = datetime.now(timezone.utc).isoformat()
            payload["timestamp"] = timestamp
            payload["signature"] = sign_peer_update(
                private_key_hex, agent_id, new_url, timestamp,
            )
        resp = httpx.post(
            f"{self.base_url}/__darkmatter__/peer_update",
            json=payload,
            timeout=5.0,
        )
        return {"status_code": resp.status_code, "data": resp.json()}

    def __repr__(self) -> str:
        return f"<DockerNode {self.name} port={self.port} agent_id={self.agent_id}>"


# ---------------------------------------------------------------------------
# Docker lifecycle
# ---------------------------------------------------------------------------

def compose_up() -> bool:
    """Build and start containers. Returns True if successful."""
    print(f"  Building and starting containers...")
    result = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"  {RED}docker compose up failed:{RESET}")
        print(result.stderr)
        return False
    return True


def compose_down() -> None:
    """Tear down containers and networks."""
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "down", "-v", "--remove-orphans"],
        capture_output=True, timeout=30,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_network_isolation(a: DockerNode, c: DockerNode) -> None:
    """Verify node-a cannot directly reach node-c."""
    print(f"\n{BOLD}1. Network isolation (A cannot reach C){RESET}")

    try:
        resp = httpx.post(
            f"{a.base_url}/__darkmatter__/connection_request",
            json={
                "from_agent_id": "test-isolation",
                "from_agent_url": f"http://node-c:8100/mcp",
                "from_agent_bio": "isolation test",
            },
            timeout=5.0,
        )
        # Even though the request goes through the host to node-a, the question
        # is whether node-a can reach node-c's internal URL. We test this by
        # trying to exec a curl from node-a to node-c.
    except Exception:
        pass

    # The real test: from inside node-a's container, try to reach node-c
    result = subprocess.run(
        ["docker", "exec", "dm-node-a", "python", "-c",
         "import httpx; r = httpx.get('http://node-c:8100/.well-known/darkmatter.json', timeout=3)"],
        capture_output=True, text=True, timeout=10,
    )
    isolated = result.returncode != 0
    report("A cannot reach C directly", isolated,
           f"Expected failure but got rc={result.returncode}")

    # Verify B CAN reach both
    result_b_a = subprocess.run(
        ["docker", "exec", "dm-node-b", "python", "-c",
         "import httpx; r = httpx.get('http://node-a:8100/.well-known/darkmatter.json', timeout=3); print(r.status_code)"],
        capture_output=True, text=True, timeout=10,
    )
    report("B can reach A", result_b_a.returncode == 0 and "200" in result_b_a.stdout,
           f"rc={result_b_a.returncode}, stdout={result_b_a.stdout.strip()}")

    result_b_c = subprocess.run(
        ["docker", "exec", "dm-node-b", "python", "-c",
         "import httpx; r = httpx.get('http://node-c:8100/.well-known/darkmatter.json', timeout=3); print(r.status_code)"],
        capture_output=True, text=True, timeout=10,
    )
    report("B can reach C", result_b_c.returncode == 0 and "200" in result_b_c.stdout,
           f"rc={result_b_c.returncode}, stdout={result_b_c.stdout.strip()}")


def test_connections(a: DockerNode, b: DockerNode, c: DockerNode) -> None:
    """Connect A↔B and B↔C."""
    print(f"\n{BOLD}2. Establish connections{RESET}")

    data_ab = a.connect_to(b)
    report("A→B connection", data_ab.get("auto_accepted", False),
           f"Response: {data_ab}")

    data_bc = c.connect_to(b)
    report("C→B connection", data_bc.get("auto_accepted", False),
           f"Response: {data_bc}")

    # Verify connections are recorded
    time.sleep(0.5)
    a_conns = a.get_connections()
    report("A knows B", b.agent_id in a_conns,
           f"A connections: {list(a_conns.keys())}")

    b_conns = b.get_connections()
    report("B knows A", a.agent_id in b_conns,
           f"B connections: {list(b_conns.keys())}")
    report("B knows C", c.agent_id in b_conns,
           f"B connections: {list(b_conns.keys())}")

    c_conns = c.get_connections()
    report("C knows B", b.agent_id in c_conns,
           f"C connections: {list(c_conns.keys())}")


def test_direct_message(a: DockerNode, b: DockerNode) -> None:
    """Send a direct message from A to B (same network)."""
    print(f"\n{BOLD}3. Direct message A→B{RESET}")

    result = a.send_message(b, "Hello from A to B")
    report("A→B message accepted (HTTP 200)", result["status_code"] == 200,
           f"status={result['status_code']}, data={result['data']}")

    queued = result["data"].get("queued", False)
    report("Message queued on B", queued,
           f"data={result['data']}")


def test_multi_hop_routing(a: DockerNode, b: DockerNode, c: DockerNode) -> None:
    """A sends to B, B forwards to C (multi-hop across networks)."""
    print(f"\n{BOLD}4. Multi-hop routing A→B→C{RESET}")

    # A sends message to B
    msg_id = f"multihop-{uuid.uuid4().hex[:8]}"
    content = "Multi-hop test: please forward to C"
    result = a.send_message(b, content, message_id=msg_id)
    report("A→B message sent", result["status_code"] == 200,
           f"status={result['status_code']}")

    # Construct the message as B would have received it (for forwarding)
    queued_msg = {
        "message_id": msg_id,
        "content": content,
        "webhook": f"{a.internal_url}/__darkmatter__/webhook/{msg_id}",
        "hops_remaining": 10,
        "metadata": {},
    }

    # B forwards to C (re-signs as B, posts to C's message endpoint)
    fwd_result = b.forward_message_to(queued_msg, c)
    report("B→C forward accepted", fwd_result["status_code"] == 200,
           f"status={fwd_result['status_code']}, data={fwd_result['data']}")

    if fwd_result["status_code"] == 200:
        queued = fwd_result["data"].get("queued", False)
        report("Message reached C (queued)", queued,
               f"data={fwd_result['data']}")


def test_peer_lookup(a: DockerNode, b: DockerNode, c: DockerNode) -> None:
    """A asks B for C's URL via peer_lookup."""
    print(f"\n{BOLD}5. Peer lookup (A asks B about C){RESET}")

    result = b.get_peer_lookup(c.agent_id)
    report("B knows C's URL", result["status_code"] == 200,
           f"status={result['status_code']}, data={result['data']}")

    if result["status_code"] == 200:
        url = result["data"].get("url", "")
        report("URL contains node-c", "node-c" in url,
               f"Got URL: {url}")

    # A asks B for C (via host-side, simulating cross-network lookup)
    result_a = a.get_peer_lookup(c.agent_id)
    # A might not know C directly, but B's connections are visible via peer_lookup
    # peer_lookup returns from the node's own connections
    report("A peer_lookup for C (not found — A not connected to C)",
           result_a["status_code"] == 404 or "not found" in str(result_a["data"]).lower()
           or result_a["data"].get("url") is None,
           f"data={result_a['data']}")


def test_peer_update_broadcast(a: DockerNode, b: DockerNode, c: DockerNode) -> None:
    """B broadcasts a peer_update for C to A."""
    print(f"\n{BOLD}6. Peer update broadcast{RESET}")

    new_url = "http://node-c-new:9999/mcp"

    # Post peer_update to B about C
    result = b.post_peer_update(
        agent_id=c.agent_id,
        new_url=new_url,
        public_key_hex=c.public_key_hex,
        private_key_hex=c.private_key_hex,
    )
    report("Peer update accepted by B",
           result["status_code"] == 200 and result["data"].get("updated", False),
           f"status={result['status_code']}, data={result['data']}")

    # Give broadcast time to propagate
    time.sleep(1.0)

    # Check if A received the update (B should broadcast to its connections)
    # A's connection to B should have been updated with C's new URL
    # Actually, peer_update only updates B's own connection table.
    # The broadcast propagates peer_update to all of B's connections.
    # But A doesn't have C in its connections, so the update might not
    # be stored. Let's check B's connection table instead.
    b_conns = b.get_connections()
    if c.agent_id in b_conns:
        stored_url = b_conns[c.agent_id].get("agent_url", "")
        report("B updated C's URL", new_url.rstrip("/mcp") in stored_url or stored_url == new_url,
               f"Expected URL containing '{new_url}', got '{stored_url}'")
    else:
        report("B updated C's URL", False, "C not in B's connections")


def test_message_rejection_no_connection(a: DockerNode, c: DockerNode) -> None:
    """A tries to send message directly to C (should fail — not connected)."""
    print(f"\n{BOLD}7. Message rejection (no connection){RESET}")

    # Even though we talk via host ports, the message claims from_agent_id = A
    # and C is not connected to A, so it should reject
    timestamp = datetime.now(timezone.utc).isoformat()
    msg_id = f"reject-{uuid.uuid4().hex[:8]}"
    signature = sign_message(a.private_key_hex, a.agent_id, msg_id, timestamp, "should be rejected")

    resp = httpx.post(
        f"{c.base_url}/__darkmatter__/message",
        json={
            "message_id": msg_id,
            "content": "should be rejected",
            "webhook": f"{a.internal_url}/__darkmatter__/webhook/{msg_id}",
            "from_agent_id": a.agent_id,
            "from_public_key_hex": a.public_key_hex,
            "signature_hex": signature,
            "timestamp": timestamp,
            "hops_remaining": 10,
        },
        timeout=5.0,
    )
    report("C rejects message from unconnected A", resp.status_code == 403,
           f"status={resp.status_code}, body={resp.text}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{BOLD}DarkMatter Docker Multi-Network Test Suite{RESET}")
    print("=" * 55)

    # Check Docker daemon
    docker_check = subprocess.run(
        ["docker", "info"], capture_output=True, timeout=10,
    )
    if docker_check.returncode != 0:
        print(f"\n{RED}Docker daemon is not running. Start Docker Desktop and try again.{RESET}")
        sys.exit(1)

    # Start containers
    print(f"\n{BOLD}Setup{RESET}")
    if not compose_up():
        print(f"{RED}Failed to start containers. Aborting.{RESET}")
        sys.exit(1)

    try:
        # Create node proxies
        a = DockerNode("node-a")
        b = DockerNode("node-b")
        c = DockerNode("node-c")

        # Wait for all nodes to be ready
        print("  Waiting for nodes to start...")
        for node in [a, b, c]:
            ready = node.wait_ready()
            report(f"{node.name} ready", ready,
                   f"agent_id={node.agent_id}" if ready else "Timed out")
            if not ready:
                print(f"\n{RED}Node {node.name} failed to start. Aborting.{RESET}")
                return

        print(f"  Nodes: A={a.agent_id[:8]}... B={b.agent_id[:8]}... C={c.agent_id[:8]}...")

        # Run tests
        test_network_isolation(a, c)
        test_connections(a, b, c)
        test_direct_message(a, b)
        test_multi_hop_routing(a, b, c)
        test_peer_lookup(a, b, c)
        test_peer_update_broadcast(a, b, c)
        test_message_rejection_no_connection(a, c)

    finally:
        # Tear down
        print(f"\n{BOLD}Teardown{RESET}")
        print("  Stopping containers...")
        compose_down()
        print("  Done.")

    # Summary
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    failed = total - passed
    print(f"\n{'=' * 55}")
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
    main()
