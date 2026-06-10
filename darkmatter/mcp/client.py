"""
Loopback client for the daemon's local API.

MCP tool implementations are thin wrappers over these helpers — the daemon
owns all state, and every mutation goes through its HTTP API on 127.0.0.1.

Depends on: config
"""

import os
from typing import Any, Optional

import httpx

from darkmatter.config import DEFAULT_PORT
from darkmatter.logging import get_logger

_log = get_logger("client")

_daemon_port: Optional[int] = None


def set_daemon_port(port: int) -> None:
    """Record the daemon port for this process (set at startup)."""
    global _daemon_port
    _daemon_port = port


def get_daemon_port() -> int:
    """Return the daemon port (falls back to env/default before startup)."""
    if _daemon_port is not None:
        return _daemon_port
    return int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))


def daemon_url(path: str) -> str:
    return f"http://127.0.0.1:{get_daemon_port()}/__darkmatter__{path}"


async def daemon_post(path: str, payload: dict, timeout: float = 35.0) -> dict[str, Any]:
    """POST to the daemon's local API. Returns the JSON body (errors included)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(daemon_url(path), json=payload)
            return resp.json()
    except httpx.HTTPError as e:
        return {"success": False, "error": f"Daemon unreachable on port {get_daemon_port()}: {e}"}
    except ValueError:
        return {"success": False, "error": "Daemon returned a non-JSON response"}


async def daemon_get(path: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict[str, Any]:
    """GET from the daemon's local API. Returns the JSON body (errors included)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(daemon_url(path), params=params or {})
            return resp.json()
    except httpx.HTTPError as e:
        return {"success": False, "error": f"Daemon unreachable on port {get_daemon_port()}: {e}"}
    except ValueError:
        return {"success": False, "error": "Daemon returned a non-JSON response"}
