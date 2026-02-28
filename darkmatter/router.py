"""
Extensible message router — rule matching, router chain.

Pure routing logic only. Execution of decisions (forwarding, spawning, etc.)
lives in the network/mesh layer to avoid circular dependencies.

Depends on: models
"""

import asyncio
import sys
from typing import Optional, Callable

from darkmatter.models import (
    AgentState,
    QueuedMessage,
    RouterAction,
    RouterDecision,
    RoutingRule,
)


# =============================================================================
# Rule Matching
# =============================================================================

def rule_matches(rule: RoutingRule, msg: QueuedMessage) -> bool:
    """Check if a rule matches a message. All specified conditions must match (AND logic)."""
    if rule.keyword is not None:
        if rule.keyword.lower() not in msg.content.lower():
            return False
    if rule.from_agent_id is not None:
        if msg.from_agent_id != rule.from_agent_id:
            return False
    if rule.metadata_key is not None:
        if rule.metadata_key not in (msg.metadata or {}):
            return False
        if rule.metadata_value is not None:
            if str((msg.metadata or {}).get(rule.metadata_key, "")) != rule.metadata_value:
                return False
    return True


# =============================================================================
# Built-in Routers
# =============================================================================

def rule_router(state: AgentState, msg: QueuedMessage) -> RouterDecision:
    """Evaluate routing rules in priority order. First match wins."""
    rules = [r for r in state.routing_rules if r.enabled]
    rules.sort(key=lambda r: r.priority, reverse=True)
    for rule in rules:
        if rule_matches(rule, msg):
            action = RouterAction(rule.action)
            return RouterDecision(
                action=action,
                forward_to=rule.forward_to if action == RouterAction.FORWARD else [],
                response=rule.response_text if action == RouterAction.RESPOND else None,
                reason=f"Matched rule '{rule.rule_id}'",
            )
    return RouterDecision(action=RouterAction.PASS, reason="No rules matched")


def spawn_router(state: AgentState, msg: QueuedMessage) -> RouterDecision:
    """Default router: always HANDLE (triggers agent spawn)."""
    return RouterDecision(action=RouterAction.HANDLE, reason="Spawn mode — handling message")


def queue_router(state: AgentState, msg: QueuedMessage) -> RouterDecision:
    """Queue-only router: always HANDLE but without spawn."""
    return RouterDecision(action=RouterAction.HANDLE, reason="Queue mode — message queued for manual handling")


# =============================================================================
# Router Chain
# =============================================================================

_ROUTER_CHAINS: dict[str, list] = {
    "spawn": [rule_router, spawn_router],
    "rules_first": [rule_router, queue_router],
    "rules_only": [rule_router],
    "queue_only": [queue_router],
}

VALID_ROUTER_MODES = set(_ROUTER_CHAINS.keys())

_custom_router: Optional[Callable] = None


def set_custom_router(fn: Optional[Callable]) -> None:
    """Register a custom router callable.

    Signature: (AgentState, QueuedMessage) -> RouterDecision
    Return PASS to defer to built-in routers. Pass None to remove.
    """
    global _custom_router
    _custom_router = fn


def get_router_chain(mode: str) -> list:
    """Return the router function list for the given mode."""
    chain = _ROUTER_CHAINS.get(mode, _ROUTER_CHAINS["spawn"])
    if _custom_router is not None:
        return [_custom_router] + chain
    return chain


async def execute_routing(state: AgentState, msg: QueuedMessage,
                          execute_decision_fn=None) -> None:
    """Run the router chain and execute the winning decision.

    execute_decision_fn is a callback that handles the actual execution
    (forwarding, spawning, etc.) — injected to avoid circular imports.
    """
    chain = get_router_chain(state.router_mode)

    for router_fn in chain:
        try:
            result = router_fn(state, msg)
            if asyncio.iscoroutine(result):
                result = await result
            decision = result
        except Exception as e:
            print(f"[DarkMatter] Router {router_fn.__name__} raised: {e}", file=sys.stderr)
            continue

        if decision.action != RouterAction.PASS:
            print(
                f"[DarkMatter] Routing decision for {msg.message_id[:12]}...: "
                f"{decision.action.value} (by {router_fn.__name__}: {decision.reason})",
                file=sys.stderr,
            )
            if execute_decision_fn is not None:
                await execute_decision_fn(state, msg, decision)
            return

    print(f"[DarkMatter] All routers passed for {msg.message_id[:12]}... — message stays in queue", file=sys.stderr)
