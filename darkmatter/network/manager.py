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
    HEALTH_CHECK_INTERVAL,
    HEALTH_FAILURE_THRESHOLD,
    STALE_CONNECTION_AGE,
    UPNP_PORT_RANGE,
    TRUST_NEGATIVE_TIMEOUT,
    CONNECTIVITY_UPGRADE_INTERVAL,
    PING_INTERVAL,
    PING_IP_WINDOW,
    PING_SILENCE_THRESHOLD,
)
from darkmatter.security import sign_peer_update
from darkmatter.network.transport import Transport, SendResult
from darkmatter.network.transports.http import strip_base_url


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
        devices = upnp.discover()
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
                print(f"[DarkMatter] UPnP port mapping attempt failed (port {ext_port}): {e}", file=sys.stderr)
                continue

        return None
    except Exception as e:
        print(f"[DarkMatter] UPnP mapping failed: {e}", file=sys.stderr)
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

    async def send(self, agent_id: str, path: str, payload: dict) -> SendResult:
        """Send a message to a peer, trying transports in priority order.

        Tries each available transport. On first success, resets health_failures
        and returns. On all failures, returns aggregate error.
        """
        state = self._get_state()
        conn = state.connections.get(agent_id) if state else None
        if conn is None:
            return SendResult(success=False, transport_name="none",
                              error=f"No connection to agent {agent_id[:12]}...")

        errors = []
        for transport in self._transports:
            if not transport.available:
                continue
            result = await transport.send(conn, path, payload)
            if result.success:
                conn.health_failures = 0
                return result
            errors.append(f"{transport.name}: {result.error}")

        return SendResult(
            success=False,
            transport_name="none",
            error="; ".join(errors) if errors else "No transports available",
        )

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
        """Return all current connections."""
        state = self._get_state()
        return state.connections if state else {}

    # -- HTTP request helper --

    async def http_request(
        self, url: str, method: str = "GET", timeout: float = 10.0, **kwargs
    ) -> "httpx.Response":
        """Make an HTTP request (used by antimatter and other subsystems)."""
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await getattr(client, method.lower())(url, **kwargs)

    # -- Peer resolution --

    def get_public_url(self) -> str:
        """Get the public URL for this agent."""
        state = self._get_state()
        if state is not None and state.public_url:
            return state.public_url
        public_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
        if public_url:
            return public_url
        port = state.port if state else 8100
        return f"http://localhost:{port}"

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
            print(f"[DarkMatter] Public URL (env): {env_url}", file=sys.stderr)
            return env_url

        result = await asyncio.to_thread(try_upnp_mapping, port)
        if result is not None:
            url, upnp_obj, ext_port = result
            if state is not None:
                state._upnp_mapping = (url, upnp_obj, ext_port)
            print(f"[DarkMatter] Public URL (UPnP): {url}", file=sys.stderr)
            return url

        # Use ping-observed IP if available
        if self._last_known_ip:
            url = f"http://{self._last_known_ip}:{port}"
            print(f"[DarkMatter] Public URL (ping): {url}", file=sys.stderr)
            return url

        url = f"http://localhost:{port}"
        print(f"[DarkMatter] Public URL (fallback): {url}", file=sys.stderr)
        return url

    async def broadcast_peer_update(self) -> None:
        """Notify all connected peers of our current URL, bio, and display name.

        Local peers receive our LAN URL (so they can reach us directly) while
        remote peers receive our public URL.
        """
        state = self._get_state()
        public_url = state.public_url or f"http://127.0.0.1:{state.port}"
        from darkmatter.network.discovery import _get_lan_ip
        lan_ip = _get_lan_ip()
        lan_url = f"http://{lan_ip}:{state.port}" if lan_ip != "localhost" else f"http://localhost:{state.port}"

        # Build transport address map
        addresses = {}
        for t in self._transports:
            if t.available:
                addr = t.get_address(state)
                if addr:
                    addresses[t.name] = addr

        def _build_payload(url_for_peer: str) -> dict:
            timestamp = datetime.now(timezone.utc).isoformat()
            p = {
                "agent_id": state.agent_id,
                "new_url": url_for_peer,
                "addresses": addresses,
                "timestamp": timestamp,
                "bio": state.bio,
                "display_name": state.display_name,
            }
            if state.public_key_hex:
                p["public_key_hex"] = state.public_key_hex
            if state.private_key_hex and state.public_key_hex:
                p["signature"] = sign_peer_update(
                    state.private_key_hex, state.agent_id, url_for_peer, timestamp
                )
            return p

        for conn in list(state.connections.values()):
            try:
                # Local peers get our LAN URL; remote peers get the public URL
                peer_url = lan_url if is_local_url(conn.agent_url) else public_url
                payload = _build_payload(peer_url)
                result = await self.send(
                    conn.agent_id, "/__darkmatter__/peer_update", payload)
                if not result.success:
                    print(f"[DarkMatter] Failed to notify {conn.agent_id[:12]}... of URL change: "
                          f"{result.error}", file=sys.stderr)
            except Exception as e:
                print(f"[DarkMatter] Failed to notify {conn.agent_id[:12]}... of URL change: {e}",
                      file=sys.stderr)

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
                print(f"[DarkMatter] Peer lookup: no impression for {peer_id[:12]}…, using default trust 0.5", file=sys.stderr)
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

        # Start background tasks
        self._tasks.append(asyncio.create_task(self._health_loop()))
        self._tasks.append(asyncio.create_task(self._connectivity_upgrade_loop()))
        self._tasks.append(asyncio.create_task(self._ping_loop()))
        print(f"[DarkMatter] Network health loop: ENABLED ({HEALTH_CHECK_INTERVAL}s interval)",
              file=sys.stderr)
        print(f"[DarkMatter] Connectivity upgrade loop: ENABLED ({CONNECTIVITY_UPGRADE_INTERVAL}s interval)",
              file=sys.stderr)
        print(f"[DarkMatter] Peer ping loop: ENABLED ({PING_INTERVAL}s interval)",
              file=sys.stderr)
        print(f"[DarkMatter] UPnP: AVAILABLE", file=sys.stderr)

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
                print(f"[DarkMatter] UPnP mapping removed (port {ext_port})", file=sys.stderr)
            except Exception as e:
                print(f"[DarkMatter] UPnP cleanup failed: {e}", file=sys.stderr)
            state._upnp_mapping = None

    # -- Background loops (internal) --

    async def _health_loop(self) -> None:
        """Periodically check connection health."""
        while True:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)

                # Connection health checks
                await self._check_connection_health()

                # Trust-based auto-disconnect
                await self._check_trust_disconnects()

                # Shard cache cleanup
                self._prune_stale_shards()

                # Update connectivity levels on all connections
                self.update_connectivity_levels()

            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[DarkMatter] Health loop error: {e}", file=sys.stderr)

    async def _check_connection_health(self) -> None:
        """Check health of all stale connections, attempt transport upgrades."""
        state = self._get_state()

        for conn in list(state.connections.values()):
            # Skip recently active connections
            if conn.last_activity:
                try:
                    last = datetime.fromisoformat(conn.last_activity.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - last).total_seconds()
                    if age < STALE_CONNECTION_AGE:
                        continue
                except Exception:
                    pass

            # Check reachability via all transports
            reachable = False
            for transport in self._transports:
                if not transport.available:
                    continue
                if await transport.is_reachable(conn):
                    reachable = True
                    conn.health_failures = 0
                    # Try to upgrade to a better transport
                    if conn.transport == "http":
                        webrtc = self.get_transport("webrtc")
                        if webrtc and webrtc.available:
                            asyncio.create_task(webrtc.upgrade(state, conn))
                    break

            if not reachable:
                conn.health_failures += 1
                if conn.health_failures >= HEALTH_FAILURE_THRESHOLD:
                    print(
                        f"[DarkMatter] Connection {conn.agent_id[:12]}... unhealthy "
                        f"({conn.health_failures} failures, url={conn.agent_url})",
                        file=sys.stderr,
                    )

    async def _check_trust_disconnects(self) -> None:
        """Auto-disconnect peers with sustained negative trust scores."""
        from darkmatter.wallet.antimatter import auto_disconnect_peer
        from darkmatter.state import save_state

        state = self._get_state()
        now = datetime.now(timezone.utc)
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
                    save_state()
            except Exception as e:
                print(f"[DarkMatter] Trust disconnect failed for {agent_id[:16]}...: {e}",
                      file=sys.stderr)

    def _prune_stale_shards(self) -> None:
        """Remove cached peer shards from disconnected peers or older than SHARD_CACHE_TTL."""
        from darkmatter.config import SHARD_CACHE_TTL
        from darkmatter.state import save_state

        state = self._get_state()
        now = datetime.now(timezone.utc)
        keep = []
        pruned = 0

        for shard in state.shared_shards:
            # Keep our own shards always
            if shard.author_agent_id == state.agent_id:
                keep.append(shard)
                continue

            # Prune if author is disconnected
            if shard.author_agent_id not in state.connections:
                pruned += 1
                continue

            # Prune if older than TTL
            try:
                updated = datetime.fromisoformat(shard.updated_at.replace("Z", "+00:00"))
                age = (now - updated).total_seconds()
                if age > SHARD_CACHE_TTL:
                    pruned += 1
                    continue
            except Exception:
                pass

            keep.append(shard)

        if pruned:
            state.shared_shards = keep
            save_state()
            print(f"[DarkMatter] Pruned {pruned} stale peer shard(s)", file=sys.stderr)

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
        """Update connectivity_level and connectivity_method on all connections."""
        state = self._get_state()
        if state is None:
            return
        for conn in state.connections.values():
            level, method = self.determine_connectivity_level(conn)
            conn.connectivity_level = level
            conn.connectivity_method = method

    # -- Connectivity upgrade loop --

    async def _connectivity_upgrade_loop(self) -> None:
        """Periodically try to upgrade connections to better connectivity levels."""
        while True:
            try:
                await asyncio.sleep(CONNECTIVITY_UPGRADE_INTERVAL)
                state = self._get_state()
                if state is None:
                    continue

                webrtc = self.get_transport("webrtc")
                if not webrtc or not webrtc.available:
                    continue

                for conn in list(state.connections.values()):
                    level, _ = self.determine_connectivity_level(conn)

                    # Already at best possible level
                    if level <= 2:
                        continue

                    # Try LAN signaling first (Level 2) if peer is on LAN
                    if conn.agent_id in state.discovered_peers:
                        from darkmatter.network.transports.webrtc import LANSignaling
                        success = await webrtc.upgrade(state, conn, LANSignaling())
                        if success:
                            conn._signaling_method = "lan"
                            lvl, meth = self.determine_connectivity_level(conn)
                            conn.connectivity_level = lvl
                            conn.connectivity_method = meth
                            peer = conn.agent_display_name or conn.agent_id[:12]
                            print(f"[DarkMatter] Upgraded {peer} to L{lvl}:{meth}", file=sys.stderr)
                            continue

                    # Try peer relay (Level 3) if we have mutual peers
                    if level > 3 and len(state.connections) > 1:
                        from darkmatter.network.transports.webrtc import PeerRelaySignaling
                        success = await webrtc.upgrade(state, conn, PeerRelaySignaling())
                        if success:
                            conn._signaling_method = "peer_relay"
                            lvl, meth = self.determine_connectivity_level(conn)
                            conn.connectivity_level = lvl
                            conn.connectivity_method = meth
                            peer = conn.agent_display_name or conn.agent_id[:12]
                            print(f"[DarkMatter] Upgraded {peer} to L{lvl}:{meth}", file=sys.stderr)

            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[DarkMatter] Connectivity upgrade loop error: {e}", file=sys.stderr)

    # -- Relay polling (SDP + messages) --

    # -- Peer Ping Loop --

    async def _ping_loop(self) -> None:
        """Periodically ping a random connected peer to detect IP changes.

        Each peer's ping response includes `your_ip` — the IP they see us on.
        We track these observations in a sliding window and broadcast a peer
        update if the majority IP changes.
        """
        while True:
            try:
                await asyncio.sleep(PING_INTERVAL)
                state = self._get_state()
                if state is None or not state.connections:
                    continue

                # Pick a random connected peer
                peers = list(state.connections.values())
                conn = random.choice(peers)
                base_url = strip_base_url(conn.agent_url)

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
                            data = resp.json()
                            observed_ip = data.get("your_ip")
                            if observed_ip and observed_ip != "unknown":
                                now = time.time()
                                self._observed_ips.append((now, observed_ip))

                                # Prune old observations
                                cutoff = now - PING_IP_WINDOW
                                while self._observed_ips and self._observed_ips[0][0] < cutoff:
                                    self._observed_ips.popleft()

                                # Check for majority IP
                                majority_ip = self._get_majority_ip()
                                if majority_ip and majority_ip != self._last_known_ip:
                                    old_ip = self._last_known_ip
                                    self._last_known_ip = majority_ip
                                    if old_ip is not None:
                                        print(f"[DarkMatter] Public IP changed (ping): "
                                              f"{old_ip} -> {majority_ip}", file=sys.stderr)
                                        state.public_url = await self.discover_public_url()
                                        await self.broadcast_peer_update()
                                    else:
                                        print(f"[DarkMatter] Public IP detected (ping): {majority_ip}",
                                              file=sys.stderr)

                            # Update last_activity on the connection
                            conn.last_activity = datetime.now(timezone.utc).isoformat()
                except Exception:
                    pass  # Best-effort — don't spam errors for pings

                # Check for inbound ping silence
                if (self._last_inbound_ping > 0 and
                        time.time() - self._last_inbound_ping > PING_SILENCE_THRESHOLD and
                        len(state.connections) > 0):
                    # No peers have pinged us in a while — possible network issue
                    pass  # Future: could trigger re-discovery

            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[DarkMatter] Ping loop error: {e}", file=sys.stderr)

    def _get_majority_ip(self) -> Optional[str]:
        """Return the most frequently observed IP in the sliding window, if it's a majority."""
        if not self._observed_ips:
            return None
        counts: dict[str, int] = {}
        for _, ip in self._observed_ips:
            counts[ip] = counts.get(ip, 0) + 1
        best_ip = max(counts, key=counts.get)
        # Require majority (>50% of observations)
        if counts[best_ip] > len(self._observed_ips) / 2:
            return best_ip
        return None
