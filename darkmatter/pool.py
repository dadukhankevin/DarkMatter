"""
Pool business logic — provider selection, request proxying, shard generation.

Depends on: config, models
"""

from typing import Optional

import httpx

from darkmatter.config import POOL_PROXY_TIMEOUT
from darkmatter.models import Pool, PoolAccessToken, PoolProvider


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


async def proxy_request(
    provider: PoolProvider,
    method: str,
    path: str,
    headers: dict[str, str],
    body: Optional[str],
) -> tuple[int, dict[str, str], bytes]:
    """Proxy an API request through a provider, injecting credentials.

    Returns (status_code, response_headers, response_body).
    """
    url = provider.base_url.rstrip("/") + path

    # Build request headers — inject credential, forward consumer headers
    req_headers = dict(headers) if headers else {}
    req_headers[provider.credential_header] = provider.credential_value
    # Remove hop-by-hop headers
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


def pool_to_shard_content(pool: Pool) -> str:
    """Generate discovery shard content for a pool."""
    lines = [
        f"# Pool: {pool.name}",
        f"Pool ID: {pool.pool_id}",
    ]
    if pool.description:
        lines.append(f"Description: {pool.description}")
    lines.append(f"Tags: {', '.join(pool.tags)}")

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
