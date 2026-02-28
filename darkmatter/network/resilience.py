"""
Network resilience — peer lookup, webhook recovery, health loop, NAT/UPnP.

Depends on: config, models, identity
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional
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
    UPNP_AVAILABLE,
    WEBRTC_AVAILABLE,
    WEBHOOK_RECOVERY_MAX_ATTEMPTS,
    WEBHOOK_RECOVERY_TIMEOUT,
)
from darkmatter.identity import (
    validate_url,
    validate_webhook_url,
    sign_peer_update,
    sign_relay_poll,
)
from darkmatter.models import AgentState, Connection

# Track last anchor that responded successfully (for failover)
_last_working_anchor: Optional[str] = None


# =============================================================================
# Public URL Discovery
# =============================================================================

def get_public_url(port: int, state=None) -> str:
    """Get the public URL for this agent."""
    if state is not None and state.public_url:
        return state.public_url
    public_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
    if public_url:
        return public_url
    return f"http://localhost:{port}"


async def discover_public_url(port: int, state=None) -> str:
    """Discover the best public URL for this agent.

    If state is provided and UPnP succeeds, stores the mapping on state._upnp_mapping
    so cleanup_upnp() can remove it on shutdown.
    """
    env_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
    if env_url:
        print(f"[DarkMatter] Public URL (env): {env_url}", file=sys.stderr)
        return env_url

    if UPNP_AVAILABLE:
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


async def check_nat_status(public_url: str) -> bool:
    """Check if we're behind NAT. Returns True if NAT is detected."""
    if "localhost" in public_url or "127.0.0.1" in public_url:
        return True
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{public_url}/__darkmatter__/status")
            return resp.status_code != 200
    except Exception:
        return True


def check_nat_status_sync(public_url: str) -> bool:
    """Synchronous version of NAT detection."""
    if "localhost" in public_url or "127.0.0.1" in public_url:
        return True
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{public_url}/__darkmatter__/status")
            return resp.status_code != 200
    except Exception:
        return True


# =============================================================================
# Anchor / Peer Lookup
# =============================================================================

def get_active_anchor() -> str:
    """Return the last known working anchor, or the first configured anchor."""
    if _last_working_anchor and _last_working_anchor in ANCHOR_NODES:
        return _last_working_anchor
    return ANCHOR_NODES[0] if ANCHOR_NODES else ""


def build_webhook_url(state, message_id: str) -> str:
    """Build the webhook URL, using anchor relay if behind NAT."""
    if state.nat_detected and ANCHOR_NODES:
        anchor = get_active_anchor()
        return f"{anchor}/__darkmatter__/webhook_relay/{state.agent_id}/{message_id}"
    return f"{get_public_url(state.port, state)}/__darkmatter__/webhook/{message_id}"


async def query_anchor_for_peer(anchor_url: str, target_agent_id: str) -> Optional[str]:
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


