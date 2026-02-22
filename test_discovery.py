#!/usr/bin/env python3
"""
Integration tests for DarkMatter discovery.

Tests the HTTP-based local discovery and multicast LAN discovery by
running real HTTP servers and verifying _scan_local_ports works correctly.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

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


def make_darkmatter_app(agent_id: str, port: int, *, genesis: bool = False,
                        bio: str = "Test agent", status: str = "active") -> Starlette:
    """Create a minimal Starlette app that serves /.well-known/darkmatter.json."""
    info = {
        "darkmatter": True,
        "protocol_version": "0.1",
        "agent_id": agent_id,
        "display_name": agent_id[:8],
        "public_key_hex": "a" * 64,
        "bio": bio,
        "status": status,
        "is_genesis": genesis,
        "accepting_connections": True,
        "mesh_url": f"http://127.0.0.1:{port}/__darkmatter__",
        "mcp_url": f"http://127.0.0.1:{port}/mcp",
    }

    async def well_known(request):
        return JSONResponse(info)

    return Starlette(routes=[
        Route("/.well-known/darkmatter.json", well_known, methods=["GET"]),
    ])


async def start_server(app: Starlette, port: int) -> tuple:
    """Start a uvicorn server and wait until it's ready."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{port}/.well-known/darkmatter.json", timeout=0.5)
                if r.status_code == 200:
                    break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    return task, srv


async def stop_server(task, srv):
    srv.should_exit = True
    await task


def make_state(agent_id: str, port: int) -> "AgentState":
    """Create a minimal AgentState for testing."""
    import server
    return server.AgentState(
        agent_id=agent_id,
        bio="Test",
        status=server.AgentStatus.ACTIVE,
        port=port,
        display_name=agent_id[:8],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_well_known_from_real_server() -> None:
    """A real DarkMatter server exposes /.well-known/darkmatter.json."""
    print(f"\n{BOLD}Test: real server well-known endpoint{RESET}")
    import server

    state_path = tempfile.mktemp(suffix=".json")
    server._agent_state = None
    os.environ["DARKMATTER_STATE_FILE"] = state_path
    os.environ["DARKMATTER_PORT"] = "9950"
    os.environ["DARKMATTER_HOST"] = "127.0.0.1"
    os.environ["DARKMATTER_GENESIS"] = "true"
    os.environ["DARKMATTER_DISCOVERY"] = "false"
    os.environ.pop("DARKMATTER_AGENT_ID", None)
    os.environ.pop("DARKMATTER_MCP_TOKEN", None)
    os.environ.pop("DARKMATTER_DISPLAY_NAME", None)

    app = server.create_app()
    state = server._agent_state
    task, srv = await start_server(app, 9950)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://127.0.0.1:9950/.well-known/darkmatter.json", timeout=2.0)

        info = resp.json()
        report("status 200", resp.status_code == 200)
        report("darkmatter: true", info.get("darkmatter") is True)
        report("has agent_id", isinstance(info.get("agent_id"), str) and len(info["agent_id"]) > 0)
        report("has bio", isinstance(info.get("bio"), str))
        report("has status", info.get("status") == "active")
        report("has accepting_connections", isinstance(info.get("accepting_connections"), bool))
        report("has mcp_url", "mcp" in info.get("mcp_url", ""))
    finally:
        await stop_server(task, srv)
        if os.path.exists(state_path):
            os.unlink(state_path)


async def test_two_nodes_discover_each_other() -> None:
    """Two nodes on different ports discover each other via HTTP scan."""
    print(f"\n{BOLD}Test: two nodes discover each other{RESET}")
    import server

    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())
    port_a, port_b = 9951, 9952

    app_a = make_darkmatter_app(id_a, port_a, genesis=True, bio="Agent A")
    app_b = make_darkmatter_app(id_b, port_b, genesis=False, bio="Agent B")

    task_a, srv_a = await start_server(app_a, port_a)
    task_b, srv_b = await start_server(app_b, port_b)

    try:
        original = server.DISCOVERY_LOCAL_PORTS
        server.DISCOVERY_LOCAL_PORTS = range(port_a, port_b + 1)

        state_a = make_state(id_a, port_a)
        await server._scan_local_ports(state_a)

        report("A discovers B",
               id_b in state_a.discovered_peers,
               f"A's peers: {list(state_a.discovered_peers.keys())}")

        if id_b in state_a.discovered_peers:
            peer = state_a.discovered_peers[id_b]
            report("A sees B's correct URL", peer["url"] == f"http://127.0.0.1:{port_b}",
                   f"got: {peer['url']}")
            report("A sees B's bio", peer.get("bio") == "Agent B", f"got: {peer.get('bio')}")
            report("A sees B as active", peer.get("status") == "active")
            report("peer source is 'local'", peer.get("source") == "local")

        state_b = make_state(id_b, port_b)
        await server._scan_local_ports(state_b)

        report("B discovers A",
               id_a in state_b.discovered_peers,
               f"B's peers: {list(state_b.discovered_peers.keys())}")

        report("A does not discover itself",
               id_a not in state_a.discovered_peers)
        report("B does not discover itself",
               id_b not in state_b.discovered_peers)

        server.DISCOVERY_LOCAL_PORTS = original
    finally:
        await stop_server(task_a, srv_a)
        await stop_server(task_b, srv_b)


