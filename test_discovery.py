#!/usr/bin/env python3
"""
End-to-end discovery tests.

Starts real DarkMatter server processes on localhost and verifies
they discover each other via the /.well-known/darkmatter.json endpoint.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PYTHON = sys.executable
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BASE_PORT = 9900
STARTUP_TIMEOUT = 15  # seconds to wait for a server to come up
DISCOVERY_WAIT = 5    # seconds to wait for discovery scan
PORT_RELEASE_WAIT = 1  # seconds to wait after stopping a node for port release

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


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

class DarkMatterNode:
    """Manages a real DarkMatter server process."""

    def __init__(self, port: int, *, display_name: str = ""):
        self.port = port
        self.display_name = display_name or f"node-{port}"
        self.state_file = tempfile.mktemp(suffix=".json", prefix=f"dm_{port}_")
        # Each node gets its own temp working directory so it generates a unique passport
        self.workdir = tempfile.mkdtemp(prefix=f"dm_{port}_")
        self.proc: subprocess.Popen | None = None
        self.agent_id: str | None = None

    def start(self) -> None:
        env = {
            **os.environ,
            "DARKMATTER_PORT": str(self.port),
            "DARKMATTER_HOST": "127.0.0.1",
            "DARKMATTER_DISPLAY_NAME": self.display_name,
            "DARKMATTER_STATE_FILE": self.state_file,
            "DARKMATTER_DISCOVERY": "true",
            "DARKMATTER_TRANSPORT": "http",
            # Override the scan range to cover our test ports
            "DARKMATTER_DISCOVERY_PORTS": f"{BASE_PORT}-{BASE_PORT + 10}",
            "PYTHONPATH": PROJECT_ROOT,
        }
        # Remove any stale tokens
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
        """Wait until the server responds to HTTP."""
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            # Check if process died
            if self.proc and self.proc.poll() is not None:
                stderr = self.proc.stderr.read().decode() if self.proc.stderr else ""
                print(f"      Process died (rc={self.proc.returncode}): {stderr[-300:]}")
                return False
            try:
                r = httpx.get(
                    f"http://127.0.0.1:{self.port}/.well-known/darkmatter.json",
                    timeout=1.0,
                )
                if r.status_code == 200:
                    info = r.json()
                    self.agent_id = info.get("agent_id")
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        # Timeout — dump stderr for diagnostics
        if self.proc and self.proc.stderr:
            try:
                self.proc.stderr.close()
            except Exception:
                pass
        return False

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if os.path.exists(self.state_file):
            os.unlink(self.state_file)
        import shutil
        if os.path.isdir(self.workdir):
            shutil.rmtree(self.workdir, ignore_errors=True)

    def __repr__(self) -> str:
        return f"<Node {self.display_name} port={self.port} pid={self.proc.pid if self.proc else None}>"


# ---------------------------------------------------------------------------
# Inline scan script template (uses darkmatter/ package)
# ---------------------------------------------------------------------------

def _scan_script(agent_id: str, port: int, port_range_start: int, port_range_end: int) -> str:
    """Generate an inline Python script that scans local ports using darkmatter."""
    return f"""
import asyncio, json, sys
sys.path.insert(0, "{PROJECT_ROOT}")

from darkmatter.models import AgentState, AgentStatus
from darkmatter.network.discovery import scan_local_ports
import darkmatter.config

darkmatter.config.DISCOVERY_LOCAL_PORTS = range({port_range_start}, {port_range_end})
import darkmatter.network.discovery as _disc
_disc.DISCOVERY_LOCAL_PORTS = range({port_range_start}, {port_range_end})

state = AgentState(
    agent_id="{agent_id}",
    bio="test",
    status=AgentStatus.ACTIVE,
    port={port},
)

async def main():
    await scan_local_ports(state)
    print(json.dumps({{
        k: v["url"] for k, v in state.discovered_peers.items()
    }}))

asyncio.run(main())
"""


def _scan_script_keys(agent_id: str, port: int, port_range_start: int, port_range_end: int) -> str:
    """Generate an inline Python script that scans and returns just peer IDs."""
    return f"""
import asyncio, json, sys
sys.path.insert(0, "{PROJECT_ROOT}")

