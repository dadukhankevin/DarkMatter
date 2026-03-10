"""
Application composition root — create_app(), startup hooks, main entry point.

This is the top-level module that wires everything together.
Depends on: everything (by design — this IS the composition root)
"""

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
import traceback
from typing import Optional

import anyio
import uvicorn
from starlette.routing import Route, Mount, Router

from darkmatter.config import (
    DEFAULT_PORT,
    DISCOVERY_PORT,
    DISCOVERY_MCAST_GROUP,
    DISCOVERY_LOCAL_PORTS,
    AGENT_ROUTER_MODE,
)
from darkmatter.models import AgentState, AgentStatus
from darkmatter.names import generate_agent_name
from darkmatter.identity import load_or_create_passport
from darkmatter.state import (
    set_state, get_state, get_state_for, save_state, state_file_path,
    load_state_from_file, clear_waiting_signal, register_agent, list_hosted_agents,
    scan_state_files,
)
from darkmatter.mcp import mcp
import darkmatter.mcp.tools  # noqa: F401 — registers @mcp.tool() decorators
from darkmatter.mcp.visibility import initialize_tool_visibility, status_updater
from darkmatter.network.manager import NetworkManager, set_network_manager, get_network_manager
from darkmatter.network.transports.http import HttpTransport
from darkmatter.network.transports.webrtc import WebRTCTransport
from darkmatter.network.discovery import (
    DiscoveryProtocol,
    discovery_loop,
    handle_well_known,
)
from darkmatter.network.mesh import (
    dispatch_webrtc_message,
    handle_connection_request,
    handle_connection_accepted,
    handle_accept_pending,
    handle_message,

    handle_status,
    handle_network_info,
    handle_status_broadcast,
    handle_impression_get,
    handle_webrtc_offer,
    handle_peer_update,
    handle_peer_lookup,
    handle_get_peers,
    handle_mesh_route,
    handle_antimatter_match,
    handle_antimatter_signal,
    handle_antimatter_result,
    handle_insight_push,
    handle_sdp_relay,
    handle_sdp_relay_deliver,
    handle_connection_proof,
    handle_admin_connect,
    handle_genome,
    handle_local_inbox,
    handle_local_pending,
    handle_local_connections,
    handle_local_set_impression,
    handle_local_config,
    handle_ping,
)
from darkmatter.wallet.antimatter import set_network_fns as set_antimatter_network_fns
from darkmatter.logging import get_logger

_log = get_logger("app")


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

    display_name = os.environ.get("DARKMATTER_DISPLAY_NAME", "")
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
        display_name=display_name or generate_agent_name(),
    )
    set_state(state)

    # Step 3: Try loading state from passport-keyed path
    path = state_file_path()
    restored = load_state_from_file(path)

    if restored:
        # Restore state but enforce passport-derived identity and spawn mode
        restored.agent_id = agent_id  # Always use passport-derived ID
        restored.private_key_hex = priv
        restored.public_key_hex = pub
        restored.port = port
        restored.status = AgentStatus.ACTIVE
        restored.router_mode = AGENT_ROUTER_MODE  # From config — don't let stale state override
        if display_name:
            restored.display_name = display_name
        elif not restored.display_name:
            restored.display_name = generate_agent_name()
        set_state(restored)
        _log.info("Restored state (display: %s, %d connections)",
                  restored.display_name or "none", len(restored.connections))
    else:
        # state already set to fresh state above
        _log.info("Starting fresh (display: %s) on port %d", display_name or "none", port)

    _log.info("Identity: %s...%s", agent_id[:16], agent_id[-8:])

    # Derive Solana wallet (ephemeral — not persisted, derived from passport each startup)
    state = get_state()
    if state.private_key_hex:
        from darkmatter.wallet.solana import _get_solana_wallet_address
        state.wallets["solana"] = _get_solana_wallet_address(state.private_key_hex)
        _log.info("Solana wallet: %s", state.wallets["solana"])

    save_state()
    clear_waiting_signal()


# =============================================================================
# Multi-agent daemon scanning
# =============================================================================

