"""
Application composition root — create_app(), startup hooks, main entry point.

This is the top-level module that wires everything together.
Depends on: everything (by design — this IS the composition root)
"""

import asyncio
import contextlib
import os
import sys
from typing import Optional

import anyio
import uvicorn
from starlette.routing import Route, Mount, Router

from darkmatter.config import (
    DEFAULT_PORT,
    DISCOVERY_PORT,
    DISCOVERY_MCAST_GROUP,
    DISCOVERY_LOCAL_PORTS,
    HEALTH_CHECK_INTERVAL,
    ANCHOR_NODES,
    AGENT_SPAWN_ENABLED,
    AGENT_SPAWN_MAX_CONCURRENT,
    AGENT_SPAWN_MAX_PER_HOUR,
    AGENT_SPAWN_TERMINAL,
    SOLANA_AVAILABLE,
    WEBRTC_AVAILABLE,
    UPNP_AVAILABLE,
)
from darkmatter.models import AgentState, AgentStatus
from darkmatter.identity import load_or_create_passport
from darkmatter.state import set_state, get_state, save_state, state_file_path, load_state_from_file
from darkmatter.mcp import mcp
from darkmatter.mcp.visibility import initialize_tool_visibility, status_updater
from darkmatter.network.resilience import (
    discover_public_url,
    check_nat_status,
    broadcast_peer_update,
    network_health_loop,
    cleanup_upnp,
    get_public_url,
)
from darkmatter.network.discovery import (
    DiscoveryProtocol,
    discovery_loop,
    ensure_entrypoint_running,
    handle_well_known,
)
from darkmatter.network.mesh import (
    handle_connection_request,
    handle_connection_accepted,
    handle_accept_pending,
    handle_message,
    handle_webhook_post,
    handle_webhook_get,
    handle_status,
    handle_network_info,
    handle_impression_get,
    handle_webrtc_offer,
    handle_peer_update,
    handle_peer_lookup,
    handle_gas_match,
    handle_gas_signal,
    handle_gas_result,
)
from darkmatter.bootstrap import handle_bootstrap, handle_bootstrap_source
from darkmatter.spawn import spawn_agent_for_message


# =============================================================================
# State initialization
# =============================================================================

def init_state(port: int = None) -> None:
    """Initialize agent state from passport + persisted state. Safe to call multiple times.

    Identity flow:
    1. Load (or create) passport from .darkmatter/passport.key in cwd
    2. Derive agent_id = public_key_hex (deterministic from passport)
    3. Try loading state from ~/.darkmatter/state/<public_key_hex>.json
    4. If not found, create fresh state
    """
    if get_state() is not None:
        return  # Already initialized

    if port is None:
        port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))

    display_name = os.environ.get("DARKMATTER_DISPLAY_NAME", os.environ.get("DARKMATTER_AGENT_ID", ""))
    bio = os.environ.get("DARKMATTER_BIO", "A DarkMatter mesh agent.")

    # Step 1: Load or create passport — this IS our identity
    priv, pub = load_or_create_passport()
    agent_id = pub  # Agent ID = public key hex

    # Step 2: Create a temporary AgentState so state_file_path() works
    state = AgentState(
        agent_id=agent_id,
        bio=bio,
        status=AgentStatus.ACTIVE,
        port=port,
        private_key_hex=priv,
        public_key_hex=pub,
        display_name=display_name or None,
    )
    set_state(state)

    # Step 3: Try loading state from passport-keyed path
    path = state_file_path()
    restored = load_state_from_file(path)

    if restored:
        # Restore state but enforce passport-derived identity
        restored.agent_id = agent_id  # Always use passport-derived ID
        restored.private_key_hex = priv
        restored.public_key_hex = pub
        restored.port = port
        restored.status = AgentStatus.ACTIVE
        if display_name:
            restored.display_name = display_name
        set_state(restored)
        print(f"[DarkMatter] Restored state (display: {restored.display_name or 'none'}, "
              f"{len(restored.connections)} connections)", file=sys.stderr)
    else:
        # state already set to fresh state above
        print(f"[DarkMatter] Starting fresh (display: {display_name or 'none'}) "
              f"on port {port}", file=sys.stderr)

    print(f"[DarkMatter] Identity: {agent_id[:16]}...{agent_id[-8:]}", file=sys.stderr)

    # Derive Solana wallet (ephemeral — not persisted, derived from passport each startup)
    state = get_state()
    if SOLANA_AVAILABLE and state.private_key_hex:
        from darkmatter.wallet.solana import _get_solana_wallet_address
        state.wallets["solana"] = _get_solana_wallet_address(state.private_key_hex)
        print(f"[DarkMatter] Solana wallet: {state.wallets['solana']}", file=sys.stderr)

    save_state()