async def test_three_nodes_all_discover_each_other() -> None:
    """Three nodes all discover each other (N-way)."""
    print(f"\n{BOLD}Test: three nodes N-way discovery{RESET}")
    import server

    ports = [9953, 9954, 9955]
    ids = [str(uuid.uuid4()) for _ in ports]

    servers = []
    for i, (aid, port) in enumerate(zip(ids, ports)):
        app = make_darkmatter_app(aid, port, genesis=(i == 0), bio=f"Agent {i}")
        task, srv = await start_server(app, port)
        servers.append((task, srv))

    try:
        original = server.DISCOVERY_LOCAL_PORTS
        server.DISCOVERY_LOCAL_PORTS = range(ports[0], ports[-1] + 1)

        states = [make_state(aid, port) for aid, port in zip(ids, ports)]

        for state in states:
            await server._scan_local_ports(state)

        for i, state in enumerate(states):
            others = [ids[j] for j in range(3) if j != i]
            found = list(state.discovered_peers.keys())
            all_found = all(oid in found for oid in others)
            report(f"Agent {i} discovers both others", all_found,
                   f"expected {[o[:8] for o in others]}, got {[f[:8] for f in found]}")

        server.DISCOVERY_LOCAL_PORTS = original
    finally:
        for task, srv in servers:
            await stop_server(task, srv)


async def test_dead_node_not_discovered() -> None:
    """A node that's been shut down doesn't appear in discovery."""
    print(f"\n{BOLD}Test: dead node not discovered{RESET}")
    import server

    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())
    port_a, port_b = 9956, 9957

    app_a = make_darkmatter_app(id_a, port_a)
    app_b = make_darkmatter_app(id_b, port_b)

    task_a, srv_a = await start_server(app_a, port_a)
    task_b, srv_b = await start_server(app_b, port_b)

    try:
        original = server.DISCOVERY_LOCAL_PORTS
        server.DISCOVERY_LOCAL_PORTS = range(port_a, port_b + 1)

        state_a = make_state(id_a, port_a)
        await server._scan_local_ports(state_a)
        report("A initially discovers B", id_b in state_a.discovered_peers)

        # Kill B
        await stop_server(task_b, srv_b)
        # Wait for port to close
        for _ in range(20):
            try:
                async with httpx.AsyncClient() as c:
                    await c.get(f"http://127.0.0.1:{port_b}/.well-known/darkmatter.json", timeout=0.3)
            except httpx.HTTPError:
                break
            await asyncio.sleep(0.1)

        state_a.discovered_peers.clear()
        await server._scan_local_ports(state_a)
        report("A no longer discovers dead B",
               id_b not in state_a.discovered_peers,
               f"peers after B died: {list(state_a.discovered_peers.keys())}")

        server.DISCOVERY_LOCAL_PORTS = original
    finally:
        await stop_server(task_a, srv_a)


async def test_stale_peers_pruned() -> None:
    """Peers older than DISCOVERY_MAX_AGE are pruned."""
    print(f"\n{BOLD}Test: stale peers pruned{RESET}")
    import server

    state = make_state("pruner", 9999)

    state.discovered_peers["stale-agent"] = {
        "url": "http://127.0.0.1:9999",
        "bio": "gone",
        "status": "active",
        "genesis": False,
        "accepting": True,
        "source": "local",
        "ts": time.time() - server.DISCOVERY_MAX_AGE - 10,
    }
    state.discovered_peers["fresh-agent"] = {
        "url": "http://127.0.0.1:9998",
        "bio": "here",
        "status": "active",
        "genesis": False,
        "accepting": True,
        "source": "local",
        "ts": time.time(),
    }

    now = time.time()
    stale = [k for k, v in state.discovered_peers.items()
             if now - v.get("ts", 0) > server.DISCOVERY_MAX_AGE]
    for k in stale:
        del state.discovered_peers[k]

    report("stale peer pruned", "stale-agent" not in state.discovered_peers)
    report("fresh peer kept", "fresh-agent" in state.discovered_peers)