def _register_discovered_agents(daemon_port: int) -> None:
    """Scan state files and register any agents not yet in the registry.

    Only registers agents whose state file indicates the same port as this daemon
    (to avoid claiming agents from other daemons).
    """
    from darkmatter.config import AGENT_ROUTER_MODE

    current_agents = set(list_hosted_agents())
    for info in scan_state_files():
        agent_id = info["agent_id"]
        if agent_id in current_agents:
            continue
        # Only adopt agents on our port
        if info.get("port", daemon_port) != daemon_port:
            continue

        state = load_state_from_file(info["path"], agent_id=agent_id)
        if state is None:
            continue

        state.port = daemon_port
        state.status = AgentStatus.ACTIVE
        state.router_mode = AGENT_ROUTER_MODE
        register_agent(agent_id, state)
        _log.info("Discovered and registered agent %s... (%s)",
                  agent_id[:12], state.display_name or "unnamed")


async def _agent_scan_loop(daemon_port: int) -> None:
    """Periodically scan for new/removed agent state files."""
    while True:
        try:
            await asyncio.sleep(10)
            _register_discovered_agents(daemon_port)
        except asyncio.CancelledError:
            return
        except Exception as e:
            _log.error("Agent scan loop error: %s", e)


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

    # Create and register NetworkManager with transport plugins
    manager = NetworkManager(state_getter=get_state, state_saver=save_state)
    manager.register_transport(HttpTransport())
    webrtc = WebRTCTransport()
    webrtc.set_message_dispatcher(dispatch_webrtc_message)
    manager.register_transport(webrtc)
    set_network_manager(manager)

    # Wire antimatter economy into NetworkManager for transport-agnostic sends
    set_antimatter_network_fns(
        send_fn=manager.send,
        http_request_fn=manager.http_request,
    )

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
                _log.warning("LAN multicast listener failed (%s), local HTTP discovery still active", e)

            # Start discovery loop (local HTTP scan + LAN multicast beacons)
            asyncio.create_task(discovery_loop(state))
            _log.info(
                "Discovery: ENABLED (local: HTTP scan ports %d-%d, LAN: multicast %s:%d)",
                DISCOVERY_LOCAL_PORTS.start,
                DISCOVERY_LOCAL_PORTS.stop - 1,
                DISCOVERY_MCAST_GROUP,
                DISCOVERY_PORT,
            )

        # Start live status updater (updates tool description and notifies clients)
        asyncio.create_task(status_updater())
        _log.info("Live status updater: ENABLED (5s interval)")

        # Initialize dynamic tool visibility (hide optional tools until needed)
        initialize_tool_visibility()

        # Scan for other agents on this machine and register them
        _register_discovered_agents(port)

        # Start periodic agent scanning task
        asyncio.create_task(_agent_scan_loop(port))
        _log.info("Multi-agent scanner: ENABLED (10s interval)")

        # Start NetworkManager (discovers public URL, starts health loop + ping loop)
        await manager.start()

    # DarkMatter mesh protocol routes
    darkmatter_routes = [
        Route("/connection_request", handle_connection_request, methods=["POST"]),
        Route("/connection_accepted", handle_connection_accepted, methods=["POST"]),
        Route("/connection_proof", handle_connection_proof, methods=["POST"]),
        Route("/accept_pending", handle_accept_pending, methods=["POST"]),
        Route("/message", handle_message, methods=["POST"]),

        Route("/status", handle_status, methods=["GET"]),
        Route("/network_info", handle_network_info, methods=["GET"]),
        Route("/status_broadcast", handle_status_broadcast, methods=["POST"]),
        Route("/impression/{agent_id}", handle_impression_get, methods=["GET"]),
        Route("/webrtc_offer", handle_webrtc_offer, methods=["POST"]),
        Route("/peer_update", handle_peer_update, methods=["POST"]),
        Route("/peer_lookup/{agent_id}", handle_peer_lookup, methods=["GET"]),
        Route("/get_peers", handle_get_peers, methods=["GET", "POST"]),
        Route("/mesh_route", handle_mesh_route, methods=["POST"]),
        Route("/antimatter_match", handle_antimatter_match, methods=["POST"]),
        Route("/antimatter_signal", handle_antimatter_signal, methods=["POST"]),
        Route("/antimatter_result", handle_antimatter_result, methods=["POST"]),
        Route("/insight_push", handle_insight_push, methods=["POST"]),
        Route("/sdp_relay", handle_sdp_relay, methods=["POST"]),
        Route("/sdp_relay_deliver", handle_sdp_relay_deliver, methods=["POST"]),
        Route("/admin_connect", handle_admin_connect, methods=["POST"]),
        Route("/genome", handle_genome, methods=["GET"]),
        Route("/ping", handle_ping, methods=["POST"]),
        # Local API — for skill/curl access
        Route("/inbox", handle_local_inbox, methods=["GET"]),
        Route("/pending_requests", handle_local_pending, methods=["GET"]),
        Route("/connections", handle_local_connections, methods=["GET"]),
        Route("/set_impression", handle_local_set_impression, methods=["POST"]),
        Route("/config", handle_local_config, methods=["POST"]),
    ]

    # Extract the MCP ASGI handler and its session manager for lifecycle.
    # Identity is passport-based — agent_id = public key hex from .darkmatter/passport.key
    mcp_starlette = mcp.streamable_http_app()
    mcp_handler = mcp_starlette.routes[0].app  # StreamableHTTPASGIApp
    session_manager = mcp_handler.session_manager

    # Monkey-patch _handle_stateful_request to make session tasks fault-tolerant.
    # The MCP SDK uses a single anyio task group for ALL sessions — if one session's
    # run_server task raises, the ENTIRE server crashes. We wrap each run_server to
    # catch all exceptions so one session dying doesn't kill the others.
    _original_handle_stateful = session_manager._handle_stateful_request

    async def _resilient_handle_stateful(scope, receive, send):
        from starlette.requests import Request as _Request
        from mcp.server.streamable_http import MCP_SESSION_ID_HEADER

        request = _Request(scope, receive)
        request_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        # For existing sessions, delegate directly (no new task spawned)
        if request_session_id is not None and request_session_id in session_manager._server_instances:
            transport = session_manager._server_instances[request_session_id]
            await transport.handle_request(scope, receive, send)
            return

        if request_session_id is None:
            # New session — wrap run_server to be fault-tolerant
            async with session_manager._session_creation_lock:
                from uuid import uuid4 as _uuid4
                from mcp.server.streamable_http import StreamableHTTPServerTransport
                from anyio.abc import TaskStatus as _TaskStatus

                new_session_id = _uuid4().hex
                http_transport = StreamableHTTPServerTransport(
                    mcp_session_id=new_session_id,
                    is_json_response_enabled=session_manager.json_response,
                    event_store=session_manager.event_store,
                    security_settings=session_manager.security_settings,
                    retry_interval=session_manager.retry_interval,
                )
                assert http_transport.mcp_session_id is not None
                session_manager._server_instances[http_transport.mcp_session_id] = http_transport
                _log.info("New MCP session: %s...", new_session_id[:16])

                async def run_server_resilient(*, task_status: _TaskStatus[None] = anyio.TASK_STATUS_IGNORED):
                    try:
                        async with http_transport.connect() as streams:
                            read_stream, write_stream = streams
                            task_status.started()
                            try:
                                await session_manager.app.run(
                                    read_stream,
                                    write_stream,
                                    session_manager.app.create_initialization_options(),
                                    stateless=False,
                                )
                            except Exception as e:
                                _log.error("MCP session %s app.run error: %s", new_session_id[:16], e)
                    except BaseException as e:
                        # Catch EVERYTHING — prevent one session from killing the task group
                        _log.error("MCP session %s crashed: %s: %s", new_session_id[:16], type(e).__name__, e)
                    finally:
                        if (
                            http_transport.mcp_session_id
                            and http_transport.mcp_session_id in session_manager._server_instances
                            and not http_transport.is_terminated
                        ):
                            del session_manager._server_instances[http_transport.mcp_session_id]
                            _log.info("Cleaned up session %s", new_session_id[:16])

                assert session_manager._task_group is not None
                await session_manager._task_group.start(run_server_resilient)
                await http_transport.handle_request(scope, receive, send)
        else:
            # Unknown session ID
            from starlette.responses import Response as _Response
            response = _Response(
                '{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Session not found"}}',
                status_code=404,
                media_type="application/json",
            )
            await response(scope, receive, send)

    session_manager._handle_stateful_request = _resilient_handle_stateful

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Start MCP session manager + run our startup hooks
        async with session_manager.run():
            await on_startup()
            yield
            await manager.stop()

    # Build the app. Use redirect_slashes=False so POST /mcp doesn't get
    # redirected to /mcp/ (which breaks MCP client connections).
    # Dual-mount: agent-scoped routes first (more specific), then bare routes (default agent)
    app = Router(
        routes=[
            Route("/.well-known/darkmatter.json", handle_well_known, methods=["GET"]),
            Mount("/__darkmatter__/{target_agent_id}", routes=darkmatter_routes),
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
    """Log startup banner."""
    _log.info("Starting mesh protocol on http://localhost:%d", port)
    _log.info("MCP transport: %s", transport)
    _log.info("Discovery: %s", "ENABLED" if discovery_enabled else "disabled")
    _log.info("WebRTC: AVAILABLE")
    _log.info("UPnP: AVAILABLE")
    _log.info("Install: pip install dmagent | https://github.com/dadukhankevin/DarkMatter")


def check_port_owner(host: str, port: int, check_agent_id: str = None) -> Optional[str]:
    """Check if a port has a DarkMatter server and return its agent_id, or None if port is free.

    If check_agent_id is provided, also checks the `agents` array in the
    well-known response for a multi-tenant match (returns that agent_id if
    the daemon hosts it, even if it's not the primary agent).
    """
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
            primary_id = info.get("agent_id")

            # Multi-tenant: check if our agent is already hosted on this daemon
            if check_agent_id and check_agent_id != primary_id:
                agents = info.get("agents", [])
                for agent in agents:
                    if agent.get("agent_id") == check_agent_id:
                        return check_agent_id

            return primary_id
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

def _init_shared_stdio_session(port: int) -> None:
    """Initialize state + networking for a stdio MCP session that shares an HTTP mesh node."""
    init_state(port)

    manager = NetworkManager(state_getter=get_state, state_saver=save_state)
    manager.register_transport(HttpTransport())
    webrtc = WebRTCTransport()
    webrtc.set_message_dispatcher(dispatch_webrtc_message)
    manager.register_transport(webrtc)
    set_network_manager(manager)
    set_antimatter_network_fns(
        send_fn=manager.send,
        http_request_fn=manager.http_request,
    )


def _spawn_http_daemon(port: int) -> subprocess.Popen:
    """Spawn a detached HTTP-mode DarkMatter daemon for persistent discovery."""
    spawn_env = dict(os.environ)
    spawn_env["DARKMATTER_TRANSPORT"] = "http"
    spawn_env["DARKMATTER_PORT"] = str(port)
    spawn_env.pop("WERKZEUG_RUN_MAIN", None)

    daemon_log = os.path.join(os.path.expanduser("~"), ".darkmatter", "http_daemon.log")
    daemon_log_fh = open(daemon_log, "a")
    kwargs = {
        "cwd": os.getcwd(),
        "env": spawn_env,
        "stdin": subprocess.DEVNULL,
        "stdout": daemon_log_fh,
        "stderr": daemon_log_fh,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    return subprocess.Popen([sys.executable, "-m", "darkmatter"], **kwargs)


def _wait_for_our_server(host: str, port: int, expected_agent_id: str, timeout_s: float = 15.0) -> bool:
    """Wait until the HTTP mesh port is owned by our agent."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        owner = check_port_owner(host, port)
        if owner == expected_agent_id:
            return True
        time.sleep(0.25)
    return False

async def run_stdio_with_http() -> None:
    """Run MCP over stdio while serving HTTP mesh endpoints in the background.

    This is the preferred mode when launched by an MCP client (e.g. Claude Code).
    The client talks MCP over stdin/stdout. The HTTP server runs alongside for
    agent-to-agent mesh communication and discovery.

    Port conflict resolution:
    - Port free -> start normally
    - Port taken by OUR server (same agent_id) -> another session of us is
      already running the HTTP mesh. Run stdio-only and share state.
    - Port taken by SOMEONE ELSE -> find a new free port and start there.
    """
    from mcp.server.stdio import stdio_server

    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    host = os.environ.get("DARKMATTER_HOST", "0.0.0.0")

    # Load our passport to get our agent_id (if we have one)
    _priv, _pub = load_or_create_passport()
    our_agent_id = _pub

    # Check who owns the port (multi-tenant aware)
    port_owner = check_port_owner(host, port, check_agent_id=our_agent_id)

    if port_owner is None:
        # Port is free — promote the HTTP mesh node to a detached daemon so
        # discovery survives MCP client restarts, then attach this stdio
        # session to the shared on-disk state.
        _log.info("No HTTP mesh daemon detected on port %d; spawning a persistent daemon.", port)
        proc = _spawn_http_daemon(port)
        if proc.poll() is not None:
            raise RuntimeError("Failed to spawn persistent DarkMatter HTTP daemon")
        if not _wait_for_our_server(host, port, our_agent_id):
            raise RuntimeError(f"Persistent DarkMatter HTTP daemon did not become ready on port {port}")

        _log.info("Persistent HTTP mesh daemon is online on port %d.", port)
        _log.info("Running stdio-only MCP against shared state.")

        _init_shared_stdio_session(port)

        async with stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )

    elif port_owner == our_agent_id:
        # Our server is already running — parallel session, share state
        _log.info("Port %d is already running our server (agent %s...).", port, our_agent_id[:12])
        _log.info("Running stdio-only MCP (parallel session, shared state).")
        _init_shared_stdio_session(port)

        async with stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )

    else:
        # Port taken by a different agent — find a new port
        _log.info("Port %d is taken by another agent (%s...).", port,
                  port_owner[:12] if port_owner != "unknown" else "unknown")
        new_port = find_free_port(host, DEFAULT_PORT)
        _log.info("Using port %d instead.", new_port)

        # Override port for this session
        os.environ["DARKMATTER_PORT"] = str(new_port)
        _log.info("Spawning persistent daemon on alternate port %d.", new_port)
        proc = _spawn_http_daemon(new_port)
        if proc.poll() is not None:
            raise RuntimeError(f"Failed to spawn persistent DarkMatter HTTP daemon on port {new_port}")
        if not _wait_for_our_server(host, new_port, our_agent_id):
            raise RuntimeError(f"Persistent DarkMatter HTTP daemon did not become ready on port {new_port}")

        _init_shared_stdio_session(new_port)

        async with stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )


# =============================================================================
# Main entry point
# =============================================================================

def main() -> None:
    """Entry point — detect transport mode and run."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd == "install-mcp":
        from darkmatter.installer import main as installer_main
        raise SystemExit(installer_main(sys.argv[2:]))
    if cmd == "init-entrypoint":
        from darkmatter.entrypoint_init import init_entrypoint
        init_entrypoint(sys.argv[2:])
        return
    if cmd == "open-entrypoint":
        from darkmatter.entrypoint_init import open_entrypoint
        open_entrypoint()
        return

    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    transport = os.environ.get("DARKMATTER_TRANSPORT", "auto")

    # Auto-detect: if stdin is not a TTY, we're being launched by an MCP client
    use_stdio = transport == "stdio" or (transport == "auto" and not sys.stdin.isatty())

    if use_stdio:
        anyio.run(run_stdio_with_http)
    else:
        # Standalone HTTP mode (manual start, or DARKMATTER_TRANSPORT=http)
        # Enable MCP SDK debug logging to catch session crashes
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        logging.getLogger("mcp").setLevel(logging.DEBUG)
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.DEBUG)
        logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.DEBUG)

        # Install asyncio exception handler to catch unhandled task failures
        def _asyncio_exception_handler(loop, context):
            exc = context.get("exception")
            msg = context.get("message", "")
            if exc:
                _log.error("ASYNCIO UNHANDLED EXCEPTION: %s — %s: %s",
                           msg, type(exc).__name__, exc, exc_info=exc)
            else:
                _log.error("ASYNCIO UNHANDLED EXCEPTION: %s — context: %s", msg, context)

        loop = asyncio.new_event_loop()
        loop.set_exception_handler(_asyncio_exception_handler)
        asyncio.set_event_loop(loop)

        # Install signal trackers to log what triggers shutdown
        import signal as _signal
        for sig in (_signal.SIGTERM, _signal.SIGINT, _signal.SIGHUP):
            old_handler = _signal.getsignal(sig)
            def _sig_handler(signum, frame, _old=old_handler, _name=sig.name):
                _log.warning("RECEIVED SIGNAL %s (%d)", _name, signum)
                traceback.print_stack(frame, file=sys.stderr)
                if callable(_old) and _old not in (_signal.SIG_DFL, _signal.SIG_IGN):
                    _old(signum, frame)
                elif _old == _signal.SIG_DFL:
                    raise SystemExit(128 + signum)
            _signal.signal(sig, _sig_handler)

        app = create_app()
        discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
        print_startup_banner(port, "streamable-http", discovery_enabled)

        host = os.environ.get("DARKMATTER_HOST", "0.0.0.0")
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