# =============================================================================
# App factory
# =============================================================================

def create_app() -> Router:
    """Create the combined Starlette app with MCP and DarkMatter endpoints.

    Returns:
        The ASGI app (a Starlette Router).
    """
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    init_state(port)

    # LAN discovery setup
    discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"

    async def on_startup() -> None:
        state = get_state()

        if discovery_enabled:
            import struct as _struct
            import socket as _socket
            loop = asyncio.get_event_loop()

            # Multicast listener for LAN discovery (best-effort)
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM, _socket.IPPROTO_UDP)
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                if hasattr(_socket, "SO_REUSEPORT"):
                    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEPORT, 1)
                sock.bind(("", DISCOVERY_PORT))
                mreq = _struct.pack("4s4s",
                    _socket.inet_aton(DISCOVERY_MCAST_GROUP),
                    _socket.inet_aton("0.0.0.0"))
                sock.setsockopt(_socket.IPPROTO_IP, _socket.IP_ADD_MEMBERSHIP, mreq)
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: DiscoveryProtocol(state),
                    sock=sock,
                )
            except OSError as e:
                print(f"[DarkMatter] LAN multicast listener failed ({e}), local HTTP discovery still active", file=sys.stderr)

            # Start discovery loop (local HTTP scan + LAN multicast beacons)
            asyncio.create_task(discovery_loop(state))
            print(f"[DarkMatter] Discovery: ENABLED (local: HTTP scan ports "
                  f"{DISCOVERY_LOCAL_PORTS.start}-{DISCOVERY_LOCAL_PORTS.stop - 1}, "
                  f"LAN: multicast {DISCOVERY_MCAST_GROUP}:{DISCOVERY_PORT})", file=sys.stderr)

        # Start live status updater (updates tool description and notifies clients)
        asyncio.create_task(status_updater())
        print(f"[DarkMatter] Live status updater: ENABLED (5s interval)", file=sys.stderr)

        # Initialize dynamic tool visibility (hide optional tools until needed)
        initialize_tool_visibility()

        # Discover public URL and detect NAT
        state.public_url = await discover_public_url(port, state=state)
        state.nat_detected = await check_nat_status(state.public_url)
        if state.nat_detected:
            print(f"[DarkMatter] NAT detected: True — using anchor webhook relay", file=sys.stderr)
        from darkmatter.network.mesh import _process_webhook_locally
        asyncio.create_task(network_health_loop(state, process_webhook_fn=_process_webhook_locally))
        print(f"[DarkMatter] Network health loop: ENABLED ({HEALTH_CHECK_INTERVAL}s interval)", file=sys.stderr)
        print(f"[DarkMatter] UPnP: {'AVAILABLE' if UPNP_AVAILABLE else 'disabled (pip install miniupnpc)'}", file=sys.stderr)

        # Register with anchor nodes on boot
        if ANCHOR_NODES and state.public_url:
            await broadcast_peer_update(state)
            print(f"[DarkMatter] Anchor nodes: registered with {len(ANCHOR_NODES)} anchor(s)", file=sys.stderr)
        elif ANCHOR_NODES:
            print(f"[DarkMatter] Anchor nodes: configured but no public URL yet", file=sys.stderr)

        # Auto-start entrypoint (human node) if not already running
        asyncio.create_task(ensure_entrypoint_running())

        # Re-spawn agents for any queued messages left from a previous session
        if AGENT_SPAWN_ENABLED and state.router_mode == "spawn" and state.message_queue:
            queued_count = len(state.message_queue)
            print(f"[DarkMatter] {queued_count} message(s) in queue from previous session, spawning agents...", file=sys.stderr)
            for msg in list(state.message_queue):
                asyncio.create_task(spawn_agent_for_message(state, msg))

    # DarkMatter mesh protocol routes
    darkmatter_routes = [
        Route("/connection_request", handle_connection_request, methods=["POST"]),
        Route("/connection_accepted", handle_connection_accepted, methods=["POST"]),
        Route("/accept_pending", handle_accept_pending, methods=["POST"]),
        Route("/message", handle_message, methods=["POST"]),
        Route("/webhook/{message_id}", handle_webhook_post, methods=["POST"]),
        Route("/webhook/{message_id}", handle_webhook_get, methods=["GET"]),
        Route("/status", handle_status, methods=["GET"]),
        Route("/network_info", handle_network_info, methods=["GET"]),
        Route("/impression/{agent_id}", handle_impression_get, methods=["GET"]),
        Route("/webrtc_offer", handle_webrtc_offer, methods=["POST"]),
        Route("/peer_update", handle_peer_update, methods=["POST"]),
        Route("/peer_lookup/{agent_id}", handle_peer_lookup, methods=["GET"]),
        Route("/gas_match", handle_gas_match, methods=["POST"]),
        Route("/gas_signal", handle_gas_signal, methods=["POST"]),
        Route("/gas_result", handle_gas_result, methods=["POST"]),
    ]

    # Extract the MCP ASGI handler and its session manager for lifecycle.
    # Identity is passport-based — agent_id = public key hex from .darkmatter/passport.key
    mcp_starlette = mcp.streamable_http_app()
    mcp_handler = mcp_starlette.routes[0].app  # StreamableHTTPASGIApp
    session_manager = mcp_handler.session_manager

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Start MCP session manager + run our startup hooks
        async with session_manager.run():
            await on_startup()
            yield
            cleanup_upnp(get_state())

    # Build the app. Use redirect_slashes=False so POST /mcp doesn't get
    # redirected to /mcp/ (which breaks MCP client connections).
    app = Router(
        routes=[
            Route("/.well-known/darkmatter.json", handle_well_known, methods=["GET"]),
            Route("/bootstrap", handle_bootstrap, methods=["GET"]),
            Route("/bootstrap/server.py", handle_bootstrap_source, methods=["GET"]),
            Mount("/__darkmatter__", routes=darkmatter_routes),
            Route("/mcp", mcp_handler),
        ],
        redirect_slashes=False,
        lifespan=lifespan,
    )

    return app


