"""
NetworkManager — central orchestrator for transport-agnostic networking.

Absorbs from resilience.py: discover_public_url, check_nat_status, broadcast_peer_update,
network_health_loop, check_connection_health, poll_webhook_relay, lookup_peer_url,
webhook_request_with_recovery, build_webhook_url, get_public_url, UPnP functions.

Depends on: config, models, identity, state, network/transport
"""

import asyncio
import ipaddress
import os
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

from darkmatter.config import (
    ANCHOR_NODES,
    ANCHOR_LOOKUP_TIMEOUT,
    PEER_LOOKUP_TIMEOUT,
    PEER_LOOKUP_MAX_CONCURRENT,
    HEALTH_CHECK_INTERVAL,
    HEALTH_FAILURE_THRESHOLD,
    STALE_CONNECTION_AGE,
    IP_CHECK_INTERVAL,
    UPNP_PORT_RANGE,
    WEBHOOK_RECOVERY_MAX_ATTEMPTS,
    WEBHOOK_RECOVERY_TIMEOUT,
    TRUST_NEGATIVE_TIMEOUT,
    CONNECTIVITY_UPGRADE_INTERVAL,
    MESSAGE_RELAY_POLL_INTERVAL,
    SDP_RELAY_TIMEOUT,
)
from darkmatter.identity import (
    validate_webhook_url,
)
from darkmatter.security import sign_peer_update, sign_relay_poll
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
    except (ImportError, OSError) as e:
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
    NAT detection, UPnP, webhook recovery, and anchor node communication.
    """

    def __init__(self, state_getter: Callable, state_saver: Callable):
        self._get_state = state_getter
        self._save_state = state_saver
        self._transports: list[Transport] = []
        self._tasks: list[asyncio.Task] = []
        self._last_working_anchor: Optional[str] = None
        self._process_webhook_fn: Optional[Callable] = None
        self._process_connection_relay_fn: Optional[Callable] = None
        self._process_relayed_message_fn: Optional[Callable] = None

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

    def peers(self) -> dict:
        """Return all current connections."""
        state = self._get_state()
        return state.connections if state else {}

    # -- Webhook helpers --

    async def webhook_request(
        self, webhook_url: str, from_agent_id: Optional[str],
        method: str = "POST", timeout: float = 30.0, **kwargs
    ) -> "httpx.Response":
        """Make an HTTP request to a webhook URL, with peer lookup recovery on failure."""
        deadline = time.monotonic() + WEBHOOK_RECOVERY_TIMEOUT
        current_url = webhook_url
        last_err: Optional[Exception] = None

        try:
            remaining = max(1.0, deadline - time.monotonic())
            async with httpx.AsyncClient(timeout=min(timeout, remaining)) as client:
                return await getattr(client, method.lower())(current_url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e

        if not from_agent_id:
            raise last_err

        urls_tried = {current_url}

        for attempt in range(1, WEBHOOK_RECOVERY_MAX_ATTEMPTS + 1):
            if time.monotonic() >= deadline:
                break

            new_base = await self.lookup_peer_url(from_agent_id, exclude_urls=urls_tried)
            if not new_base:
                print(f"[DarkMatter] Webhook recovery: no alternative URL found for {from_agent_id[:12]}… (attempt {attempt})", file=sys.stderr)
                break

            parsed = urlparse(current_url if attempt == 1 else webhook_url)
            path = parsed.path
            if not (path.startswith("/__darkmatter__/webhook/") or
                    path.startswith("/__darkmatter__/webhook_relay/")):
                print(f"[DarkMatter] Webhook recovery: unexpected path {path}, aborting", file=sys.stderr)
                break

            new_base = strip_base_url(new_base)
            new_webhook = f"{new_base}{path}"

            if new_webhook in urls_tried:
                print(f"[DarkMatter] Webhook recovery: already tried {new_webhook}, aborting", file=sys.stderr)
                break
            urls_tried.add(new_webhook)

            err = validate_webhook_url(
                new_webhook, get_state_fn=self._get_state,
                get_public_url_fn=lambda port: self.get_public_url(),
            )
            if err:
                print(f"[DarkMatter] Webhook recovery: validation failed for {new_webhook}: {err}", file=sys.stderr)
                break

            print(f"[DarkMatter] Webhook recovery: {webhook_url} -> {new_webhook} "
                  f"(attempt {attempt}/{WEBHOOK_RECOVERY_MAX_ATTEMPTS})", file=sys.stderr)

            try:
                remaining = max(1.0, deadline - time.monotonic())
                async with httpx.AsyncClient(timeout=min(timeout, remaining)) as client:
                    return await getattr(client, method.lower())(new_webhook, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_err = e
                continue

        raise last_err

    def build_webhook_url(self, message_id: str, peer_url: str = None) -> str:
        """Build the webhook URL, using anchor relay if behind NAT.

        If peer_url is local (private IP / localhost / LAN), bypass the anchor
        relay and use a direct URL the peer can reach us on.
        """
        state = self._get_state()
        if state.nat_detected and ANCHOR_NODES:
            # Local peers don't need anchor relay
            if peer_url and is_local_url(peer_url):
                # Determine the right host for the webhook:
                # - Same machine (localhost/127.x) → use localhost
                # - LAN peer (10.x, 192.168.x, etc.) → use our LAN IP
                try:
                    peer_host = urlparse(peer_url).hostname or ""
                    if peer_host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
                        webhook_host = "localhost"
                    else:
                        webhook_host = self._get_lan_ip()
                except Exception:
                    webhook_host = "localhost"
                return f"http://{webhook_host}:{state.port}/__darkmatter__/webhook/{message_id}"
            anchor = self.get_active_anchor()
            return f"{anchor}/__darkmatter__/webhook_relay/{state.agent_id}/{message_id}"
        return f"{self.get_public_url()}/__darkmatter__/webhook/{message_id}"

    @staticmethod
    def _get_lan_ip() -> str:
        """Get the LAN IP address of this machine."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "localhost"

    # -- Peer resolution --

    def get_active_anchor(self) -> str:
        """Return the last known working anchor, or the first configured anchor."""
        if self._last_working_anchor and self._last_working_anchor in ANCHOR_NODES:
            return self._last_working_anchor
        return ANCHOR_NODES[0] if ANCHOR_NODES else ""

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

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("https://api.ipify.org?format=json")
                if resp.status_code == 200:
                    ip = resp.json().get("ip")
                    if ip:
                        url = f"http://{ip}:{port}"
                        print(f"[DarkMatter] Public URL (ipify): {url}", file=sys.stderr)
                        return url
        except Exception as e:
            print(f"[DarkMatter] ipify lookup failed: {e}", file=sys.stderr)

        url = f"http://localhost:{port}"
        print(f"[DarkMatter] Public URL (fallback): {url}", file=sys.stderr)
        return url

    async def check_nat_status(self, public_url: str) -> bool:
        """Check if we're behind NAT by asking an anchor to probe our public URL.

        The old self-test (hitting our own public IP) gave false negatives
        because NAT hairpinning lets the request succeed locally even when
        external clients can't reach us.  Now we ask the anchor to try.
        Falls back to self-test if no anchor is available.
        """
        if "localhost" in public_url or "127.0.0.1" in public_url:
            return True

        # Ask anchor to probe our public URL
        for anchor in ANCHOR_NODES:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        f"{anchor}/__darkmatter__/probe_reachability",
                        json={"url": f"{public_url}/__darkmatter__/status"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        reachable = data.get("reachable", False)
                        print(f"[DarkMatter] Anchor reachability probe: {'reachable' if reachable else 'UNREACHABLE'}", file=sys.stderr)
                        return not reachable
            except Exception:
                pass  # Anchor doesn't support probe yet, fall back

        # Fallback: compare local LAN IP vs public URL IP
        # If they differ, we're likely behind NAT
        try:
            from urllib.parse import urlparse
            import socket
            parsed = urlparse(public_url)
            public_host = parsed.hostname
            # Get actual LAN IP by connecting to an external address (no data sent)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
            finally:
                s.close()
            if public_host and local_ip and public_host != local_ip:
                print(f"[DarkMatter] NAT inferred: local={local_ip} != public={public_host}", file=sys.stderr)
                return True
        except Exception:
            pass

        # Last resort: self-test (unreliable due to hairpinning)
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{public_url}/__darkmatter__/status")
                return resp.status_code != 200
        except Exception:
            return True

    async def broadcast_peer_update(self) -> None:
        """Notify all connected peers and anchor nodes of our current URL, bio, and display name.

        Local peers receive our LAN URL (so they can reach us directly) while
        remote peers and anchors receive our public URL.
        """
        state = self._get_state()
        public_url = state.public_url or f"http://127.0.0.1:{state.port}"
        lan_ip = self._get_lan_ip()
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

        for anchor_url in ANCHOR_NODES:
            try:
                payload = _build_payload(public_url)
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(f"{anchor_url}/__darkmatter__/peer_update", json=payload)
            except Exception as e:
                print(f"[DarkMatter] Failed to notify anchor {anchor_url} of URL: {e}",
                      file=sys.stderr)

    async def lookup_peer_url(self, target_agent_id: str,
                              exclude_urls: Optional[set[str]] = None) -> Optional[str]:
        """Find an agent's current URL — peers first (trust-weighted consensus), anchors as fallback."""
        if exclude_urls is None:
            exclude_urls = set()

        # 1. Try peer consensus first — peers ARE the mesh
        result = await self._peer_consensus_lookup(target_agent_id, exclude_urls)
        if result:
            return result

        # 2. Fall back to anchor nodes only if peers failed
        return await self._anchor_lookup(target_agent_id, exclude_urls)

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

    async def _anchor_lookup(self, target_agent_id: str,
                              exclude_urls: set[str]) -> Optional[str]:
        """Query anchor nodes for an agent's URL (infrastructure fallback)."""
        if not ANCHOR_NODES:
            return None

        tasks = [asyncio.create_task(self._query_anchor(a, target_agent_id))
                 for a in ANCHOR_NODES]
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=ANCHOR_LOOKUP_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = task.result()
                if result is not None and result not in exclude_urls:
                    for t in pending:
                        t.cancel()
                    return result
            if pending:
                done2, pending2 = await asyncio.wait(pending, timeout=0.5)
                for task in done2:
                    result = task.result()
                    if result is not None and result not in exclude_urls:
                        for t in pending2:
                            t.cancel()
                        return result
                for t in pending2:
                    t.cancel()
        except Exception:
            for t in tasks:
                t.cancel()
        return None

    # -- Lifecycle --

    def set_process_webhook_fn(self, fn: Callable) -> None:
        """Set the webhook processing callback (injected to avoid circular imports)."""
        self._process_webhook_fn = fn

    def set_process_connection_relay_fn(self, fn: Callable) -> None:
        """Set the connection relay processing callback (injected to avoid circular imports)."""
        self._process_connection_relay_fn = fn

    def set_process_relayed_message_fn(self, fn: Callable) -> None:
        """Set the relayed message processing callback (Level 5 anchor relay)."""
        self._process_relayed_message_fn = fn

    async def start(self) -> None:
        """Start all transports, discover public URL, detect NAT, start background tasks."""
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

        # Discover public URL and detect NAT
        state.public_url = await self.discover_public_url()
        state.nat_detected = await self.check_nat_status(state.public_url)
        if state.nat_detected:
            print(f"[DarkMatter] NAT detected: True — using anchor webhook relay", file=sys.stderr)

        # Start background tasks
        self._tasks.append(asyncio.create_task(self._health_loop()))
        self._tasks.append(asyncio.create_task(self._connectivity_upgrade_loop()))
        print(f"[DarkMatter] Network health loop: ENABLED ({HEALTH_CHECK_INTERVAL}s interval)",
              file=sys.stderr)
        print(f"[DarkMatter] Connectivity upgrade loop: ENABLED ({CONNECTIVITY_UPGRADE_INTERVAL}s interval)",
              file=sys.stderr)
        print(f"[DarkMatter] UPnP: AVAILABLE", file=sys.stderr)

        # Register with anchor nodes
        if ANCHOR_NODES and state.public_url:
            await self.broadcast_peer_update()
            print(f"[DarkMatter] Anchor nodes: registered with {len(ANCHOR_NODES)} anchor(s)",
                  file=sys.stderr)
        elif ANCHOR_NODES:
            print(f"[DarkMatter] Anchor nodes: configured but no public URL yet", file=sys.stderr)

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
        """Periodically check connection health and detect IP changes."""
        last_ip_check = 0.0
        last_known_ip = None

        while True:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                now = time.time()

                # IP change detection
                if now - last_ip_check >= IP_CHECK_INTERVAL:
                    last_ip_check = now
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            resp = await client.get("https://api.ipify.org?format=json")
                            if resp.status_code == 200:
                                current_ip = resp.json().get("ip")
                                if last_known_ip is None:
                                    last_known_ip = current_ip
                                elif current_ip != last_known_ip:
                                    print(f"[DarkMatter] Public IP changed: "
                                          f"{last_known_ip} -> {current_ip}", file=sys.stderr)
                                    last_known_ip = current_ip
                                    state = self._get_state()
                                    state.public_url = await self.discover_public_url()
                                    await self.broadcast_peer_update()
                    except Exception:
                        pass

                # Connection health checks
                await self._check_connection_health()

                # Trust-based auto-disconnect
                await self._check_trust_disconnects()

                # Shard cache cleanup
                self._prune_stale_shards()

                # NAT relay polling
                state = self._get_state()
                if state.nat_detected and ANCHOR_NODES and state.private_key_hex:
                    await self._poll_webhook_relay()
                    await self._poll_sdp_relay()
                    await self._poll_message_relay()

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

    async def _poll_webhook_relay(self) -> None:
        """Poll anchor nodes for buffered webhook callbacks (NAT relay)."""
        state = self._get_state()
        ts = datetime.now(timezone.utc).isoformat()
        sig = sign_relay_poll(state.private_key_hex, state.agent_id, ts)

        ordered = list(ANCHOR_NODES)
        if self._last_working_anchor and self._last_working_anchor in ordered:
            ordered.remove(self._last_working_anchor)
            ordered.insert(0, self._last_working_anchor)

        for anchor in ordered:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{anchor}/__darkmatter__/webhook_relay_poll/{state.agent_id}",
                        params={"signature": sig, "timestamp": ts},
                    )
                    if resp.status_code != 200:
                        continue
                    self._last_working_anchor = anchor
                    data = resp.json()
                    callbacks = data.get("callbacks", [])
                    for cb in callbacks:
                        msg_id = cb.get("message_id", "")
                        cb_data = cb.get("data", {})
                        if msg_id and cb_data and self._process_webhook_fn:
                            result, _ = self._process_webhook_fn(state, msg_id, cb_data)
                            if result.get("success"):
                                print(f"[DarkMatter] Relay: processed webhook for {msg_id}",
                                      file=sys.stderr)

                    # Poll for connection relay callbacks
                    if self._process_connection_relay_fn:
                        resp2 = await client.get(
                            f"{anchor}/__darkmatter__/connection_relay_poll/{state.agent_id}",
                            params={"signature": sig, "timestamp": ts},
                        )
                        if resp2.status_code == 200:
                            data2 = resp2.json()
                            for cb_data in data2.get("callbacks", []):
                                if isinstance(cb_data, dict) and cb_data.get("agent_id"):
                                    self._process_connection_relay_fn(state, cb_data)
                                    print(f"[DarkMatter] Relay: connection accepted by {cb_data['agent_id'][:12]}...",
                                          file=sys.stderr)
                    return
            except Exception as e:
                print(f"[DarkMatter] Relay poll error ({anchor}): {e}", file=sys.stderr)
                continue

    # -- Connectivity level --

    def determine_connectivity_level(self, conn) -> tuple[int, str]:
        """Determine the connectivity level for a connection.

        Returns (level, method_label):
            1 = direct (HTTP or WebRTC with direct signaling)
            2 = LAN WebRTC (WebRTC via LAN multicast signaling)
            3 = Peer-relayed WebRTC (WebRTC via mutual peer SDP relay)
            4 = Anchor-relayed WebRTC (WebRTC via anchor SDP relay)
            5 = Anchor message relay (all messages through anchor)
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
                elif signaling == "anchor_relay":
                    return 4, "anchor-relay"
                else:
                    return 1, "direct"

        if conn.transport == "http":
            # Check if we're actually reaching this peer via anchor relay
            anchor_transport = self.get_transport("anchor_relay")
            if anchor_transport:
                # If HTTP is working directly, it's level 1
                return 1, "direct"

        if conn.transport == "anchor_relay":
            return 5, "anchor-msg-relay"

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
                            continue

                    # Try anchor relay signaling (Level 4) if available
                    if level > 4 and ANCHOR_NODES:
                        from darkmatter.network.transports.webrtc import AnchorRelaySignaling
                        success = await webrtc.upgrade(state, conn, AnchorRelaySignaling())
                        if success:
                            conn._signaling_method = "anchor_relay"
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

    async def _poll_sdp_relay(self) -> None:
        """Poll anchor for buffered SDP signals when behind NAT."""
        state = self._get_state()
        if not state.private_key_hex or not ANCHOR_NODES:
            return

        ts = datetime.now(timezone.utc).isoformat()
        sig = sign_relay_poll(state.private_key_hex, state.agent_id, ts)

        for anchor in ANCHOR_NODES:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{anchor}/__darkmatter__/sdp_relay_poll/{state.agent_id}",
                        params={"signature": sig, "timestamp": ts},
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    signals = data.get("signals", [])
                    for signal in signals:
                        sig_type = signal.get("type")
                        from_id = signal.get("from_agent_id", "")
                        if sig_type == "offer" and from_id:
                            offer_data = signal.get("offer_data")
                            if offer_data:
                                # Process the SDP offer
                                webrtc = self.get_transport("webrtc")
                                if webrtc:
                                    answer = await webrtc.handle_offer(state, offer_data)
                                    if answer:
                                        # Post the answer back to anchor for the offerer
                                        await client.post(
                                            f"{anchor}/__darkmatter__/sdp_relay/{from_id}",
                                            json={
                                                "from_agent_id": state.agent_id,
                                                "answer_data": answer,
                                                "type": "answer",
                                            },
                                        )
                                        conn = state.connections.get(from_id)
                                        if conn:
                                            conn._signaling_method = "anchor_relay"
                    return
            except Exception as e:
                print(f"[DarkMatter] SDP relay poll error ({anchor}): {e}", file=sys.stderr)

    async def _poll_message_relay(self) -> None:
        """Poll anchor for buffered messages when behind NAT (Level 5)."""
        state = self._get_state()
        if not state.private_key_hex or not ANCHOR_NODES:
            return

        ts = datetime.now(timezone.utc).isoformat()
        sig = sign_relay_poll(state.private_key_hex, state.agent_id, ts)

        for anchor in ANCHOR_NODES:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{anchor}/__darkmatter__/message_relay_poll/{state.agent_id}",
                        params={"signature": sig, "timestamp": ts},
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    messages = data.get("messages", [])
                    for msg in messages:
                        # Decrypt E2E encrypted relay messages
                        if msg.get("e2e_encrypted") and msg.get("encrypted_payload"):
                            sender_pub = msg.get("encrypted_payload", {}).get("sender_public_key_hex")
                            if not sender_pub:
                                sender_pub = msg.get("from_agent_id", "")
                                print(f"[DarkMatter] Relay decrypt: no sender_public_key_hex in encrypted_payload, falling back to from_agent_id {sender_pub[:12]}…", file=sys.stderr)
                            try:
                                from darkmatter.security import decrypt_from_peer
                                import json as _json
                                plaintext = decrypt_from_peer(
                                    msg["encrypted_payload"],
                                    state.private_key_hex,
                                    sender_pub,
                                )
                                msg = _json.loads(plaintext.decode("utf-8"))
                            except Exception as e:
                                print(f"[DarkMatter] Relay decryption failed: {e}", file=sys.stderr)
                                continue

                        path = msg.get("path", "")
                        payload = msg.get("payload", {})
                        from_id = msg.get("from_agent_id", "")
                        if path and payload and self._process_relayed_message_fn:
                            self._process_relayed_message_fn(state, path, payload, from_id)
                        elif path and payload:
                            print(f"[DarkMatter] Relay: received message on {path} from {from_id[:12]}...",
                                  file=sys.stderr)
                    return
            except Exception as e:
                print(f"[DarkMatter] Message relay poll error ({anchor}): {e}", file=sys.stderr)

    # -- Private helpers --

    async def _query_anchor(self, anchor_url: str, target_agent_id: str) -> Optional[str]:
        """Query a single anchor node for an agent's URL."""
        try:
            async with httpx.AsyncClient(timeout=ANCHOR_LOOKUP_TIMEOUT) as client:
                resp = await client.get(
                    f"{anchor_url}/__darkmatter__/peer_lookup/{target_agent_id}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("url"):
                        return data["url"]
        except Exception:
            pass
        return None