from darkmatter.models import AgentState, AgentStatus
from darkmatter.network.discovery import scan_local_ports
import darkmatter.config

darkmatter.config.DISCOVERY_LOCAL_PORTS = range({port_range_start}, {port_range_end})
import darkmatter.network.discovery as _disc
_disc.DISCOVERY_LOCAL_PORTS = range({port_range_start}, {port_range_end})

state = AgentState(
    agent_id="{agent_id}",
    bio="test",
    status=AgentStatus.ACTIVE,
    port={port},
)

async def main():
    await scan_local_ports(state)
    print(json.dumps(list(state.discovered_peers.keys())))

asyncio.run(main())
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_well_known_endpoint() -> None:
    """A running server exposes /.well-known/darkmatter.json."""
    print(f"\n{BOLD}Test: well-known endpoint{RESET}")

    node = DarkMatterNode(BASE_PORT, display_name="wk-test")
    try:
        node.start()
        ready = node.wait_ready()
        report("server starts and responds", ready)
        if not ready:
            return

        r = httpx.get(f"http://127.0.0.1:{node.port}/.well-known/darkmatter.json", timeout=2.0)
        info = r.json()

        report("darkmatter: true", info.get("darkmatter") is True)
        report("has agent_id", bool(info.get("agent_id")))
        report("has bio", isinstance(info.get("bio"), str))
        report("status is active", info.get("status") == "active")
        report("accepting_connections", info.get("accepting_connections") is True)
        report("has mcp_url", "mcp" in info.get("mcp_url", ""))
    finally:
        node.stop()
        time.sleep(PORT_RELEASE_WAIT)


def test_two_nodes_discover_each_other() -> None:
    """Two nodes on different ports discover each other via HTTP scan."""
    print(f"\n{BOLD}Test: two nodes discover each other{RESET}")

    node_a = DarkMatterNode(BASE_PORT, display_name="alpha")
    node_b = DarkMatterNode(BASE_PORT + 1, display_name="beta")

    try:
        node_a.start()
        node_b.start()

        if not node_a.wait_ready():
            report("Node A started", False, "failed to start")
            return
        if not node_b.wait_ready():
            report("Node B started", False, "failed to start")
            return

        # Verify basic HTTP connectivity
        r = httpx.get(
            f"http://127.0.0.1:{node_a.port}/.well-known/darkmatter.json",
            timeout=2.0,
        )
        report("B can reach A via HTTP", r.status_code == 200)

        r = httpx.get(
            f"http://127.0.0.1:{node_b.port}/.well-known/darkmatter.json",
            timeout=2.0,
        )
        report("A can reach B via HTTP", r.status_code == 200)

        # Run a manual scan from A's perspective to find B
        result = subprocess.run(
            [PYTHON, "-c", _scan_script(node_a.agent_id, node_a.port, BASE_PORT, BASE_PORT + 10)],
            capture_output=True, text=True, timeout=10,
        )

        if result.returncode != 0:
            report("scan script ran", False, result.stderr[-200:])
            return

        peers = json.loads(result.stdout.strip())
        report("A's scan finds B", node_b.agent_id in peers,
               f"found: {peers}")
        if node_b.agent_id in peers:
            report("B's URL correct",
                   peers[node_b.agent_id] == f"http://127.0.0.1:{node_b.port}")
        report("A doesn't find itself", node_a.agent_id not in peers)

        # Same scan from B's perspective
        result = subprocess.run(
            [PYTHON, "-c", _scan_script(node_b.agent_id, node_b.port, BASE_PORT, BASE_PORT + 10)],
            capture_output=True, text=True, timeout=10,
        )

        peers = json.loads(result.stdout.strip())
        report("B's scan finds A", node_a.agent_id in peers,
               f"found: {peers}")

    finally:
        node_a.stop()
        node_b.stop()
        time.sleep(PORT_RELEASE_WAIT)


