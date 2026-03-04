"""
Pool business logic — provider selection, request proxying, shard generation.

Depends on: config, models
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Optional

import httpx

from darkmatter.config import POOL_PROXY_TIMEOUT
from darkmatter.models import Pool, PoolAccessToken, PoolProvider


# =============================================================================
# Lookup helpers (unchanged)
# =============================================================================

def find_pool(pools: list[Pool], pool_id: str) -> Optional[Pool]:
    """Find a pool by ID."""
    for p in pools:
        if p.pool_id == pool_id:
            return p
    return None


def find_access_token(pool: Pool, token_id: str) -> Optional[PoolAccessToken]:
    """Find an access token by ID within a pool."""
    for at in pool.access_tokens:
        if at.token_id == token_id:
            return at
    return None


def select_provider(pool: Pool, path: str) -> Optional[PoolProvider]:
    """Select the best provider for a given path.

    Strategy: cheapest enabled provider with fewest failures whose allowed_paths
    include the requested path.
    """
    candidates = [
        pv for pv in pool.providers
        if pv.enabled and any(path.startswith(ap) for ap in pv.allowed_paths)
    ]
    if not candidates:
        return None
    # Sort by price (ascending), then failures (ascending)
    candidates.sort(key=lambda pv: (pv.price_per_call, pv.failures))
    return candidates[0]


# =============================================================================
# ProxyResult & PoolHandler ABC
# =============================================================================

@dataclass
class ProxyResult:
    """Result of a handler proxying a request."""
    status_code: int
    headers: dict[str, str]
    body: bytes
    cost: float
    streaming: bool = False
    body_stream: Optional[AsyncIterator[bytes]] = field(default=None, repr=False)


class PoolHandler(ABC):
    """Abstract base for pool request handlers.

    A handler controls how requests are validated, costed, and proxied.
    Subclass this to support streaming, per-token billing, or any
    API-specific behavior.
    """

    @property
    @abstractmethod
    def handler_type(self) -> str:
        """String identifier stored on the Pool model (e.g. 'passthrough')."""
        ...

    def validate_request(
        self,
        provider: PoolProvider,
        method: str,
        path: str,
        headers: dict[str, str],
        body: Optional[str],
    ) -> Optional[str]:
        """Pre-flight validation. Return an error string, or None if OK."""
        return None

    def estimate_cost(self, provider: PoolProvider, method: str, path: str, body: Optional[str]) -> float:
        """Estimate cost for balance pre-check. Default: provider.price_per_call."""
        return provider.price_per_call

    @abstractmethod
    async def proxy(
        self,
        provider: PoolProvider,
        method: str,
        path: str,
        headers: dict[str, str],
        body: Optional[str],
    ) -> ProxyResult:
        """Execute the proxied request and return the result with actual cost."""
        ...


# =============================================================================
# Handler Registry
# =============================================================================

_handler_registry: dict[str, PoolHandler] = {}


def register_handler(handler: PoolHandler) -> None:
    """Register a handler instance by its handler_type."""
    _handler_registry[handler.handler_type] = handler


def get_handler(handler_type: str) -> Optional[PoolHandler]:
    """Look up a registered handler by type string."""
    return _handler_registry.get(handler_type)


# =============================================================================
# PassthroughHandler — default, identical to old proxy_request behavior
# =============================================================================

class PassthroughHandler(PoolHandler):
    """Buffer the full response, charge price_per_call. The original behavior."""

    @property
    def handler_type(self) -> str:
        return "passthrough"

    async def proxy(
        self,
        provider: PoolProvider,
        method: str,
        path: str,
        headers: dict[str, str],
        body: Optional[str],
    ) -> ProxyResult:
        status_code, resp_headers, resp_body = await _do_http_proxy(provider, method, path, headers, body)
        return ProxyResult(
            status_code=status_code,
            headers=resp_headers,
            body=resp_body,
            cost=provider.price_per_call,
        )


# Register at module load
register_handler(PassthroughHandler())


# =============================================================================
# Raw HTTP proxy (shared utility)
# =============================================================================

async def _do_http_proxy(
    provider: PoolProvider,
    method: str,
    path: str,
    headers: dict[str, str],
    body: Optional[str],
) -> tuple[int, dict[str, str], bytes]:
    """Low-level HTTP proxy — inject credentials, strip hop-by-hop headers."""
    url = provider.base_url.rstrip("/") + path

    req_headers = dict(headers) if headers else {}
    req_headers[provider.credential_header] = provider.credential_value
    for h in ("host", "transfer-encoding", "connection"):
        req_headers.pop(h, None)

    async with httpx.AsyncClient(timeout=POOL_PROXY_TIMEOUT) as client:
        resp = await client.request(
            method=method.upper(),
            url=url,
            headers=req_headers,
            content=body.encode() if body else None,
        )

    resp_headers = {k: v for k, v in resp.headers.items()
                    if k.lower() not in ("transfer-encoding", "connection", "content-encoding")}

    return resp.status_code, resp_headers, resp.content


# =============================================================================
# Backward-compat wrapper
# =============================================================================

async def proxy_request(
    provider: PoolProvider,
    method: str,
    path: str,
    headers: dict[str, str],
    body: Optional[str],
) -> tuple[int, dict[str, str], bytes]:
    """Proxy an API request through a provider, injecting credentials.

    Returns (status_code, response_headers, response_body).
    Kept for backward compatibility — delegates to _do_http_proxy.
    """
    return await _do_http_proxy(provider, method, path, headers, body)


# =============================================================================
# Shard content generation
# =============================================================================

def pool_to_shard_content(pool: Pool) -> str:
    """Generate discovery shard content for a pool."""
    lines = [
        f"# Pool: {pool.name}",
        f"Pool ID: {pool.pool_id}",
    ]
    if pool.description:
        lines.append(f"Description: {pool.description}")
    lines.append(f"Tags: {', '.join(pool.tags)}")
    lines.append(f"Handler: {pool.handler_type}")

    if pool.providers:
        enabled = [pv for pv in pool.providers if pv.enabled]
        lines.append(f"Providers: {len(enabled)} active")
        paths = set()
        prices = []
        for pv in enabled:
            paths.update(pv.allowed_paths)
            prices.append(pv.price_per_call)
        lines.append(f"Endpoints: {', '.join(sorted(paths))}")
        if prices:
            lines.append(f"Price range: {min(prices)}-{max(prices)} {enabled[0].price_token}/call")
    else:
        lines.append("Providers: none yet")

    return "\n".join(lines)