# =============================================================================
# Startup banner + port utilities
# =============================================================================

def print_startup_banner(port: int, transport: str, discovery_enabled: bool) -> None:
    """Print startup banner to stderr."""
    print(f"[DarkMatter] Starting mesh protocol on http://localhost:{port}", file=sys.stderr)
    print(f"[DarkMatter] MCP transport: {transport}", file=sys.stderr)
    print(f"[DarkMatter] Discovery: {'ENABLED' if discovery_enabled else 'disabled'}", file=sys.stderr)
    print(f"[DarkMatter] WebRTC: {'AVAILABLE' if WEBRTC_AVAILABLE else 'disabled (pip install aiortc)'}", file=sys.stderr)
    print(f"[DarkMatter] UPnP: {'AVAILABLE' if UPNP_AVAILABLE else 'disabled (pip install miniupnpc)'}", file=sys.stderr)
    spawn_info = f"ENABLED (max {AGENT_SPAWN_MAX_CONCURRENT} concurrent, {AGENT_SPAWN_MAX_PER_HOUR}/hr)" if AGENT_SPAWN_ENABLED else "disabled"
    if AGENT_SPAWN_ENABLED and AGENT_SPAWN_TERMINAL:
        spawn_info += " [terminal mode]"
    print(f"[DarkMatter] Agent auto-spawn: {spawn_info}", file=sys.stderr)
    print(f"[DarkMatter] Bootstrap: curl http://localhost:{port}/bootstrap | bash", file=sys.stderr)