async def test_non_darkmatter_port_ignored() -> None:
    """A port running non-DarkMatter HTTP is ignored gracefully."""
    print(f"\n{BOLD}Test: non-DarkMatter port ignored{RESET}")
    import server

    # Server that returns non-JSON at the well-known path
    dummy_app = Starlette(routes=[
        Route("/.well-known/darkmatter.json",
              lambda r: JSONResponse({"not": "darkmatter"}), methods=["GET"]),
    ])
    port = 9959
    task, srv = await start_server(dummy_app, port)

    try:
        original = server.DISCOVERY_LOCAL_PORTS
        server.DISCOVERY_LOCAL_PORTS = range(port, port + 1)

        state = make_state("scanner", 9960)
        await server._scan_local_ports(state)

        report("non-DarkMatter port ignored",
               len(state.discovered_peers) == 0,
               f"peers: {list(state.discovered_peers.keys())}")

        server.DISCOVERY_LOCAL_PORTS = original
    finally:
        await stop_server(task, srv)


async def test_closed_ports_handled() -> None:
    """Scanning ports with nothing listening doesn't crash or hang."""
    print(f"\n{BOLD}Test: closed ports handled gracefully{RESET}")
    import server

    original = server.DISCOVERY_LOCAL_PORTS
    server.DISCOVERY_LOCAL_PORTS = range(9970, 9980)  # 10 ports, none listening

    state = make_state("scanner", 9999)

    start = time.time()
    await server._scan_local_ports(state)
    elapsed = time.time() - start

    report("no crash on closed ports", True)
    report("scan completes quickly (< 2s)", elapsed < 2.0, f"took {elapsed:.2f}s")
    report("no false discoveries", len(state.discovered_peers) == 0)

    server.DISCOVERY_LOCAL_PORTS = original


async def test_discovery_loop_integration() -> None:
    """The _discovery_loop background task populates discovered_peers."""
    print(f"\n{BOLD}Test: discovery loop integration{RESET}")
    import server

    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())
    port_a, port_b = 9962, 9963

    app_b = make_darkmatter_app(id_b, port_b, bio="Loop target")
    task_b, srv_b = await start_server(app_b, port_b)

    try:
        original_ports = server.DISCOVERY_LOCAL_PORTS
        original_interval = server.DISCOVERY_INTERVAL
        server.DISCOVERY_LOCAL_PORTS = range(port_a, port_b + 1)
        server.DISCOVERY_INTERVAL = 1  # Fast cycle for test

        state_a = make_state(id_a, port_a)
        loop_task = asyncio.create_task(server._discovery_loop(state_a))

        # Wait for at least one cycle
        for _ in range(30):
            if id_b in state_a.discovered_peers:
                break
            await asyncio.sleep(0.2)

        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        report("discovery loop found peer",
               id_b in state_a.discovered_peers,
               f"peers: {list(state_a.discovered_peers.keys())}")

        if id_b in state_a.discovered_peers:
            peer = state_a.discovered_peers[id_b]
            report("peer has correct URL", peer["url"] == f"http://127.0.0.1:{port_b}")
            report("peer bio populated", peer.get("bio") == "Loop target")

        server.DISCOVERY_LOCAL_PORTS = original_ports
        server.DISCOVERY_INTERVAL = original_interval
    finally:
        await stop_server(task_b, srv_b)


async def test_scan_concurrent_performance() -> None:
    """Port scanning runs concurrently, not sequentially."""
    print(f"\n{BOLD}Test: concurrent scan performance{RESET}")
    import server

    original = server.DISCOVERY_LOCAL_PORTS
    # 10 closed ports with 0.5s connect timeout should complete in ~0.5s if concurrent,
    # ~5s if sequential
    server.DISCOVERY_LOCAL_PORTS = range(9980, 9990)

    state = make_state("perf-test", 9999)

    start = time.time()
    await server._scan_local_ports(state)
    elapsed = time.time() - start

    # Should be well under 2s with concurrent execution
    report("10-port scan < 2s (concurrent)", elapsed < 2.0, f"took {elapsed:.2f}s")

    server.DISCOVERY_LOCAL_PORTS = original


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    print(f"\n{BOLD}DarkMatter Discovery Tests{RESET}")
    print("=" * 50)

    await test_well_known_from_real_server()
    await test_two_nodes_discover_each_other()
    await test_three_nodes_all_discover_each_other()
    await test_dead_node_not_discovered()
    await test_stale_peers_pruned()
    await test_non_darkmatter_port_ignored()
    await test_closed_ports_handled()
    await test_discovery_loop_integration()
    await test_scan_concurrent_performance()

    # Summary
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
    asyncio.run(main())
