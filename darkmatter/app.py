"""
Application composition root — create_app(), startup hooks, main entry point.

Process model (one agent per project):
- The HTTP daemon owns the agent: it loads the passport, owns all state,
  and is the ONLY process that writes the state file.
- MCP stdio sessions (spawned by Claude Code et al.) are thin clients that
  proxy every tool call to the daemon's loopback API.

This is the top-level module that wires everything together.
Depends on: everything (by design — this IS the composition root)
"""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import traceback
from typing import Optional
from uuid import uuid4

import anyio
import httpx
import uvicorn
from anyio.abc import TaskStatus
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER, StreamableHTTPServerTransport
from mcp.server.stdio import stdio_server
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Mount, Router

from darkmatter.config import (
    BOOTSTRAP_MODE,
    DEFAULT_PORT,
    DISCOVERY_PORT,
    DISCOVERY_MCAST_GROUP,
    DISCOVERY_LOCAL_PORTS,
    AGENT_ROUTER_MODE,
    NETWORK_TIER,
    MAX_CONNECTIONS,
)
from darkmatter.models import AgentState, AgentStatus
from darkmatter.names import generate_agent_name
from darkmatter.identity import load_or_create_passport
from darkmatter.state import (
    set_state, get_state, save_state, state_file_path,
    load_state_from_file, _is_pid_alive,
)
from darkmatter.mcp import mcp
import darkmatter.mcp.tools  # noqa: F401 — registers @mcp.tool() decorators
from darkmatter.mcp.client import set_daemon_port
from darkmatter.mcp.visibility import status_updater
from darkmatter.network.manager import NetworkManager, set_network_manager
from darkmatter.network.transports.http import HttpTransport
from darkmatter.network.transports.webrtc import WebRTCTransport
from darkmatter.network.discovery import (
    DiscoveryProtocol,
    discovery_loop,
    handle_well_known,
)
from darkmatter.network.access import check_access
from darkmatter.network.mesh import (
    dispatch_webrtc_message,
    handle_connection_request,
    handle_connection_accepted,
    handle_accept_pending,
    handle_message,
    handle_status,
    handle_local_agents,
    handle_network_info,
    handle_status_broadcast,
    handle_impression_get,
    handle_webrtc_offer,
    handle_peer_update,
    handle_peer_lookup,
    handle_get_peers,
    handle_mesh_route,
    handle_antimatter_request,
    handle_sdp_relay,
    handle_sdp_relay_deliver,
    handle_connection_proof,
    handle_ping,
)
from darkmatter.network.local_api import (
    handle_local_inbox,
    handle_inbox_consume,
    handle_inbox_wait,
    handle_local_send_message,
    handle_local_connect,
    handle_local_respond_pending,
    handle_local_disconnect,
    handle_local_pending,
    handle_local_connections,
    handle_local_discover,
    handle_local_config,
    handle_local_set_impression,
    handle_register_session,
    handle_local_context,
    handle_local_wallet,
    handle_local_send_payment,
    handle_send_proxy,
)
from darkmatter.wallet import get_all_providers
from darkmatter.extensions import crypto_enabled, load_crypto_extensions
from darkmatter.trust import set_network_fns as set_trust_network_fns
from darkmatter.installer import main as installer_main
from darkmatter.logging import get_logger

_log = get_logger("app")


def _guarded(route_name: str, handler):
    """Wrap a route handler with access control."""
    async def wrapper(request):
        denied = check_access(request, route_name, get_state())
        if denied is not None:
            return denied
        return await handler(request)
    wrapper.__name__ = handler.__name__
    return wrapper


def _wire_network_fns(manager: NetworkManager) -> None:
    """Wire optional subsystems to the active NetworkManager."""
    set_trust_network_fns(send_fn=manager.send)
    if load_crypto_extensions():
        from darkmatter.wallet.antimatter import set_network_fns as set_crypto_network_fns
        set_crypto_network_fns(
            send_fn=manager.send,
            http_request_fn=manager.http_request,
        )