def test_three_nodes_nway() -> None:
    """Three nodes all discover each other."""
    print(f"\n{BOLD}Test: three-node N-way discovery{RESET}")

    nodes = [
        DarkMatterNode(BASE_PORT + i, display_name=f"node-{i}")
        for i in range(3)
    ]

    try:
        for n in nodes:
            n.start()
        for n in nodes:
            if not n.wait_ready():
                report(f"{n} started", False, "failed to start")
                return

        # Run scan from each node's perspective
        for i, scanner in enumerate(nodes):
            result = subprocess.run(
                [PYTHON, "-c", _scan_script_keys(scanner.agent_id, scanner.port, BASE_PORT, BASE_PORT + 10)],
                capture_output=True, text=True, timeout=10,
            )

            found = json.loads(result.stdout.strip())
            others = [n.agent_id for j, n in enumerate(nodes) if j != i]
            all_found = all(o in found for o in others)
            report(f"Node {i} finds both others", all_found,
                   f"expected {[o[:8] for o in others]}, found {[f[:8] for f in found]}")

    finally:
        for n in nodes:
            n.stop()
        time.sleep(PORT_RELEASE_WAIT)


def test_dead_node_disappears() -> None:
    """A killed node is no longer discovered on the next scan."""
    print(f"\n{BOLD}Test: dead node disappears{RESET}")

    node_a = DarkMatterNode(BASE_PORT, display_name="alive")
    node_b = DarkMatterNode(BASE_PORT + 1, display_name="doomed")

    try:
        node_a.start()
        node_b.start()
        if not node_a.wait_ready():
            report("Node A started", False, "failed to start")
            return
        if not node_b.wait_ready():
            report("Node B started", False, "failed to start")
            return

        # Scan — should find B
        result = subprocess.run(
            [PYTHON, "-c", _scan_script_keys(node_a.agent_id, node_a.port, BASE_PORT, BASE_PORT + 10)],
            capture_output=True, text=True, timeout=10,
        )
        found_before = json.loads(result.stdout.strip())
        report("B found while alive", node_b.agent_id in found_before)

        # Kill B
        node_b.stop()
        time.sleep(1)

        # Scan again — B should be gone
        result = subprocess.run(
            [PYTHON, "-c", _scan_script_keys(node_a.agent_id, node_a.port, BASE_PORT, BASE_PORT + 10)],
            capture_output=True, text=True, timeout=10,
        )
        found_after = json.loads(result.stdout.strip())
        report("B gone after kill", node_b.agent_id not in found_after,
               f"still found: {found_after}")

    finally:
        node_a.stop()
        node_b.stop()
        time.sleep(PORT_RELEASE_WAIT)


def test_scan_performance() -> None:
    """Scanning 10 closed ports completes in under 2 seconds."""
    print(f"\n{BOLD}Test: scan performance{RESET}")

    result = subprocess.run(
        [PYTHON, "-c", f"""
import asyncio, time, sys
sys.path.insert(0, "{PROJECT_ROOT}")

from darkmatter.models import AgentState, AgentStatus
from darkmatter.network.discovery import scan_local_ports
import darkmatter.config

darkmatter.config.DISCOVERY_LOCAL_PORTS = range(9800, 9810)
import darkmatter.network.discovery as _disc
_disc.DISCOVERY_LOCAL_PORTS = range(9800, 9810)

state = AgentState(agent_id="perf", bio="t", status=AgentStatus.ACTIVE, port=9999)

async def main():
    start = time.time()
    await scan_local_ports(state)
    print(f"{{time.time() - start:.2f}}")

asyncio.run(main())
"""],
        capture_output=True, text=True, timeout=15,
    )

    elapsed = float(result.stdout.strip())
    report("10 closed ports < 2s", elapsed < 2.0, f"took {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{BOLD}DarkMatter Discovery Tests (end-to-end){RESET}")
    print("=" * 50)

    test_well_known_endpoint()
    test_two_nodes_discover_each_other()
    test_three_nodes_nway()
    test_dead_node_disappears()
    test_scan_performance()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{'=' * 50}")
    print(f"{BOLD}Results: {GREEN}{passed} passed{RESET}, ", end="")
    if failed:
        print(f"{RED}{failed} failed{RESET}")
    else:
        print(f"{BOLD}0 failed{RESET}")

    if failed:
        print(f"\n{RED}Failed tests:{RESET}")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}: {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
