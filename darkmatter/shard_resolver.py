"""
Backward compatibility — imports from insight_resolver.

This file exists so that any external code importing from darkmatter.shard_resolver
continues to work. All logic lives in darkmatter.insight_resolver.
"""

from darkmatter.insight_resolver import (  # noqa: F401
    FUNC_PATTERNS,
    detect_function_name,
    ResolvedRegion,
    find_text_in_lines,
    find_function_by_name,
    resolve_region,
    HEALTHY_THRESHOLD,
    STALE_THRESHOLD,
    MAX_STALE_VIEWS,
    hash_content,
    compute_health,
    InsightHealth as ShardHealth,  # backward compat alias
    assess_health,
)