# =============================================================================
# State initialization (daemon only)
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
        network_tier=NETWORK_TIER,
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
        restored.router_mode = AGENT_ROUTER_MODE  # From config — don't let stale state override
        restored.network_tier = NETWORK_TIER       # Env var overrides persisted value on boot
        if display_name:
            restored.display_name = display_name
        elif not restored.display_name:
            restored.display_name = generate_agent_name()
        set_state(restored)
        _log.info("Restored state (display: %s, %d connections)",
                  restored.display_name or "none", len(restored.connections))
    else:
        _log.info("Starting fresh (display: %s) on port %d", display_name or "none", port)

    _log.info("Identity: %s...%s", agent_id[:16], agent_id[-8:])

    # Derive wallets only when the optional crypto addon is enabled.
    state = get_state()
    crypto_loaded = load_crypto_extensions()
    if crypto_loaded and state.private_key_hex:
        for chain, provider in get_all_providers().items():
            # Allow env var override (e.g. DARKMATTER_WALLET_SOLANA) so bootstrap
            # operators can receive antimatter fees to their own wallet
            env_key = f"DARKMATTER_WALLET_{chain.upper()}"
            override = os.environ.get(env_key, "").strip()
            if override:
                state.wallets[chain] = override
                _log.info("%s wallet (override): %s", chain.capitalize(), override)
            else:
                state.wallets[chain] = provider.derive_address(state.private_key_hex)
                _log.info("%s wallet: %s", chain.capitalize(), state.wallets[chain])

    # Bootstrap mode: auto-accept all incoming connections
    if BOOTSTRAP_MODE:
        state = get_state()
        state.security_settings["auto_accept_all"] = True
        _log.info("Bootstrap mode: ENABLED (auto-accept all connections)")

    save_state()


# =============================================================================
# Identity attestation (on-chain passport proof)
# =============================================================================

async def _attestation_loop(state: AgentState) -> None:
    """Retry identity attestation every 60s until all chains are attested.

    Handles wallets funded after startup — keeps retrying until balance
    is available and attestation succeeds.
    """
    while True:
        all_attested = True
        for chain, provider in get_all_providers().items():
            address = state.wallets.get(chain)
            if not address:
                continue
            if chain in state.wallet_attestations:
                continue
            all_attested = False
            try:
                existing = await provider.verify_identity_attestation(address, state.agent_id)
                if existing["status"] == "match":
                    state.wallet_attestations[chain] = existing.get("timestamp", "verified")
                    save_state()
                    _log.info("Identity attestation exists on %s (since %s)", chain, existing.get("timestamp"))
                    continue
                balance = await provider.get_balance(address)
                if not balance.get("success") or balance.get("balance", 0) <= 0:
                    continue
                _log.info("Creating identity attestation on %s...", chain)
                result = await provider.attest_identity(
                    state.private_key_hex, state.wallets, state.agent_id,
                )
                if result.get("success"):
                    state.wallet_attestations[chain] = result["tx_signature"]
                    save_state()
                    _log.info("Identity attested on %s: tx %s", chain, result["tx_signature"])
            except Exception as e:
                _log.warning("Attestation check failed on %s: %s", chain, e)
        if all_attested:
            _log.info("All chains attested — stopping attestation loop")
            return
        await asyncio.sleep(60)


# =============================================================================
# Local auto-peering (one daemon per project, loopback connections)
# =============================================================================

# peer_id -> monotonic time of last attempt; avoids hammering local peers that
# decline or have auto-accept disabled.
_auto_peer_attempts: dict[str, float] = {}
_AUTO_PEER_RETRY_S = 300.0


