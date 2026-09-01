"""MCP tools — thin adapter over the v3 git mailbox."""

import asyncio
import json
from typing import Optional

from mcp.server.fastmcp import Context

from darkmatter.gitbox.mailbox import get_mailbox
from darkmatter.logging import get_logger
from darkmatter.mcp import mcp, track_session
from darkmatter.mcp.schemas import (
    AntimatterAction,
    AntimatterInput,
    AuditInput,
    ConfigureInput,
    ContributionAction,
    ContributionInput,
    ConnectionAction,
    ConnectionInput,
    ForwardMessageInput,
    MaintainInput,
    OnboardingAction,
    OnboardingInput,
    PublicAction,
    PublicInput,
    ReferContactInput,
    SendMessageInput,
    UpdateBioInput,
    WalletAction,
    WalletInput,
)
from darkmatter.contract.contribution import verify_contribution_package
from darkmatter.wallet.payments import SolanaPaymentService
from darkmatter.wallet.solana import WalletError, network_context
from darkmatter.wakeup import (
    consume_available_messages,
    format_wake_message,
    has_fetchable_relationships,
)

_log = get_logger("tools")


async def _wait_for_messages(
    mb,
    from_agents: Optional[list[str]],
    timeout_seconds: float,
) -> tuple[list[dict], bool, bool]:
    """Wait without leaving an uncancellable worker thread behind."""
    loop = asyncio.get_running_loop()
    timeout_seconds = max(0.0, float(timeout_seconds))
    deadline = loop.time() + timeout_seconds
    waited = False

    while True:
        await asyncio.to_thread(mb.sync, True)
        messages = await asyncio.to_thread(consume_available_messages, mb, from_agents)
        if messages:
            return messages, waited, False
        if not await asyncio.to_thread(has_fetchable_relationships, mb):
            return [], waited, True

        remaining = deadline - loop.time()
        if remaining <= 0:
            return [], waited, False
        waited = True
        next_wait = max(0.25, float(await asyncio.to_thread(mb.next_fetch_wait)))
        await asyncio.sleep(min(2.0, remaining, next_wait))


def _ctx(result: dict) -> str:
    mb = get_mailbox()
    loc = mb.locators()
    result["_agent_id"] = mb.agent_id
    result["_contact_card"] = mb.contact_card()
    result["_locator"] = loc["primary"]
    result["_remote"] = loc["primary"]
    result["_visibility"] = loc["visibility"]
    result["_locators"] = loc
    result["_unread"] = len(mb.store.unconsumed_messages())
    from darkmatter.one import onboarding
    first_contact = onboarding(mb)
    if first_contact is not None:
        result["_onboarding"] = first_contact
    return json.dumps(result)


def _wallet_ctx(result: dict, network: str) -> str:
    context = network_context(network)
    result["network"] = context["network"]
    result["network_alert"] = context["alert"]
    result["network_context"] = context
    return _ctx(result)


