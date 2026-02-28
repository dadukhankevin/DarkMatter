"""
Network transport abstraction — _send_to_peer (WebRTC-first, HTTP fallback).

Depends on: config, models
"""

import json
import sys
from typing import Optional

import httpx

from darkmatter.config import WEBRTC_MESSAGE_SIZE_LIMIT


async def http_post_to_peer(conn, path: str, payload: dict) -> dict:
    """Send an HTTP POST to a peer. Returns the response dict."""
    base_url = conn.agent_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if base_url.endswith(suffix):
            base_url = base_url[:-len(suffix)]
            break
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(base_url + path, json=payload)
        result = resp.json()
        result["transport"] = "http"
        return result


async def send_to_peer(conn, path: str, payload: dict,
                       lookup_peer_url_fn=None, save_state_fn=None,
                       get_state_fn=None) -> dict:
    """Send a message to a peer, using WebRTC data channel if available, else HTTP.

    On HTTP failure, attempts peer URL recovery via lookup_peer_url_fn before giving up.
    """
    # Try WebRTC first if channel is open
    if conn.webrtc_channel is not None:
        try:
            ready = getattr(conn.webrtc_channel, "readyState", None)
            if ready == "open":
                data = json.dumps({"path": path, "payload": payload})
                if len(data) <= WEBRTC_MESSAGE_SIZE_LIMIT:
                    conn.webrtc_channel.send(data)
                    conn.health_failures = 0
                    return {"success": True, "transport": "webrtc"}
        except Exception as e:
            print(f"[DarkMatter] WebRTC send failed, falling back to HTTP: {e}", file=sys.stderr)

    # HTTP — try direct, then recover via peer lookup on failure
    last_error = None
    try:
        result = await http_post_to_peer(conn, path, payload)
        conn.health_failures = 0
        return result
    except Exception as e:
        last_error = e

    # Direct HTTP failed — try peer lookup to find updated URL
    if lookup_peer_url_fn and get_state_fn:
        state = get_state_fn()
        if state is not None:
            print(f"[DarkMatter] HTTP send to {conn.agent_id[:12]}... failed, attempting peer lookup", file=sys.stderr)
            new_url = await lookup_peer_url_fn(state, conn.agent_id)
            if new_url and new_url != conn.agent_url:
                old_url = conn.agent_url
                conn.agent_url = new_url
                if save_state_fn:
                    save_state_fn()
                print(f"[DarkMatter] Recovered URL for {conn.agent_id[:12]}...: {old_url} -> {new_url}", file=sys.stderr)
                try:
                    result = await http_post_to_peer(conn, path, payload)
                    conn.health_failures = 0
                    return result
                except Exception:
                    pass

    raise last_error


def strip_base_url(url: str) -> str:
    """Strip /mcp or /__darkmatter__ suffix from a URL."""
    base = url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base