async def _connect_local_peer(state: AgentState, peer_id: str, base_url: str) -> None:
    """Connect to a peer on another localhost daemon, auto-accepted over loopback."""
    from darkmatter.network.mesh import (
        build_outbound_request_payload,
        build_connection_from_accepted,
    )

    our_url = f"http://127.0.0.1:{state.port}"
    payload = build_outbound_request_payload(state, our_url)

    target = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{target}/__darkmatter__/connection_request", json=payload)
        result = resp.json()
    if result.get("auto_accepted"):
        state.connections[result["agent_id"]] = build_connection_from_accepted(result)
        save_state()
        _log.info("Auto-peered %s... -> %s... (loopback %s)",
                  state.agent_id[:8], peer_id[:8], target)


async def _auto_peer_local_agents(state: AgentState) -> None:
    """Connect this agent to every other localhost agent discovered via port scan.

    Idempotent and rate-limited: connected peers are skipped, declined peers
    are retried at most every _AUTO_PEER_RETRY_S seconds. Honors the
    `auto_peer_local` security setting (default on).
    """
    if not state.security_settings.get("auto_peer_local", True):
        return

    now = time.monotonic()
    wall_now = time.time()
    for peer_id, info in list(state.discovered_peers.items()):
        if peer_id == state.agent_id or peer_id in state.connections:
            continue
        if info.get("source") != "local":
            continue
        # Only chase peers seen recently — avoids hammering dead daemons.
        if wall_now - info.get("ts", 0) > 60:
            continue
        if now - _auto_peer_attempts.get(peer_id, -_AUTO_PEER_RETRY_S) < _AUTO_PEER_RETRY_S:
            continue
        if len(state.connections) >= MAX_CONNECTIONS:
            break
        base = info.get("url", "")
        if not base:
            continue
        _auto_peer_attempts[peer_id] = now
        try:
            await _connect_local_peer(state, peer_id, base)
        except Exception as e:
            _log.debug("Auto-peer %s... -> %s... failed: %s",
                       state.agent_id[:8], peer_id[:8], e)