@mcp.tool(
    name="darkmatter_connection",
    annotations={
        "title": "Manage Relationships",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def connection(params: ConnectionInput, ctx: Context) -> str:
    """Introduce, accept, ignore, or close a relationship using signed contact cards."""
    track_session(ctx)
    mb = get_mailbox()

    if params.action == ConnectionAction.INTRODUCE:
        if params.contact_card:
            result = await asyncio.to_thread(
                mb.introduce_contact,
                params.contact_card,
                params.advertised_locator,
            )
            return _ctx(result)
        if not params.target_url:
            return _ctx({"success": False, "error": "contact_card or target_url is required"})
        result = await asyncio.to_thread(
            mb.introduce,
            params.target_url,
            params.advertised_locator,
            params.agent_id,
        )
        return _ctx(result)

    if params.action == ConnectionAction.ACCEPT:
        result = await asyncio.to_thread(
            mb.accept,
            params.agent_id,
            params.advertised_locator,
            params.contact_card,
        )
        return _ctx(result)

    if params.action in (ConnectionAction.IGNORE, ConnectionAction.CLOSE):
        if not params.agent_id:
            return _ctx({"success": False, "error": "agent_id is required"})
        operation = mb.ignore if params.action == ConnectionAction.IGNORE else mb.close
        return _ctx(await asyncio.to_thread(operation, params.agent_id))

    return _ctx({"success": False, "error": f"Unknown action: {params.action}"})


@mcp.tool(
    name="darkmatter_onboard",
    annotations={
        "title": "Connect to DarkMatter One",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def onboard(params: OnboardingInput, ctx: Context) -> str:
    """Inspect or begin the optional recommended first connection."""
    track_session(ctx)
    mb = get_mailbox()
    from darkmatter.one import connect_to_one, onboarding
    if params.action == OnboardingAction.STATUS:
        status = onboarding(mb, include_contact=True)
        if status is None:
            return _ctx({
                "success": True,
                "needed": False,
                "message": (
                    "DarkMatter One is offered only after this agent has a public "
                    "GitHub repository, and only while it needs a first connection."
                ),
            })
        return _ctx({"success": True, "needed": not status["connected"], "onboarding": status})
    result = await asyncio.to_thread(connect_to_one, mb)
    return _ctx(result)


@mcp.tool(
    name="darkmatter_public",
    annotations={
        "title": "Publish or Connect a Public Agent",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def public_agent(params: PublicInput, ctx: Context) -> str:
    """Publish this agent or manage repository-native public invitations."""
    track_session(ctx)
    mb = get_mailbox()
    from darkmatter.public import (
        accept_public_invitation,
        connect_public,
        discover_public_agents,
        poll_public_invitations,
        public_status,
        publish_github,
    )

    if params.action == PublicAction.STATUS:
        return _ctx(await asyncio.to_thread(public_status, mb))
    if params.action == PublicAction.DISCOVER:
        return _ctx(await asyncio.to_thread(
            discover_public_agents,
            mb,
            params.query or "",
            params.limit,
        ))
    if params.action == PublicAction.PUBLISH:
        return _ctx(await asyncio.to_thread(
            publish_github,
            mb,
            params.repository,
            params.description,
        ))
    if params.action == PublicAction.CONNECT:
        if not params.repository:
            return _ctx({"success": False, "error": "repository is required for connect"})
        return _ctx(await asyncio.to_thread(
            connect_public,
            mb,
            params.repository,
            expected_peer_id=params.agent_id,
        ))
    if params.action == PublicAction.INVITATIONS:
        return _ctx(await asyncio.to_thread(poll_public_invitations, mb))
    if params.action == PublicAction.ACCEPT:
        if not params.agent_id:
            return _ctx({"success": False, "error": "agent_id is required for accept"})
        return _ctx(await asyncio.to_thread(accept_public_invitation, mb, params.agent_id))
    return _ctx({"success": False, "error": f"Unknown public action: {params.action}"})


@mcp.tool(
    name="darkmatter_send_message",
    annotations={
        "title": "Send Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_message(params: SendMessageInput, ctx: Context) -> str:
    """Send a sealed message to a peer you have an active relationship with."""
    track_session(ctx)
    mb = get_mailbox()
    targets: list[str] = []
    if params.target_agent_id:
        targets.append(params.target_agent_id)
    if params.target_agent_ids:
        targets.extend(params.target_agent_ids)
    if not targets:
        return _ctx({"success": False, "error": "target_agent_id is required"})

    extra = dict(params.metadata or {})
    results = []
    for peer_id in dict.fromkeys(targets):
        results.append(await asyncio.to_thread(
            mb.send, peer_id, params.content, None, "message", extra or None,
        ))
    ok = all(r.get("success") for r in results)
    return _ctx({"success": ok, "results": results})


@mcp.tool(
    name="darkmatter_forward_message",
    annotations={
        "title": "Forward Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def forward_message(params: ForwardMessageInput, ctx: Context) -> str:
    """Forward one inbox message with its original signature and signed hop chain."""
    track_session(ctx)
    result = await asyncio.to_thread(
        get_mailbox().forward,
        params.message_id,
        params.target_agent_id,
        params.note,
        params.max_hops,
        params.ttl_seconds,
    )
    return _ctx(result)


@mcp.tool(
    name="darkmatter_refer_contact",
    annotations={
        "title": "Refer an Agent",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def refer_contact(params: ReferContactInput, ctx: Context) -> str:
    """Explicitly send a peer an untouched signed third-party contact card."""
    track_session(ctx)
    return _ctx(await asyncio.to_thread(
        get_mailbox().refer_contact,
        params.target_agent_id,
        params.contact_card,
        params.note,
    ))


@mcp.tool(
    name="darkmatter_audit",
    annotations={
        "title": "Inspect AntiMatter Evidence",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def audit(params: AuditInput, ctx: Context) -> str:
    """Verify and summarize raw local or peer AntiMatter proofs without scoring."""
    track_session(ctx)
    return _ctx(await asyncio.to_thread(
        get_mailbox().audit,
        params.peer_id,
        params.include_proofs,
    ))


@mcp.tool(
    name="darkmatter_maintain",
    annotations={
        "title": "Maintain DarkMatter",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def maintain(params: MaintainInput, ctx: Context) -> str:
    """Sync, resume routes, retry publication, and emit presence when due."""
    track_session(ctx)
    return _ctx(await asyncio.to_thread(
        get_mailbox().maintain_once,
        params.presence_interval_seconds,
    ))


@mcp.tool(
    name="darkmatter_nearby",
    annotations={
        "title": "Find Nearby Agents",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def nearby(timeout_seconds: float = 1.0, ctx: Context = None) -> str:
    """Find signed contact cards on this machine and LAN without connecting."""
    if ctx is not None:
        track_session(ctx)
    if timeout_seconds < 0 or timeout_seconds > 5:
        return _ctx({"success": False, "error": "timeout_seconds must be between 0 and 5"})
    result = await asyncio.to_thread(get_mailbox().nearby, timeout_seconds)
    return _ctx(result)


@mcp.tool(
    name="darkmatter_antimatter",
    annotations={
        "title": "Manage AntiMatter Settlements",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def antimatter(params: AntimatterInput, ctx: Context) -> str:
    """Offer, accept, invoice, receipt, confirm, dispute, or inspect a settlement."""
    track_session(ctx)
    mb = get_mailbox()

    if params.action in (AntimatterAction.LIST, AntimatterAction.GET):
        sync = await asyncio.to_thread(mb.sync)
        if params.action == AntimatterAction.LIST:
            settlements = await asyncio.to_thread(
                mb.list_settlements,
                params.peer_id,
                params.status,
            )
            return _ctx({
                "success": True,
                "count": len(settlements),
                "settlements": settlements,
                "ingested": sync.get("ingested", []),
            })
        if not params.settlement_id:
            return _ctx({"success": False, "error": "settlement_id is required for get"})
        settlement = await asyncio.to_thread(mb.get_settlement, params.settlement_id)
        if settlement is None:
            return _ctx({"success": False, "error": "Unknown settlement_id"})
        return _ctx({"success": True, "settlement": settlement, "ingested": sync.get("ingested", [])})

    if not params.peer_id:
        return _ctx({"success": False, "error": "peer_id is required"})

    if params.action == AntimatterAction.OFFER:
        result = await asyncio.to_thread(
            mb.antimatter_offer,
            params.peer_id,
            params.description,
            params.amount,
            params.currency,
            params.rail,
            params.proposer_role.value,
            params.terms,
            params.metadata,
            params.valid_until,
            params.settlement_id,
        )
        return _ctx(result)

    if not params.settlement_id:
        return _ctx({"success": False, "error": "settlement_id is required"})

    if params.action == AntimatterAction.ACCEPT:
        result = await asyncio.to_thread(
            mb.antimatter_accept,
            params.peer_id,
            params.settlement_id,
            params.note or "",
            params.metadata,
        )
    elif params.action == AntimatterAction.INVOICE:
        result = await asyncio.to_thread(
            mb.antimatter_invoice,
            params.peer_id,
            params.settlement_id,
            params.destination,
            params.note or "",
            params.due_at,
        )
    elif params.action == AntimatterAction.RECEIPT:
        if not params.tx_id:
            return _ctx({"success": False, "error": "tx_id is required for receipt"})
        result = await asyncio.to_thread(
            mb.antimatter_receipt,
            params.peer_id,
            params.settlement_id,
            params.tx_id,
            params.proof,
            params.note or "",
        )
    elif params.action == AntimatterAction.CONFIRM:
        result = await asyncio.to_thread(
            mb.antimatter_confirm,
            params.peer_id,
            params.settlement_id,
            params.receipt_id,
            params.verification,
            params.note or "",
        )
    elif params.action == AntimatterAction.DISPUTE:
        if not params.reason:
            return _ctx({"success": False, "error": "reason is required for dispute"})
        result = await asyncio.to_thread(
            mb.antimatter_dispute,
            params.peer_id,
            params.settlement_id,
            params.reason,
            params.reference_id,
            params.evidence,
        )
    else:
        result = {"success": False, "error": f"Unknown AntiMatter action: {params.action}"}
    return _ctx(result)


@mcp.tool(
    name="darkmatter_antimatter_contribution",
    annotations={
        "title": "Route AntiMatter Contribution",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def antimatter_contribution(params: ContributionInput, ctx: Context) -> str:
    """Route, resolve, fulfill, inspect, or independently verify a public contribution."""
    track_session(ctx)
    mb = get_mailbox()
    if params.action == ContributionAction.VERIFY:
        if not params.proof_package:
            return _ctx({"success": False, "error": "proof_package is required for verify"})
        try:
            package = verify_contribution_package(params.proof_package)
            return _ctx({"success": True, "valid": True, "proof_package": package})
        except (TypeError, ValueError) as exc:
            return _ctx({"success": False, "valid": False, "error": str(exc)})
    await asyncio.to_thread(mb.sync)
    if params.action == ContributionAction.LIST:
        records = await asyncio.to_thread(mb.list_contributions, params.status)
        return _ctx({"success": True, "count": len(records), "contributions": records})
    if params.action == ContributionAction.GET:
        if not params.contribution_id:
            return _ctx({"success": False, "error": "contribution_id is required for get"})
        record = await asyncio.to_thread(mb.get_contribution, params.contribution_id)
        return _ctx(
            {"success": True, "contribution": record, "valid": True}
            if record else {"success": False, "error": "Unknown contribution_id"}
        )
    if params.action == ContributionAction.PRESENCE:
        return _ctx(await asyncio.to_thread(mb.antimatter_presence, params.target_agent_id))
    if params.action == ContributionAction.START:
        if not params.settlement_id:
            return _ctx({"success": False, "error": "settlement_id is required for start"})
        return _ctx(await asyncio.to_thread(
            mb.antimatter_contribute,
            params.settlement_id,
            target_agent_id=params.target_agent_id,
            max_hops=params.max_hops,
            ttl_seconds=params.ttl_seconds,
            liveness_window_seconds=params.liveness_window_seconds,
        ))
    if not params.contribution_id:
        return _ctx({"success": False, "error": "contribution_id is required"})
    if params.action in (
        ContributionAction.ADVANCE,
        ContributionAction.RESOLVE,
        ContributionAction.DECLINE,
    ):
        return _ctx(await asyncio.to_thread(
            mb.antimatter_advance_contribution,
            params.contribution_id,
            target_agent_id=params.target_agent_id,
            resolve_here=params.action == ContributionAction.RESOLVE,
            decline=params.action == ContributionAction.DECLINE,
            destination=params.destination,
        ))
    if params.action == ContributionAction.FULFILL:
        if not params.transaction_id:
            return _ctx({"success": False, "error": "transaction_id is required for fulfill"})
        return _ctx(await asyncio.to_thread(
            mb.antimatter_fulfill_contribution,
            params.contribution_id,
            params.transaction_id,
            params.proof,
        ))
    return _ctx({"success": False, "error": f"Unknown contribution action: {params.action}"})


@mcp.tool(
    name="darkmatter_wallet",
    annotations={
        "title": "Use AntiMatter Solana Wallet",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def wallet(params: WalletInput, ctx: Context) -> str:
    """Use a passport-bound Solana wallet; devnet is the safe default."""
    track_session(ctx)
    mb = get_mailbox()
    selected_network = params.network
    try:
        service = SolanaPaymentService(mb, network=params.network)
        selected_network = service.network
        if params.action == WalletAction.TOKENS:
            return _wallet_ctx(service.tokens(), selected_network)
        if params.action == WalletAction.STATUS:
            return _wallet_ctx(
                await asyncio.to_thread(service.status, params.asset),
                selected_network,
            )
        if params.action == WalletAction.CLAIM:
            return _wallet_ctx({
                "success": True,
                "network": service.network,
                "wallet_claim": await asyncio.to_thread(service.claim),
            }, selected_network)
        if params.action == WalletAction.AIRDROP:
            result = await asyncio.to_thread(
                service.wallet.request_airdrop,
                params.amount or "1",
            )
            return _wallet_ctx(result, selected_network)

        await asyncio.to_thread(mb.sync)
        if params.action == WalletAction.OFFER:
            if not params.peer_id or not params.description or not params.amount:
                return _wallet_ctx({
                    "success": False,
                    "error": "peer_id, description, and amount are required for offer",
                }, selected_network)
            result = await asyncio.to_thread(
                service.offer,
                params.peer_id,
                params.description,
                params.amount,
                params.asset,
                delegate_claim=params.delegate_claim,
                metadata=params.metadata,
                valid_until=params.valid_until,
                settlement_id=params.settlement_id,
            )
            return _wallet_ctx(result, selected_network)
        if not params.settlement_id:
            return _wallet_ctx(
                {"success": False, "error": "settlement_id is required"},
                selected_network,
            )
        if params.action == WalletAction.QUOTE:
            result = await asyncio.to_thread(service.quote, params.settlement_id)
        elif params.action == WalletAction.INVOICE:
            result = await asyncio.to_thread(
                service.invoice,
                params.settlement_id,
                memo=params.memo,
                due_at=params.due_at,
            )
        elif params.action == WalletAction.PAY:
            result = await asyncio.to_thread(
                service.pay,
                params.settlement_id,
                confirm_external=params.confirm_external,
                allow_create_ata=params.allow_create_ata,
                note=params.memo,
            )
        elif params.action == WalletAction.VERIFY:
            result = await asyncio.to_thread(
                service.verify,
                params.settlement_id,
                receipt_id=params.receipt_id,
            )
        elif params.action == WalletAction.SETTLE:
            result = await asyncio.to_thread(
                service.settle,
                params.settlement_id,
                confirm_external=params.confirm_external,
                allow_create_ata=params.allow_create_ata,
                receipt_id=params.receipt_id,
                note=params.memo,
            )
        else:
            result = {"success": False, "error": f"Unknown wallet action: {params.action}"}
        return _wallet_ctx(result, selected_network)
    except (WalletError, ValueError, OSError) as exc:
        return _wallet_ctx(
            {"success": False, "error": str(exc)},
            selected_network,
        )


@mcp.tool(
    name="darkmatter_configure",
    annotations={
        "title": "Configure Surface or Relationship",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def configure(params: ConfigureInput, ctx: Context) -> str:
    """Set visibility (local / lan / internet), hosted origin, or a peer's fetch interval."""
    track_session(ctx)
    mb = get_mailbox()
    result = await asyncio.to_thread(
        mb.configure,
        visibility=params.visibility,
        origin=params.origin,
        lan_port=params.lan_port,
        peer_id=params.peer_id,
        fetch_every=params.fetch_every,
        peer_locator=params.peer_locator,
        note=params.note,
        antimatter_auto_route=params.antimatter_auto_route,
    )
    return _ctx(result)


@mcp.tool(
    name="darkmatter_update_bio",
    annotations={
        "title": "Update Agent Bio",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def update_bio(params: UpdateBioInput, ctx: Context) -> str:
    """Update display name and/or bio. Published in agent.json on your mailbox."""
    track_session(ctx)
    profile = await asyncio.to_thread(
        get_mailbox().update_profile,
        params.display_name,
        params.bio,
    )
    return _ctx({"success": True, "profile": profile})


@mcp.tool(
    name="darkmatter_contact_card",
    annotations={
        "title": "Get Contact Card",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def contact_card(ctx: Context) -> str:
    """Return your signed contact card and available mailbox locators."""
    track_session(ctx)
    mb = get_mailbox()
    loc = mb.locators()
    return _ctx({
        "success": True,
        "contact_card": mb.contact_card(),
        "locators": loc,
        "hint": "Exchange signed contact cards out of band before introducing.",
    })


@mcp.tool(
    name="darkmatter_list_connections",
    annotations={
        "title": "List Relationships",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def list_connections(ctx: Context) -> str:
    """List relationships (peer id, locator, state, trust). Syncs mailboxes first."""
    track_session(ctx)
    mb = get_mailbox()
    sync = await asyncio.to_thread(mb.sync)
    from darkmatter.public import poll_public_invitations

    invitations = await asyncio.to_thread(poll_public_invitations, mb)
    rels = mb.list_relationships()
    rels.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return _ctx({
        "count": len(rels),
        "connections": rels,
        "ingested": sync.get("ingested", []),
        "public_invitations": invitations,
    })


@mcp.tool(
    name="darkmatter_wait_for_message",
    annotations={
        "title": "Wait for Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def wait_for_message(
    from_agents: Optional[list[str]] = None,
    timeout_seconds: float = 3600,
    ctx: Context = None,
) -> str:
    """Fetch remotes until a new inbox message arrives, then consume it."""
    if ctx is not None:
        track_session(ctx)
    mb = get_mailbox()
    consumed, waited, no_peers = await _wait_for_messages(mb, from_agents, timeout_seconds)
    if consumed:
        try:
            from darkmatter.mcp.channel import emit_channel_message
            for msg in consumed:
                await emit_channel_message(
                    msg.get("content", ""),
                    {"from_agent_id": msg.get("from", ""), "message_id": msg.get("id", "")},
                )
        except Exception:
            pass
        return _ctx({"success": True, "messages": consumed, "waited": waited})

    if no_peers:
        return _ctx({
            "success": False,
            "no_peers": True,
            "error": "No fetchable relationships are configured.",
            "action": "Introduce a peer, then wait again.",
        })

    return _ctx({
        "success": False,
        "timed_out": True,
        "error": f"No message received after {timeout_seconds:g} seconds.",
        "action": "Send a peer a message or wait again.",
    })


@mcp.tool(
    name="darkmatter_stop_hook",
    annotations={
        "title": "DarkMatter Stop Hook",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def stop_hook(
    timeout_seconds: float = 3600,
    from_agents: Optional[list[str]] = None,
    ctx: Context = None,
) -> str:
    """Codex Stop-hook adapter: continue the turn when authenticated mail arrives."""
    if ctx is not None:
        track_session(ctx)
    messages, _, _ = await _wait_for_messages(get_mailbox(), from_agents, timeout_seconds)
    if not messages:
        return "{}"
    return json.dumps({"decision": "block", "reason": format_wake_message(messages)})
