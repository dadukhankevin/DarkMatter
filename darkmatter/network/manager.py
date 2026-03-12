"""
NetworkManager — central orchestrator for transport-agnostic networking.

Manages transport plugins, peer resolution, health monitoring, UPnP,
peer ping-based IP detection, and connectivity upgrades.

Depends on: config, models, identity, state, network/transport
"""

import asyncio
import ipaddress
import os
import random
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

from darkmatter.config import (
    PEER_LOOKUP_TIMEOUT,
    PEER_LOOKUP_MAX_CONCURRENT,
    HEALTH_FAILURE_THRESHOLD,
    HEALTH_DORMANT_THRESHOLD,
    HEALTH_DORMANT_RETRY_CYCLES,
    UPNP_PORT_RANGE,
    TRUST_NEGATIVE_TIMEOUT,
    MAINTENANCE_CYCLE_INTERVAL,
    PING_INTERVAL,
    PING_IP_WINDOW,
    PING_SILENCE_THRESHOLD,
    BOOTSTRAP_PEERS,
    BOOTSTRAP_MODE,
    BOOTSTRAP_RECONNECT_INTERVAL,
    BOOTSTRAP_RECONNECT_MAX,
)
from darkmatter.security import sign_peer_update
from darkmatter.network.transport import Transport, SendResult
from darkmatter.network.transports.http import strip_base_url
from darkmatter.logging import get_logger

_log = get_logger("manager")


# =============================================================================
# Global accessor (mirrors state.py pattern)
# =============================================================================

_network_manager: Optional["NetworkManager"] = None


def get_network_manager() -> "NetworkManager":
    """Get the global NetworkManager instance."""
    if _network_manager is None:
        raise RuntimeError("NetworkManager not initialized — call set_network_manager() first")
    return _network_manager


def set_network_manager(mgr: "NetworkManager") -> None:
    """Set the global NetworkManager instance."""
    global _network_manager
    _network_manager = mgr


# =============================================================================
# UPnP helpers (kept as module-level functions, called by manager)
# =============================================================================

def try_upnp_mapping(local_port: int) -> Optional[tuple]:
    """Try to create a UPnP port mapping. Returns (url, upnp_obj, ext_port) or None."""
    import random
    try:
        import miniupnpc
        upnp = miniupnpc.UPnP()
        upnp.discoverdelay = 2000
        try:
            devices = upnp.discover()
        except Exception as disc_err:
            # miniupnpc 2.3.x raises Exception("Success") on successful discovery
            if "success" in str(disc_err).lower():
                devices = 1
            else:
                raise
        if devices == 0:
            return None
        upnp.selectigd()
        external_ip = upnp.externalipaddress()
        if not external_ip:
            return None

        for _ in range(5):
            ext_port = random.randint(*UPNP_PORT_RANGE)
            try:
                upnp.addportmapping(
                    ext_port, "TCP", upnp.lanaddr, local_port,
                    "DarkMatter mesh", ""
                )
                url = f"http://{external_ip}:{ext_port}"
                return (url, upnp, ext_port)
            except Exception as e:
                _log.warning("UPnP port mapping attempt failed (port %s): %s", ext_port, e)
                continue

        return None
    except Exception as e:
        _log.error("UPnP mapping failed: %s", e)
        return None


# =============================================================================
# URL locality check
# =============================================================================