def check_port_owner(host: str, port: int) -> Optional[str]:
    """Check if a port has a DarkMatter server and return its agent_id, or None if port is free."""
    import socket as _socket
    # First check if port is in use at all
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return None  # Port is free
        except OSError:
            pass  # Port in use — probe it

    # Port is taken — check if it's a DarkMatter node
    try:
        import httpx
        resp = httpx.get(f"http://127.0.0.1:{port}/.well-known/darkmatter.json", timeout=1.0)
        if resp.status_code == 200:
            info = resp.json()
            return info.get("agent_id")
    except Exception:
        pass
    return "unknown"  # Port taken by non-DarkMatter process


def find_free_port(host: str, start: int) -> int:
    """Find a free port in the discovery range (start to start+10)."""
    import socket as _socket
    for port in range(start, start + 11):
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free ports in range {start}-{start + 10}")


# =============================================================================
# Dual transport — stdio + HTTP
# =============================================================================

async def run_stdio_with_http() -> None:
    """Run MCP over stdio while serving HTTP mesh endpoints in the background.

    This is the preferred mode when launched by an MCP client (e.g. Claude Code).
    The client talks MCP over stdin/stdout. The HTTP server runs alongside for
    agent-to-agent mesh communication, discovery, and webhooks.

    Port conflict resolution:
    - Port free -> start normally
    - Port taken by OUR server (same agent_id) -> another session of us is
      already running the HTTP mesh. Run stdio-only and share state.
    - Port taken by SOMEONE ELSE -> find a new free port and start there.
    """
    from mcp.server.stdio import stdio_server

    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    host = os.environ.get("DARKMATTER_HOST", "127.0.0.1")

    # Load our passport to get our agent_id (if we have one)
    _priv, _pub = load_or_create_passport()
    our_agent_id = _pub

    # Check who owns the port
    port_owner = check_port_owner(host, port)

    if port_owner is None:
        # Port is free — start normally
        app = create_app()
        discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
        print_startup_banner(port, "stdio (with HTTP mesh on port " + str(port) + ")", discovery_enabled)

        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)

        async with stdio_server() as (read_stream, write_stream):
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.serve)
                await mcp._mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp._mcp_server.create_initialization_options(),
                )
                server.should_exit = True

    elif port_owner == our_agent_id:
        # Our server is already running — parallel session, share state
        print(f"[DarkMatter] Port {port} is already running our server (agent {our_agent_id[:12]}...).", file=sys.stderr)
        print(f"[DarkMatter] Running stdio-only MCP (parallel session, shared state).", file=sys.stderr)

        init_state(port)

        async with stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )

    else:
        # Port taken by a different agent — find a new port
        print(f"[DarkMatter] Port {port} is taken by another agent ({port_owner[:12] if port_owner != 'unknown' else 'unknown'}...).", file=sys.stderr)
        new_port = find_free_port(host, DEFAULT_PORT)
        print(f"[DarkMatter] Using port {new_port} instead.", file=sys.stderr)

        # Override port for this session
        os.environ["DARKMATTER_PORT"] = str(new_port)

        app = create_app()
        discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
        print_startup_banner(new_port, "stdio (with HTTP mesh on port " + str(new_port) + ")", discovery_enabled)

        config = uvicorn.Config(app, host=host, port=new_port, log_level="warning")
        server = uvicorn.Server(config)

        async with stdio_server() as (read_stream, write_stream):
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.serve)
                await mcp._mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp._mcp_server.create_initialization_options(),
                )
                server.should_exit = True


# =============================================================================
# Main entry point
# =============================================================================

def main() -> None:
    """Entry point — detect transport mode and run."""
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    transport = os.environ.get("DARKMATTER_TRANSPORT", "auto")

    # Auto-detect: if stdin is not a TTY, we're being launched by an MCP client
    use_stdio = transport == "stdio" or (transport == "auto" and not sys.stdin.isatty())

    if use_stdio:
        anyio.run(run_stdio_with_http)
    else:
        # Standalone HTTP mode (manual start, or DARKMATTER_TRANSPORT=http)
        app = create_app()
        discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
        print_startup_banner(port, "streamable-http", discovery_enabled)

        host = os.environ.get("DARKMATTER_HOST", "127.0.0.1")
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