async def lookup_peer_url(state, target_agent_id: str,
                          exclude_urls: set[str] | None = None) -> Optional[str]:
    """Find an agent's current URL, querying anchor nodes first, then peer fan-out."""
    if exclude_urls is None:
        exclude_urls = set()

    if ANCHOR_NODES:
        tasks = [asyncio.create_task(query_anchor_for_peer(a, target_agent_id)) for a in ANCHOR_NODES]
        try:
            done, pending = await asyncio.wait(tasks, timeout=ANCHOR_LOOKUP_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
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

    from darkmatter.network import strip_base_url
    peers = [c for c in state.connections.values() if c.agent_id != target_agent_id]
    if not peers:
        return None

    peers = peers[:PEER_LOOKUP_MAX_CONCURRENT]

    async def _query_peer(conn) -> Optional[str]:
        try:
            base_url = strip_base_url(conn.agent_url)
            async with httpx.AsyncClient(timeout=PEER_LOOKUP_TIMEOUT) as client:
                resp = await client.get(
                    f"{base_url}/__darkmatter__/peer_lookup/{target_agent_id}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("url"):
                        return data["url"]
        except Exception:
            pass
        return None

    tasks = [asyncio.create_task(_query_peer(p)) for p in peers]
    try:
        done, pending = await asyncio.wait(tasks, timeout=PEER_LOOKUP_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()
            if result is not None and result not in exclude_urls:
                for t in pending:
                    t.cancel()
                return result
        if pending:
            done2, pending2 = await asyncio.wait(pending, timeout=1.0)
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


# =============================================================================
# Webhook Recovery
# =============================================================================

async def webhook_request_with_recovery(
    state, webhook_url: str, from_agent_id: Optional[str],
    method: str = "POST", timeout: float = 30.0, **kwargs
) -> "httpx.Response":
    """Make an HTTP request to a webhook URL, recovering via peer lookup on connection failure."""
    deadline = time.monotonic() + WEBHOOK_RECOVERY_TIMEOUT
    current_url = webhook_url
    last_err: Exception | None = None

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

        new_base = await lookup_peer_url(state, from_agent_id, exclude_urls=urls_tried)
        if not new_base:
            break

        parsed = urlparse(current_url if attempt == 1 else webhook_url)
        path = parsed.path
        if not (path.startswith("/__darkmatter__/webhook/") or
                path.startswith("/__darkmatter__/webhook_relay/")):
            break

        from darkmatter.network import strip_base_url
        new_base = strip_base_url(new_base)
        new_webhook = f"{new_base}{path}"

        if new_webhook in urls_tried:
            break
        urls_tried.add(new_webhook)

        err = validate_webhook_url(new_webhook)
        if err:
            break

        print(f"[DarkMatter] Webhook recovery: {webhook_url} -> {new_webhook} (attempt {attempt}/{WEBHOOK_RECOVERY_MAX_ATTEMPTS})", file=sys.stderr)

        try:
            remaining = max(1.0, deadline - time.monotonic())
            async with httpx.AsyncClient(timeout=min(timeout, remaining)) as client:
                return await getattr(client, method.lower())(new_webhook, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e
            continue

    raise last_err


# =============================================================================
# Peer Update Broadcast
# =============================================================================

async def broadcast_peer_update(state) -> None:
    """Notify all connected peers and anchor nodes of our current URL and bio."""
    if not state.public_url:
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "agent_id": state.agent_id,
        "new_url": state.public_url,
        "timestamp": timestamp,
        "bio": state.bio,
    }
    if state.public_key_hex:
        payload["public_key_hex"] = state.public_key_hex
    if state.private_key_hex and state.public_key_hex:
        payload["signature"] = sign_peer_update(
            state.private_key_hex, state.agent_id, state.public_url, timestamp
        )

    from darkmatter.network import strip_base_url
    for conn in list(state.connections.values()):
        try:
            base_url = strip_base_url(conn.agent_url)
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{base_url}/__darkmatter__/peer_update", json=payload)
        except Exception as e:
            print(f"[DarkMatter] Failed to notify {conn.agent_id[:12]}... of URL change: {e}", file=sys.stderr)

    for anchor_url in ANCHOR_NODES:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{anchor_url}/__darkmatter__/peer_update", json=payload)
        except Exception as e:
            print(f"[DarkMatter] Failed to notify anchor {anchor_url} of URL: {e}", file=sys.stderr)


# =============================================================================
# Health Loop
# =============================================================================

async def check_connection_health(state, attempt_webrtc_upgrade_fn=None) -> None:
    """Check health of all stale connections and update failure counts."""
    from darkmatter.network import strip_base_url
    for conn in list(state.connections.values()):
        if conn.last_activity:
            try:
                last = datetime.fromisoformat(conn.last_activity.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - last).total_seconds()
                if age < STALE_CONNECTION_AGE:
                    continue
            except Exception:
                pass

        try:
            base_url = strip_base_url(conn.agent_url)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/__darkmatter__/status")
                if resp.status_code == 200:
                    conn.health_failures = 0
                    if conn.transport == "http" and WEBRTC_AVAILABLE and attempt_webrtc_upgrade_fn:
                        asyncio.create_task(attempt_webrtc_upgrade_fn(state, conn))
                    continue
        except Exception:
            pass

        conn.health_failures += 1
        if conn.health_failures >= HEALTH_FAILURE_THRESHOLD:
            print(
                f"[DarkMatter] Connection {conn.agent_id[:12]}... unhealthy "
                f"({conn.health_failures} failures, url={conn.agent_url})",
                file=sys.stderr,
            )


async def network_health_loop(state, attempt_webrtc_upgrade_fn=None, process_webhook_fn=None) -> None:
    """Background task: periodically check connection health and detect IP changes."""
    last_ip_check = 0.0
    last_known_ip = None

    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            now = time.time()

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
                                print(f"[DarkMatter] Public IP changed: {last_known_ip} -> {current_ip}", file=sys.stderr)
                                last_known_ip = current_ip
                                state.public_url = await discover_public_url(state.port, state=state)
                                await broadcast_peer_update(state)
                except Exception:
                    pass

            await check_connection_health(state, attempt_webrtc_upgrade_fn)

            if state.nat_detected and ANCHOR_NODES and state.private_key_hex:
                await poll_webhook_relay(state, process_webhook_fn=process_webhook_fn)

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[DarkMatter] Health loop error: {e}", file=sys.stderr)


async def poll_webhook_relay(state, process_webhook_fn=None) -> None:
    """Poll anchor nodes for buffered webhook callbacks (NAT relay).

    Args:
        process_webhook_fn: Callback(state, message_id, data) -> (result_dict, status_code).
            Injected to avoid circular import with mesh.py.
    """
    global _last_working_anchor
    ts = datetime.now(timezone.utc).isoformat()
    sig = sign_relay_poll(state.private_key_hex, state.agent_id, ts)

    ordered = list(ANCHOR_NODES)
    if _last_working_anchor and _last_working_anchor in ordered:
        ordered.remove(_last_working_anchor)
        ordered.insert(0, _last_working_anchor)

    for anchor in ordered:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{anchor}/__darkmatter__/webhook_relay_poll/{state.agent_id}",
                    params={"signature": sig, "timestamp": ts},
                )
                if resp.status_code != 200:
                    continue
                _last_working_anchor = anchor
                data = resp.json()
                callbacks = data.get("callbacks", [])
                for cb in callbacks:
                    msg_id = cb.get("message_id", "")
                    cb_data = cb.get("data", {})
                    if msg_id and cb_data and process_webhook_fn:
                        result, _ = process_webhook_fn(state, msg_id, cb_data)
                        if result.get("success"):
                            print(f"[DarkMatter] Relay: processed webhook for {msg_id}", file=sys.stderr)
                return
        except Exception as e:
            print(f"[DarkMatter] Relay poll error ({anchor}): {e}", file=sys.stderr)
            continue


# =============================================================================
# UPnP
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
            except Exception:
                continue

        return None
    except Exception as e:
        print(f"[DarkMatter] UPnP mapping failed: {e}", file=sys.stderr)
        return None


def cleanup_upnp(state) -> None:
    """Remove UPnP port mapping on shutdown."""
    if state is None or state._upnp_mapping is None:
        return
    url, upnp_obj, ext_port = state._upnp_mapping
    try:
        upnp_obj.deleteportmapping(ext_port, "TCP")
        print(f"[DarkMatter] UPnP mapping removed (port {ext_port})", file=sys.stderr)
    except Exception as e:
        print(f"[DarkMatter] UPnP cleanup failed: {e}", file=sys.stderr)
    state._upnp_mapping = None