def is_local_url(url: str) -> bool:
    """Check if a URL points to a local/private address (localhost, LAN, etc.)."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback
    except (ValueError, TypeError):
        return False


# =============================================================================
# NetworkManager
# =============================================================================

class NetworkManager:
    """Central orchestrator for transport-agnostic networking.

    Manages transport plugins, peer resolution, health monitoring,
    NAT detection, UPnP, and peer URL recovery.
    """

    def __init__(self, state_getter: Callable, state_saver: Callable):
        self._get_state = state_getter
        self._save_state = state_saver
        self._transports: list[Transport] = []
        self._tasks: list[asyncio.Task] = []
        # Peer ping state
        self._observed_ips: deque = deque()  # (timestamp, ip) tuples
        self._last_known_ip: Optional[str] = None
        self._last_inbound_ping: float = 0.0

    # -- Transport registry --

    def register_transport(self, transport: Transport) -> None:
        """Register a transport plugin. Sorted by priority (lower = tried first)."""
        self._transports.append(transport)
        self._transports.sort(key=lambda t: t.priority)

    def get_transport(self, name: str) -> Optional[Transport]:
        """Get a registered transport by name."""
        for t in self._transports:
            if t.name == name:
                return t
        return None

    # -- Core API --

    # Paths that can be relayed through a mutual peer
    RELAYABLE = frozenset((
        "/__darkmatter__/message",
        "/__darkmatter__/status_broadcast",
        "/__darkmatter__/peer_update",
        "/__darkmatter__/insight_push",
    ))

    async def send(self, agent_id: str, path: str, payload: dict) -> SendResult:
        """Send to a peer. Direct first, then relay. Fast — no recovery in hot path."""
        conn = self._find_connection(agent_id)
        if conn is None:
            return SendResult(success=False, transport_name="none",
                              error=f"No connection to {agent_id[:12]}...")

        # 1. Direct send — try each transport
        result = await self._direct_send(conn, path, payload)
        if result.success:
            return result

        # 2. Relay through a mutual peer
        if path in self.RELAYABLE:
            relay = await self._relay_send(agent_id, path, payload)
            if relay.success:
                return relay

        return result  # Return the direct failure

    def _find_connection(self, agent_id: str):
        """Find a connection across all hosted agents."""
        from darkmatter.state import list_hosted_agents, get_state_for
        state = self._get_state()
        if state:
            conn = state.connections.get(agent_id)
            if conn:
                return conn
        for aid in list_hosted_agents():
            s = get_state_for(aid)
            if s and agent_id in s.connections:
                return s.connections[agent_id]
        return None

    async def _direct_send(self, conn, path: str, payload: dict) -> SendResult:
        """Try each transport in priority order. Return first success or last failure."""
        last = SendResult(success=False, transport_name="none", error="No transports")
        for transport in self._transports:
            if not transport.available:
                continue
            result = await transport.send(conn, path, payload)
            if result.success:
                conn.health_failures = 0
                return result
            last = result
        return last

    async def _relay_send(self, target_id: str, path: str, payload: dict) -> SendResult:
        """Relay a message through the best reachable mutual peer."""
        import uuid
        state = self._get_state()
        if state is None:
            return SendResult(success=False, transport_name="relay", error="No state")

        envelope = {
            "route_id": f"relay-{uuid.uuid4().hex[:12]}",
            "route_type": "message",
            "target_agent_id": target_id,
            "source_agent_id": state.agent_id,
            "hops_remaining": 5,
            "visited": [state.agent_id],
            "trust_chain": [{"agent_id": state.agent_id, "trust_to_next": 0.5}],
            "payload": payload,
            "original_path": path,
        }

        # Candidates: connected peers that aren't the target, sorted by trust
        candidates = sorted(
            [(aid, c) for aid, c in state.connections.items() if aid != target_id],
            key=lambda x: getattr(state.impressions.get(x[0]), "score", 0),
            reverse=True,
        )

        if not candidates:
            return SendResult(success=False, transport_name="relay", error="No relay candidates")

        _log.info("Relay: %d candidate(s) for %s...", len(candidates), target_id[:12])

        for relay_id, relay_conn in candidates:
            name = relay_conn.agent_display_name or relay_id[:12]
            result = await self._direct_send(relay_conn, "/__darkmatter__/mesh_route", envelope)
            if not result.success:
                _log.info("Relay via %s failed: %s", name, result.error)
                continue

            status = (result.response or {}).get("status", "")
            if status in ("delivered", "forwarded_direct", "forwarded"):
                _log.info("Relay to %s... via %s succeeded (%s)", target_id[:12], name, status)
                return SendResult(success=True, transport_name=f"relay:{name}", response=result.response)

            _log.info("Relay via %s: unexpected status '%s'", name, status)

        return SendResult(success=False, transport_name="relay", error="All relay candidates failed")

    def preferred_transport_for(self, agent_id: str):
        """Return the first currently-usable transport for a peer, or None."""
        state = self._get_state()
        conn = state.connections.get(agent_id) if state else None
        if conn is None:
            return None
        for transport in self._transports:
            if transport.available:
                return transport
        return None


    def peers(self) -> dict:
        """Return all current connections across all hosted agents."""
        from darkmatter.state import list_hosted_agents, get_state_for

        all_conns = {}
        for aid in list_hosted_agents():
            s = get_state_for(aid)
            if s:
                all_conns.update(s.connections)
        # Fallback for single-agent
        if not all_conns:
            state = self._get_state()
            if state:
                return state.connections
        return all_conns

    # -- HTTP request helper --

    async def http_request(
        self, url: str, method: str = "GET", timeout: float = 10.0, **kwargs
    ) -> "httpx.Response":
        """Make an HTTP request (used by antimatter and other subsystems)."""
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await getattr(client, method.lower())(url, **kwargs)

    # -- Peer resolution --

    def get_public_url(self, agent_id: str = None) -> str:
        """Get the public URL for an agent.

        If agent_id is provided, returns an agent-scoped URL
        (e.g. http://host:port/__darkmatter__/{agent_id}) so remote peers
        can route back to the correct local agent.

        If agent_id is None, returns the base URL.
        """
        state = self._get_state()
        base_url = None
        if state is not None and state.public_url:
            base_url = state.public_url
        if not base_url:
            base_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
        if not base_url:
            port = state.port if state else 8100
            base_url = f"http://localhost:{port}"

        if agent_id:
            return f"{base_url}/__darkmatter__/{agent_id}"
        return base_url

    async def discover_public_url(self) -> str:
        """Discover the best public URL for this agent.

        Priority: env var > UPnP > ping-observed IP > localhost fallback.
        If UPnP succeeds, stores the mapping on state._upnp_mapping
        so cleanup can remove it on shutdown.
        """
        state = self._get_state()
        port = state.port if state else 8100

        env_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
        if env_url:
            _log.info("Public URL (env): %s", env_url)
            return env_url

        result = await asyncio.to_thread(try_upnp_mapping, port)
        if result is not None:
            url, upnp_obj, ext_port = result
            if state is not None:
                state._upnp_mapping = (url, upnp_obj, ext_port)
            _log.info("Public URL (UPnP): %s", url)
            return url

        # Use ping-observed IP if available
        if self._last_known_ip:
            url = f"http://{self._last_known_ip}:{port}"
            _log.info("Public URL (ping): %s", url)
            return url

        url = f"http://localhost:{port}"
        _log.info("Public URL (fallback): %s", url)
        return url

    async def broadcast_peer_update(self, agent_id: str = None) -> None:
        """Notify all connected peers of our current URL, bio, and display name.

        If agent_id is specified, broadcast for that agent only.
        Otherwise broadcast for all hosted agents.

        Local peers receive our LAN URL (so they can reach us directly) while
        remote peers receive our public URL.
        """
        from darkmatter.state import list_hosted_agents, get_state_for

        if agent_id:
            agent_ids = [agent_id]
        else:
            agent_ids = list_hosted_agents()

        for aid in agent_ids:
            state = get_state_for(aid) if aid != (self._get_state() or object()).agent_id else self._get_state()
            if state is None:
                state = get_state_for(aid)
            if state is None:
                continue

            base_url = state.public_url or f"http://127.0.0.1:{state.port}"
            from darkmatter.network.discovery import _get_lan_ip
            lan_ip = _get_lan_ip()
            lan_base = f"http://{lan_ip}:{state.port}" if lan_ip != "localhost" else f"http://localhost:{state.port}"

            # Build transport address map
            addresses = {}
            for t in self._transports:
                if t.available:
                    addr = t.get_address(state)
                    if addr:
                        addresses[t.name] = addr

            def _build_payload(url_for_peer: str, _state=state) -> dict:
                timestamp = datetime.now(timezone.utc).isoformat()
                p = {
                    "agent_id": _state.agent_id,
                    "new_url": url_for_peer,
                    "addresses": addresses,
                    "timestamp": timestamp,
                    "bio": _state.bio,
                    "display_name": _state.display_name,
                    # LAN info for same-network peers (hairpin NAT workaround)
                    "lan_ip": lan_ip,
                    "local_port": _state.port,
                }
                if _state.public_key_hex:
                    p["public_key_hex"] = _state.public_key_hex
                if _state.private_key_hex and _state.public_key_hex:
                    p["signature"] = sign_peer_update(
                        _state.private_key_hex, _state.agent_id, url_for_peer, timestamp
                    )
                return p

            failed_peers = []
            for conn in list(state.connections.values()):
                try:
                    # Local peers get our LAN URL; remote peers get the public URL
                    peer_url = lan_base if is_local_url(conn.agent_url) else base_url
                    payload = _build_payload(peer_url)
                    result = await self.send(
                        conn.agent_id, "/__darkmatter__/peer_update", payload)
                    if not result.success:
                        _log.warning("Failed to notify %s... of URL change: %s", conn.agent_id[:12], result.error)
                        failed_peers.append((state, conn, _build_payload))
                except Exception as e:
                    _log.warning("Failed to notify %s... of URL change: %s", conn.agent_id[:12], e)
                    failed_peers.append((state, conn, _build_payload))

            if failed_peers:
                asyncio.create_task(self._retry_peer_update(failed_peers))

    async def _retry_peer_update(self, failed_peers: list) -> None:
        """Background retry for peers that failed during broadcast_peer_update.

        Attempts 3 retries with increasing backoff (5s, 15s, 30s).
        Before each retry, tries URL recovery via peer consensus.
        """
        backoffs = [5, 15, 30]
        remaining = list(failed_peers)

        for attempt, delay in enumerate(backoffs, 1):
            if not remaining:
                break
            await asyncio.sleep(delay)

            still_failed = []
            for state, conn, build_payload in remaining:
                peer_name = conn.agent_display_name or conn.agent_id[:12]
                try:
                    # Try to recover URL via mutual peers first
                    reconnected = await self._attempt_reconnect(state, conn)
                    if reconnected:
                        _log.info("Retry broadcast: recovered %s on attempt %d", peer_name, attempt)
                        continue

                    # Re-send peer_update with current URL
                    peer_url = conn.agent_url
                    payload = build_payload(peer_url, state)
                    result = await self.send(
                        conn.agent_id, "/__darkmatter__/peer_update", payload)
                    if result.success:
                        _log.info("Retry broadcast: notified %s on attempt %d", peer_name, attempt)
                    else:
                        _log.debug("Retry broadcast: %s still unreachable (attempt %d)", peer_name, attempt)
                        still_failed.append((state, conn, build_payload))
                except Exception as e:
                    _log.debug("Retry broadcast: %s failed (attempt %d): %s", peer_name, attempt, e)
                    still_failed.append((state, conn, build_payload))

            remaining = still_failed

        if remaining:
            names = [c.agent_display_name or c.agent_id[:12] for _, c, _ in remaining]
            _log.warning("Retry broadcast: gave up on %d peers: %s", len(remaining), ", ".join(names))

    async def lookup_peer_url(self, target_agent_id: str,
                              exclude_urls: Optional[set[str]] = None) -> Optional[str]:
        """Find an agent's current URL via peer consensus lookup."""
        if exclude_urls is None:
            exclude_urls = set()

        return await self._peer_consensus_lookup(target_agent_id, exclude_urls)

    async def _peer_consensus_lookup(self, target_agent_id: str,
                                      exclude_urls: set[str]) -> Optional[str]:
        """Fan out to connected peers, collect ALL responses, pick URL by trust-weighted consensus."""
        state = self._get_state()
        peers = [c for c in state.connections.values() if c.agent_id != target_agent_id]
        if not peers:
            return None

        peers = peers[:PEER_LOOKUP_MAX_CONCURRENT]

        async def _query(conn):
            try:
                base = strip_base_url(conn.agent_url)
                async with httpx.AsyncClient(timeout=PEER_LOOKUP_TIMEOUT) as client:
                    resp = await client.get(f"{base}/__darkmatter__/peer_lookup/{target_agent_id}")
                    if resp.status_code == 200:
                        data = resp.json()
                        url = data.get("url")
                        if url and url not in exclude_urls:
                            return (conn.agent_id, url)
            except Exception:
                pass
            return None

        results = await asyncio.gather(*[_query(p) for p in peers], return_exceptions=True)
        responses = [r for r in results if r is not None and not isinstance(r, Exception)]

        if not responses:
            return None

        # Group by URL, weight by trust
        url_scores: dict[str, float] = {}
        for peer_id, url in responses:
            imp = state.impressions.get(peer_id)
            if not imp:
                _log.warning("Peer lookup: no impression for %s\u2026, using default trust 0.5", peer_id[:12])
            weight = imp.score if imp else 0.5  # Default trust for unscored peers
            weight = max(weight, 0.1)  # Floor so even low-trust peers count
            url_scores[url] = url_scores.get(url, 0.0) + weight

        return max(url_scores, key=url_scores.get)

    # -- Lifecycle --

    async def start(self) -> None:
        """Start all transports, discover public URL, start background tasks."""
        state = self._get_state()

        # Start transports
        for transport in self._transports:
            if transport.available:
                await transport.start(state)

        # Wire up HttpTransport with peer lookup and public URL getter
        http_transport = self.get_transport("http")
        if http_transport is not None:
            http_transport._lookup_peer_url = self.lookup_peer_url
            http_transport._save_state = self._save_state
            http_transport._get_public_url = self.get_public_url

        # Discover public URL
        state.public_url = await self.discover_public_url()

        # Start background tasks — ping loop is the single heartbeat that drives
        # health detection, reconnection, IP tracking, and WebRTC upgrades
        self._tasks.append(asyncio.create_task(self._ping_loop()))
        self._tasks.append(asyncio.create_task(self._bootstrap_loop()))
        _log.info("Ping loop: ENABLED (%ss interval, drives health + upgrades)", PING_INTERVAL)
        if not BOOTSTRAP_MODE and BOOTSTRAP_PEERS:
            _log.info("Bootstrap loop: ENABLED (peers: %s)", ", ".join(BOOTSTRAP_PEERS))
        elif BOOTSTRAP_MODE:
            _log.info("Bootstrap mode: ACTIVE (this node is a bootstrap peer)")
        _log.info("UPnP: AVAILABLE")

        # Broadcast our current URL to all peers on startup (non-blocking)
        asyncio.create_task(self.broadcast_peer_update())

    async def stop(self) -> None:
        """Cancel background tasks, stop transports, cleanup UPnP."""
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        for transport in self._transports:
            await transport.stop()

        # UPnP cleanup
        state = self._get_state()
        if state is not None and getattr(state, '_upnp_mapping', None) is not None:
            url, upnp_obj, ext_port = state._upnp_mapping
            try:
                upnp_obj.deleteportmapping(ext_port, "TCP")
                _log.info("UPnP mapping removed (port %s)", ext_port)
            except Exception as e:
                _log.error("UPnP cleanup failed: %s", e)
            state._upnp_mapping = None

    # -- Background loops (internal) --

    async def _attempt_reconnect(self, state, conn) -> bool:
        """Try to reconnect to a peer via URL recovery and LAN discovery.

        Returns True if reconnection succeeded.
        """
        peer_name = conn.agent_display_name or conn.agent_id[:12]

        # 1. Try peer consensus URL recovery
        try:
            new_url = await self.lookup_peer_url(
                conn.agent_id, exclude_urls={conn.agent_url}
            )
            if new_url and new_url != conn.agent_url:
                old_url = conn.agent_url
                conn.agent_url = new_url
                if self._save_state:
                    self._save_state()
                _log.info("Reconnect: recovered URL for %s: %s -> %s", peer_name, old_url, new_url)

                # Verify the new URL is reachable
                for transport in self._transports:
                    if transport.available and await transport.is_reachable(conn):
                        conn.health_failures = 0
                        await self.broadcast_peer_update()
                        _log.info("Reconnect: %s is reachable at %s", peer_name, new_url)
                        return True
        except Exception as e:
            _log.debug("Reconnect URL recovery failed for %s: %s", peer_name, e)

        # 2. Check discovered_peers for LAN URL
        discovered = state.discovered_peers.get(conn.agent_id)
        if discovered:
            lan_url = discovered.get("url")
            if lan_url and lan_url != conn.agent_url:
                old_url = conn.agent_url
                conn.agent_url = lan_url
                if self._save_state:
                    self._save_state()
                _log.info("Reconnect: trying LAN URL for %s: %s -> %s", peer_name, old_url, lan_url)

                for transport in self._transports:
                    if transport.available and await transport.is_reachable(conn):
                        conn.health_failures = 0
                        await self.broadcast_peer_update()
                        _log.info("Reconnect: %s reachable via LAN at %s", peer_name, lan_url)
                        return True
                # LAN URL didn't work either — restore original
                conn.agent_url = old_url

        # 3. Try sending a connection_request to re-establish (if we found any URL)
        try:
            from darkmatter.network.mesh import build_outbound_request_payload, build_connection_from_accepted
            public_url = self.get_public_url(agent_id=state.agent_id)
            payload = build_outbound_request_payload(state, public_url)
            base = strip_base_url(conn.agent_url)

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{base}/__darkmatter__/connection_request",
                    json=payload,
                )
            if resp.status_code == 200:
                result_data = resp.json()
                if result_data.get("auto_accepted") or result_data.get("already_connected"):
                    conn.health_failures = 0
                    conn.last_activity = datetime.now(timezone.utc).isoformat()
                    _log.info("Reconnect: re-established connection to %s", peer_name)
                    return True
        except Exception as e:
            _log.debug("Reconnect request failed for %s: %s", peer_name, e)

        return False

    def _all_connections(self) -> list[tuple]:
        """Collect (state, conn) pairs across all hosted agents."""
        from darkmatter.state import list_hosted_agents, get_state_for

        all_conns = []
        for aid in list_hosted_agents():
            s = get_state_for(aid)
            if s:
                for conn in s.connections.values():
                    all_conns.append((s, conn))

        if not all_conns:
            state = self._get_state()
            if state:
                all_conns = [(state, c) for c in state.connections.values()]

        return all_conns

    async def _cleanup_dead_webrtc(self) -> None:
        """Detect dead WebRTC channels, clean up, and downgrade to HTTP.

        The connectivity upgrade loop will re-attempt WebRTC later.
        """
        from darkmatter.state import list_hosted_agents, get_state_for

        webrtc = self.get_transport("webrtc")
        if not webrtc:
            return

        for aid in list_hosted_agents():
            s = get_state_for(aid)
            if s is None:
                continue
            for conn in list(s.connections.values()):
                if conn.webrtc_pc is None:
                    continue
                pc_state = getattr(conn.webrtc_pc, "connectionState", "unknown")
                if pc_state in ("failed", "closed", "disconnected"):
                    peer = conn.agent_display_name or conn.agent_id[:12]
                    _log.info("WebRTC dead channel (%s) for %s, downgrading to HTTP",
                              pc_state, peer)
                    await webrtc.cleanup(conn)

    async def _check_trust_disconnects(self) -> None:
        """Auto-disconnect peers with sustained negative trust scores across all hosted agents."""
        from darkmatter.wallet.antimatter import auto_disconnect_peer
        from darkmatter.state import save_state, list_hosted_agents, get_state_for

        now = datetime.now(timezone.utc)

        for aid in list_hosted_agents():
            state = get_state_for(aid)
            if state is None:
                continue

            to_disconnect = []
            for agent_id, imp in list(state.impressions.items()):
                if agent_id not in state.connections:
                    continue
                if not imp.negative_since:
                    continue
                try:
                    neg_dt = datetime.fromisoformat(imp.negative_since)
                    elapsed = (now - neg_dt).total_seconds()
                    if elapsed >= TRUST_NEGATIVE_TIMEOUT:
                        to_disconnect.append(agent_id)
                except (ValueError, TypeError):
                    continue

            for agent_id in to_disconnect:
                try:
                    if await auto_disconnect_peer(state, agent_id):
                        save_state(agent_id=aid)
                except Exception as e:
                    _log.error("Trust disconnect failed for %s...: %s", agent_id[:16], e)

    def _prune_stale_insights(self) -> None:
        """Remove cached peer insights from disconnected peers or older than INSIGHT_CACHE_TTL."""
        from darkmatter.config import INSIGHT_CACHE_TTL
        from darkmatter.state import save_state, list_hosted_agents, get_state_for

        now = datetime.now(timezone.utc)

        for aid in list_hosted_agents():
            state = get_state_for(aid)
            if state is None:
                continue

            keep = []
            pruned = 0

            for insight in state.insights:
                if insight.author_agent_id == state.agent_id:
                    keep.append(insight)
                    continue
                if insight.author_agent_id not in state.connections:
                    pruned += 1
                    continue
                try:
                    updated = datetime.fromisoformat(insight.updated_at.replace("Z", "+00:00"))
                    age = (now - updated).total_seconds()
                    if age > INSIGHT_CACHE_TTL:
                        pruned += 1
                        continue
                except Exception:
                    pass
                keep.append(insight)

            if pruned:
                state.insights = keep
                save_state(agent_id=aid)
                _log.info("Pruned %s stale peer insight(s) for agent %s...", pruned, aid[:12])

    # -- Connectivity level --

    def determine_connectivity_level(self, conn) -> tuple[int, str]:
        """Determine the connectivity level for a connection.

        Returns (level, method_label):
            1 = direct (HTTP or WebRTC with direct signaling)
            2 = LAN WebRTC (WebRTC via LAN multicast signaling)
            3 = Peer-relayed WebRTC (WebRTC via mutual peer SDP relay)
            0 = unknown
        """
        signaling = getattr(conn, "_signaling_method", "")

        if conn.transport == "webrtc" and conn.webrtc_channel is not None:
            ready = getattr(conn.webrtc_channel, "readyState", None)
            if ready == "open":
                if signaling == "lan":
                    return 2, "lan-webrtc"
                elif signaling == "peer_relay":
                    return 3, "peer-relay"
                else:
                    return 1, "direct"

        if conn.transport == "http":
            return 1, "direct"

        return 0, "unknown"

    def update_connectivity_levels(self) -> None:
        """Update connectivity_level and connectivity_method on all connections across all agents."""
        from darkmatter.state import list_hosted_agents, get_state_for

        for aid in list_hosted_agents():
            state = get_state_for(aid)
            if state is None:
                continue
            for conn in state.connections.values():
                level, method = self.determine_connectivity_level(conn)
                conn.connectivity_level = level
                conn.connectivity_method = method

    # -- Connectivity upgrade loop --

    # -- Consolidated Ping Loop --
    #
    # Single heartbeat that drives everything: health detection, IP tracking,
    # reconnection, WebRTC upgrades, and periodic maintenance.
    # Pings one random peer every PING_INTERVAL (~1s).

    async def _ping_loop(self) -> None:
        """Ping a random peer every interval. This is the only background loop.

        On success: track IP, update last_activity, try WebRTC upgrade.
        On failure: increment health_failures, trigger reconnection.
        Periodically: cleanup dead WebRTC, trust disconnects, insight pruning.
        """
        cycle = 0

        while True:
            try:
                await asyncio.sleep(PING_INTERVAL)
                cycle += 1

                all_conns = self._all_connections()
                if not all_conns:
                    continue

                state, conn = random.choice(all_conns)
                is_dormant = getattr(conn, "_dormant", False)

                # Dormant peers: skip most pings, only retry periodically
                if is_dormant:
                    dormant_cycle = getattr(conn, "_dormant_cycle", 0) + 1
                    conn._dormant_cycle = dormant_cycle
                    if dormant_cycle % HEALTH_DORMANT_RETRY_CYCLES != 0:
                        continue
                    _log.info("Dormant retry for %s... (cycle %d)",
                              conn.agent_id[:12], dormant_cycle)

                # Ping the peer
                base_url = strip_base_url(conn.agent_url)
                ping_ok = False

                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.post(
                            f"{base_url}/__darkmatter__/ping",
                            json={
                                "agent_id": state.agent_id,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        if resp.status_code == 200:
                            ping_ok = True
                            data = resp.json()

                            # Track observed IP
                            observed_ip = data.get("your_ip")
                            if observed_ip and observed_ip != "unknown":
                                now = time.time()
                                self._observed_ips.append((now, observed_ip))
                                cutoff = now - PING_IP_WINDOW
                                while self._observed_ips and self._observed_ips[0][0] < cutoff:
                                    self._observed_ips.popleft()

                                majority_ip = self._get_majority_ip()
                                if majority_ip and majority_ip != self._last_known_ip:
                                    old_ip = self._last_known_ip
                                    self._last_known_ip = majority_ip
                                    if old_ip is not None:
                                        _log.info("Public IP changed: %s -> %s", old_ip, majority_ip)
                                    else:
                                        _log.info("Public IP detected: %s", majority_ip)
                                    # Always update URL and broadcast on IP change or first detection
                                    state.public_url = await self.discover_public_url()
                                    await self.broadcast_peer_update()

                            conn.last_activity = datetime.now(timezone.utc).isoformat()
                except Exception as e:
                    peer = conn.agent_display_name or conn.agent_id[:12]
                    _log.warning("Ping %s failed: %s", peer, e)

                if ping_ok:
                    # --- Ping succeeded ---
                    conn.health_failures = 0
                    if is_dormant:
                        conn._dormant = False
                        conn._dormant_cycle = 0
                        peer = conn.agent_display_name or conn.agent_id[:12]
                        _log.info("Peer %s recovered from dormant", peer)

                    # Try WebRTC upgrade if still on HTTP
                    if conn.transport == "http" and conn.webrtc_channel is None:
                        webrtc = self.get_transport("webrtc")
                        if webrtc and webrtc.available:
                            if conn.agent_id in state.discovered_peers:
                                from darkmatter.network.transports.webrtc import LANSignaling
                                asyncio.create_task(webrtc.upgrade(state, conn, LANSignaling()))
                            else:
                                asyncio.create_task(webrtc.upgrade(state, conn))
                elif is_dormant:
                    # --- Dormant ping failed — try URL recovery ---
                    peer = conn.agent_display_name or conn.agent_id[:12]
                    _log.info("Dormant peer %s ping failed, attempting reconnect via consensus", peer)
                    reconnected = await self._attempt_reconnect(state, conn)
                    if reconnected:
                        conn._dormant = False
                        conn._dormant_cycle = 0
                        conn.health_failures = 0
                        _log.info("Dormant peer %s recovered via consensus lookup", peer)

                else:
                    # --- Ping failed ---
                    conn.health_failures = getattr(conn, "health_failures", 0) + 1
                    peer = conn.agent_display_name or conn.agent_id[:12]

                    if (conn.health_failures >= HEALTH_FAILURE_THRESHOLD and
                            conn.health_failures % HEALTH_FAILURE_THRESHOLD == 0 and
                            conn.health_failures < HEALTH_DORMANT_THRESHOLD):
                        _log.warning("Peer %s unreachable (%d failures), reconnecting...",
                                     peer, conn.health_failures)
                        reconnected = await self._attempt_reconnect(state, conn)
                        if reconnected:
                            conn._dormant = False
                            conn._dormant_cycle = 0

                    elif conn.health_failures >= HEALTH_DORMANT_THRESHOLD and not is_dormant:
                        conn._dormant = True
                        conn._dormant_cycle = 0
                        _log.warning("Peer %s marked dormant (%d failures)", peer, conn.health_failures)

                # Inbound ping silence detection
                if (self._last_inbound_ping > 0 and
                        time.time() - self._last_inbound_ping > PING_SILENCE_THRESHOLD and
                        len(state.connections) > 0):
                    _log.info("Ping silence (>%ds) — re-discovering URL", PING_SILENCE_THRESHOLD)
                    try:
                        new_url = await self.discover_public_url()
                        if new_url != state.public_url:
                            _log.info("URL changed: %s -> %s", state.public_url, new_url)
                            state.public_url = new_url
                        await self.broadcast_peer_update()
                        for bp_url in BOOTSTRAP_PEERS:
                            try:
                                async with httpx.AsyncClient(timeout=5.0) as bc:
                                    await bc.get(f"{bp_url.rstrip('/')}/__darkmatter__/status")
                            except Exception:
                                _log.warning("Bootstrap %s unreachable", bp_url)
                    except Exception as e:
                        _log.error("Ping silence recovery failed: %s", e)
                    self._last_inbound_ping = time.time()

                # Periodic maintenance (every ~60s)
                if cycle % MAINTENANCE_CYCLE_INTERVAL == 0:
                    await self._cleanup_dead_webrtc()
                    await self._check_trust_disconnects()
                    self._prune_stale_insights()
                    self.update_connectivity_levels()

            except asyncio.CancelledError:
                return
            except Exception as e:
                _log.error("Ping loop error: %s", e)

    def _get_majority_ip(self) -> Optional[str]:
        """Return the most frequently observed IP in the sliding window, if it's a majority."""
        if not self._observed_ips:
            return None
        counts: dict[str, int] = {}
        for _, ip in self._observed_ips:
            counts[ip] = counts.get(ip, 0) + 1
        best_ip = max(counts, key=counts.get)
        if counts[best_ip] > len(self._observed_ips) / 2:
            return best_ip
        return None

    # -- Bootstrap loop --

    async def _bootstrap_loop(self) -> None:
        """Maintain connections to bootstrap peers with exponential backoff.

        Skips if this node is in bootstrap mode (to avoid connecting to itself)
        or if no bootstrap peers are configured.
        """
        if BOOTSTRAP_MODE or not BOOTSTRAP_PEERS:
            return

        backoff = {url: BOOTSTRAP_RECONNECT_INTERVAL for url in BOOTSTRAP_PEERS}
        first_run = True

        while True:
            try:
                # Connect quickly on first boot, then use normal backoff interval
                await asyncio.sleep(5 if first_run else min(backoff.values()))
                first_run = False

                state = self._get_state()
                if state is None:
                    continue

                # Collect all HEALTHY connected peer URLs across all hosted agents
                from darkmatter.state import list_hosted_agents, get_state_for
                healthy_urls = set()
                for aid in list_hosted_agents():
                    s = get_state_for(aid)
                    if s:
                        for conn in s.connections.values():
                            failures = getattr(conn, "health_failures", 0)
                            dormant = getattr(conn, "_dormant", False)
                            if failures < HEALTH_FAILURE_THRESHOLD and not dormant:
                                healthy_urls.add(strip_base_url(conn.agent_url))
                # Fallback for single-agent
                for conn in state.connections.values():
                    failures = getattr(conn, "health_failures", 0)
                    dormant = getattr(conn, "_dormant", False)
                    if failures < HEALTH_FAILURE_THRESHOLD and not dormant:
                        healthy_urls.add(strip_base_url(conn.agent_url))

                for bootstrap_url in BOOTSTRAP_PEERS:
                    base = bootstrap_url.rstrip("/")

                    # Skip if already connected AND healthy to this bootstrap peer
                    if base in healthy_urls:
                        backoff[bootstrap_url] = BOOTSTRAP_RECONNECT_INTERVAL
                        continue

                    try:
                        from darkmatter.network.mesh import (
                            build_outbound_request_payload,
                            build_connection_from_accepted,
                        )

                        public_url = self.get_public_url(agent_id=state.agent_id)
                        payload = build_outbound_request_payload(state, public_url)

                        async with httpx.AsyncClient(timeout=15.0) as client:
                            resp = await client.post(
                                f"{base}/__darkmatter__/connection_request",
                                json=payload,
                            )

                        if resp.status_code == 200:
                            result_data = resp.json()
                            if result_data.get("auto_accepted"):
                                peer_id = result_data["agent_id"]
                                if peer_id not in state.connections:
                                    conn = build_connection_from_accepted(result_data)
                                    # Override URL with the bootstrap URL we actually connected to
                                    # (the bootstrap may advertise localhost if PUBLIC_URL isn't set)
                                    conn.agent_url = base
                                    state.connections[peer_id] = conn
                                    # Mark as infrastructure peer — exempt from reciprocity scaling
                                    from darkmatter.models import Impression
                                    imp = state.impressions.get(peer_id, Impression(score=0.0))
                                    imp.infrastructure = True
                                    state.impressions[peer_id] = imp
                                    self._save_state()
                                    _log.info(
                                        "Bootstrap: connected to %s (%s)",
                                        result_data.get("agent_display_name", peer_id[:12]),
                                        bootstrap_url,
                                    )
                                backoff[bootstrap_url] = BOOTSTRAP_RECONNECT_INTERVAL
                            else:
                                _log.info("Bootstrap: connection request pending at %s", bootstrap_url)
                                backoff[bootstrap_url] = min(
                                    backoff[bootstrap_url] * 2, BOOTSTRAP_RECONNECT_MAX
                                )
                        else:
                            _log.warning(
                                "Bootstrap: %s returned %d", bootstrap_url, resp.status_code
                            )
                            backoff[bootstrap_url] = min(
                                backoff[bootstrap_url] * 2, BOOTSTRAP_RECONNECT_MAX
                            )

                    except Exception as e:
                        _log.warning("Bootstrap: failed to connect to %s: %s", bootstrap_url, e)
                        backoff[bootstrap_url] = min(
                            backoff[bootstrap_url] * 2, BOOTSTRAP_RECONNECT_MAX
                        )

            except asyncio.CancelledError:
                return
            except Exception as e:
                _log.error("Bootstrap loop error: %s", e)
