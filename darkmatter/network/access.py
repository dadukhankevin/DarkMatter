"""
HTTP route access checks for mesh endpoints.

Kept separate from mesh protocol handlers so access policy can be tested and
changed without touching message or routing logic.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse

from darkmatter.config import ROUTE_ACCESS
from darkmatter.models import AgentState


def client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def is_local_request(request: Request) -> bool:
    """Check if request comes from localhost."""
    ip = client_ip(request)
    return ip in ("127.0.0.1", "::1", "localhost", "unknown")


def is_connected_peer(request: Request, state: Optional[AgentState]) -> bool:
    """Check if request comes from a known connected peer by connection URL host."""
    if state is None:
        return False
    request_ip = client_ip(request)
    for conn in state.connections.values():
        try:
            if urlparse(conn.agent_url).hostname == request_ip:
                return True
        except Exception:
            pass
    return False


def check_access(
    request: Request,
    route_name: str,
    state: Optional[AgentState] = None,
) -> Optional[JSONResponse]:
    """Return an error response when a route is not accessible."""
    level = ROUTE_ACCESS.get(route_name, "peer")

    if level == "public":
        return None
    if level == "local":
        if is_local_request(request):
            return None
        return JSONResponse(
            {"error": "This endpoint is only accessible from localhost"},
            status_code=403,
        )
    if level == "peer":
        if is_local_request(request):
            return None
        if is_connected_peer(request, state):
            return None
        return JSONResponse(
            {"error": "This endpoint requires a peer connection"},
            status_code=403,
        )
    return None