async def _maintenance_loop() -> None:
    """Periodic daemon housekeeping: prune dead session PIDs, auto-peer locals."""
    while True:
        try:
            await asyncio.sleep(10)
            state = get_state()
            if state is None:
                continue
            if state.active_sessions:
                alive = [s for s in state.active_sessions if _is_pid_alive(s["pid"])]
                if len(alive) != len(state.active_sessions):
                    state.active_sessions = alive
                    save_state()
            await _auto_peer_local_agents(state)
        except asyncio.CancelledError:
            return
        except Exception as e:
            _log.error("Maintenance loop error: %s", e)


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
    set_daemon_port(port)  # In-daemon MCP sessions proxy to ourselves over loopback

    # Create and register NetworkManager with transport plugins
    manager = NetworkManager(state_getter=get_state, state_saver=save_state)
    manager.register_transport(HttpTransport())
    webrtc = WebRTCTransport()
    webrtc.set_message_dispatcher(dispatch_webrtc_message)
    manager.register_transport(webrtc)
    set_network_manager(manager)

    _wire_network_fns(manager)

    # LAN discovery setup
    discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"

    async def on_startup() -> None:
        state = get_state()

        if discovery_enabled:
            loop = asyncio.get_event_loop()

            # Multicast listener for LAN discovery (best-effort)
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.bind(("", DISCOVERY_PORT))
                mreq = struct.pack("4s4s",
                    socket.inet_aton(DISCOVERY_MCAST_GROUP),
                    socket.inet_aton("0.0.0.0"))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
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

        # Start live status updater (status file + inbox hygiene)
        asyncio.create_task(status_updater())
        _log.info("Live status updater: ENABLED (5s interval)")

        # Periodic housekeeping: session PID pruning + local auto-peering
        asyncio.create_task(_maintenance_loop())

        # Background: retry identity attestation until all crypto chains attested.
        if crypto_enabled() and state.private_key_hex and state.agent_id and get_all_providers():
            asyncio.create_task(_attestation_loop(state))

        # Start NetworkManager (discovers public URL, starts health loop + ping loop)
        await manager.start()

    # DarkMatter mesh protocol routes (wrapped with access control)
    darkmatter_routes = [
        # Public — discovery + connection establishment
        Route("/connection_request", _guarded("connection_request", handle_connection_request), methods=["POST"]),
        Route("/connection_accepted", _guarded("connection_accepted", handle_connection_accepted), methods=["POST"]),
        Route("/connection_proof", _guarded("connection_proof", handle_connection_proof), methods=["POST"]),
        Route("/accept_pending", _guarded("accept_pending", handle_accept_pending), methods=["POST"]),
        Route("/status", _guarded("status", handle_status), methods=["GET"]),
        Route("/local_agents", _guarded("local_agents", handle_local_agents), methods=["GET"]),

        # Peer — mesh protocol (connected peers + localhost)
        Route("/message", _guarded("message", handle_message), methods=["POST"]),
        Route("/status_broadcast", _guarded("status_broadcast", handle_status_broadcast), methods=["POST"]),
        Route("/peer_update", _guarded("peer_update", handle_peer_update), methods=["POST"]),
        Route("/ping", _guarded("ping", handle_ping), methods=["POST"]),
        Route("/webrtc_offer", _guarded("webrtc_offer", handle_webrtc_offer), methods=["POST"]),
        Route("/sdp_relay", _guarded("sdp_relay", handle_sdp_relay), methods=["POST"]),
        Route("/sdp_relay_deliver", _guarded("sdp_relay_deliver", handle_sdp_relay_deliver), methods=["POST"]),
        Route("/antimatter_request", _guarded("antimatter_request", handle_antimatter_request), methods=["POST"]),
        Route("/mesh_route", _guarded("mesh_route", handle_mesh_route), methods=["POST"]),
        Route("/network_info", _guarded("network_info", handle_network_info), methods=["GET"]),
        Route("/impression/{agent_id}", _guarded("impression", handle_impression_get), methods=["GET"]),
        Route("/peer_lookup/{agent_id}", _guarded("peer_lookup", handle_peer_lookup), methods=["GET"]),
        Route("/get_peers", _guarded("get_peers", handle_get_peers), methods=["GET", "POST"]),

        # Local — daemon API for MCP sessions and skills (localhost sockets only)
        Route("/inbox", _guarded("inbox", handle_local_inbox), methods=["GET"]),
        Route("/inbox/consume", _guarded("inbox_consume", handle_inbox_consume), methods=["POST"]),
        Route("/inbox/wait", _guarded("inbox_wait", handle_inbox_wait), methods=["POST"]),
        Route("/send_message", _guarded("send_message", handle_local_send_message), methods=["POST"]),
        Route("/connect", _guarded("connect", handle_local_connect), methods=["POST"]),
        Route("/respond_pending", _guarded("respond_pending", handle_local_respond_pending), methods=["POST"]),
        Route("/disconnect", _guarded("disconnect", handle_local_disconnect), methods=["POST"]),
        Route("/pending_requests", _guarded("pending_requests", handle_local_pending), methods=["GET"]),
        Route("/connections", _guarded("connections", handle_local_connections), methods=["GET"]),
        Route("/discover", _guarded("discover", handle_local_discover), methods=["POST"]),
        Route("/config", _guarded("config", handle_local_config), methods=["POST"]),
        Route("/set_impression", _guarded("set_impression", handle_local_set_impression), methods=["POST"]),
        Route("/register_session", _guarded("register_session", handle_register_session), methods=["POST"]),
        Route("/context", _guarded("context", handle_local_context), methods=["GET"]),
        Route("/wallet", _guarded("wallet", handle_local_wallet), methods=["GET"]),
        Route("/send_payment", _guarded("send_payment", handle_local_send_payment), methods=["POST"]),
        Route("/send_proxy", _guarded("send_proxy", handle_send_proxy), methods=["POST"]),
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
        request = Request(scope, receive)
        request_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        # For existing sessions, delegate directly (no new task spawned)
        if request_session_id is not None and request_session_id in session_manager._server_instances:
            transport = session_manager._server_instances[request_session_id]
            await transport.handle_request(scope, receive, send)
            return

        if request_session_id is None:
            # New session — wrap run_server to be fault-tolerant
            async with session_manager._session_creation_lock:
                new_session_id = uuid4().hex
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

                async def run_server_resilient(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED):
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
            response = Response(
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
    # When behind a reverse proxy that strips path prefixes (e.g. DO App
    # Platform), also mount routes at root so they're reachable.
    route_list = [
        Route("/.well-known/darkmatter.json", handle_well_known, methods=["GET"]),
        Mount("/__darkmatter__", routes=darkmatter_routes),
        Route("/mcp", mcp_handler),
    ]
    if os.environ.get("DARKMATTER_BOOTSTRAP_MODE", "").lower() == "true":
        # Duplicate routes at root for prefix-stripping reverse proxies
        route_list.extend(darkmatter_routes)

    app = Router(
        routes=route_list,
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


def check_port_owner(host: str, port: int) -> Optional[str]:
    """Check if a port has a DarkMatter server and return its agent_id, or None if port is free."""
    # First check if port is in use at all
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return None  # Port is free
        except OSError:
            pass  # Port in use — probe it

    # Port is taken — check if it's a DarkMatter node
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/.well-known/darkmatter.json", timeout=1.0)
        if resp.status_code == 200:
            return resp.json().get("agent_id")
    except Exception:
        pass
    return "unknown"  # Port taken by non-DarkMatter process


def find_free_port(host: str, start: int) -> int:
    """Find a free port in the discovery range (start to start+100)."""
    for port in range(start, start + 101):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free ports in range {start}-{start + 100}")


def bind_host_for_tier(tier: str) -> str:
    """Derive the daemon's bind address from its network tier.

    Tier enforcement starts at the socket: a localhost-tier agent is bound to
    the loopback interface, so no LAN or internet packet can ever reach it —
    no header or filter can change that. LAN/global tiers bind all interfaces
    and rely on socket-IP checks in the access layer.

    DARKMATTER_HOST overrides (explicit operator choice).
    """
    explicit = os.environ.get("DARKMATTER_HOST", "").strip()
    if explicit:
        return explicit
    if tier == "local":
        return "127.0.0.1"
    return "0.0.0.0"


# =============================================================================
# Stdio MCP session — thin client of the daemon
# =============================================================================

def _spawn_http_daemon(port: int) -> subprocess.Popen:
    """Spawn a detached HTTP-mode DarkMatter daemon for this project."""
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


def _wait_for_our_server(port: int, expected_agent_id: str, timeout_s: float = 15.0) -> bool:
    """Wait until the HTTP mesh port is owned by our agent."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        owner = check_port_owner("127.0.0.1", port)
        if owner == expected_agent_id:
            return True
        time.sleep(0.25)
    return False


def _persisted_port(public_key_hex: str) -> Optional[int]:
    """Read the agent's last-used port from its state file (read-only peek)."""
    import json
    from darkmatter.state import get_state_dir
    path = os.path.join(get_state_dir(), f"{public_key_hex}.json")
    try:
        with open(path, "r") as f:
            return int(json.load(f).get("port"))
    except Exception:
        return None


def _register_with_daemon(port: int) -> None:
    """Announce this stdio session to the daemon (best-effort)."""
    try:
        httpx.post(
            f"http://127.0.0.1:{port}/__darkmatter__/register_session",
            json={"pid": os.getpid(), "cwd": os.getcwd()},
            timeout=3.0,
        )
    except Exception as e:
        _log.warning("Could not register session with daemon: %s", e)


def _resolve_daemon_port(our_agent_id: str) -> int:
    """Find (or start) this project's daemon and return its port.

    Resolution order:
    1. The port persisted in our state file, if our daemon still owns it.
    2. DARKMATTER_PORT / the default port, if free or owned by us.
    3. Any free port in the range — spawn a fresh daemon there.
    """
    candidate_ports = []
    persisted = _persisted_port(our_agent_id)
    if persisted:
        candidate_ports.append(persisted)
    env_port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    if env_port not in candidate_ports:
        candidate_ports.append(env_port)

    for port in candidate_ports:
        owner = check_port_owner("127.0.0.1", port)
        if owner == our_agent_id:
            _log.info("Daemon already running on port %d.", port)
            return port
        if owner is None:
            _log.info("Spawning daemon on port %d.", port)
            os.environ["DARKMATTER_PORT"] = str(port)
            proc = _spawn_http_daemon(port)
            if proc.poll() is not None:
                raise RuntimeError("Failed to spawn DarkMatter daemon")
            if _wait_for_our_server(port, our_agent_id):
                return port
            raise RuntimeError(f"DarkMatter daemon did not become ready on port {port}")

    # All candidates taken by other agents — pick a fresh port
    port = find_free_port("127.0.0.1", DEFAULT_PORT)
    _log.info("Ports taken by other agents; spawning daemon on port %d.", port)
    os.environ["DARKMATTER_PORT"] = str(port)
    proc = _spawn_http_daemon(port)
    if proc.poll() is not None:
        raise RuntimeError("Failed to spawn DarkMatter daemon")
    if not _wait_for_our_server(port, our_agent_id):
        raise RuntimeError(f"DarkMatter daemon did not become ready on port {port}")
    return port


async def run_stdio_with_http() -> None:
    """Run MCP over stdio as a thin client of this project's daemon.

    This is the mode used when launched by an MCP client (e.g. Claude Code).
    The client talks MCP over stdin/stdout; every tool call proxies to the
    daemon's loopback API. This process never touches the state file.
    """
    # Load our passport to get our agent_id (creates one on first run)
    _priv, _pub = load_or_create_passport()
    our_agent_id = _pub

    port = _resolve_daemon_port(our_agent_id)
    set_daemon_port(port)
    _register_with_daemon(port)

    from darkmatter.mcp.pump import channel_pump
    pump_task = None

    async with stdio_server() as (read_stream, write_stream):
        pump_task = asyncio.get_event_loop().create_task(channel_pump(port))
        try:
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )
        finally:
            if pump_task:
                pump_task.cancel()


# =============================================================================
# Main entry point
# =============================================================================

def main() -> None:
    """Entry point — detect transport mode and run."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd == "install-mcp":
        raise SystemExit(installer_main(sys.argv[2:]))
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    transport = os.environ.get("DARKMATTER_TRANSPORT", "auto")

    # Auto-detect: if stdin is not a TTY, we're being launched by an MCP client
    use_stdio = transport == "stdio" or (transport == "auto" and not sys.stdin.isatty())

    if use_stdio:
        anyio.run(run_stdio_with_http)
    else:
        # Daemon mode (manual start, or DARKMATTER_TRANSPORT=http)
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
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old_handler = signal.getsignal(sig)
            def _sig_handler(signum, frame, _old=old_handler, _name=sig.name):
                _log.warning("RECEIVED SIGNAL %s (%d)", _name, signum)
                traceback.print_stack(frame, file=sys.stderr)
                if callable(_old) and _old not in (signal.SIG_DFL, signal.SIG_IGN):
                    _old(signum, frame)
                elif _old == signal.SIG_DFL:
                    raise SystemExit(128 + signum)
            signal.signal(sig, _sig_handler)

        app = create_app()
        discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
        print_startup_banner(port, "streamable-http", discovery_enabled)

        # Bind address follows the agent's network tier (socket-level enforcement)
        host = bind_host_for_tier(get_state().network_tier)
        _log.info("Network tier: %s (binding %s)", get_state().network_tier, host)
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
