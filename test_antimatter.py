"""AntiMatter protocol state, trust, transport, and MCP tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from darkmatter.antimatter import (
    PROTOCOL,
    RECEIPT,
    AntimatterError,
    event_body,
    normalize_amount,
)
from darkmatter.contract.contribution import (
    MAX_CONTRIBUTION_HOPS,
    append_contribution_hop,
    create_contribution_ticket,
    create_source_receipt,
    resolve_contribution,
    verify_contribution_package,
)
from darkmatter.contract.envelope import seal_envelope
from darkmatter.contract.tenure import create_passport_claim
from darkmatter.gitbox.mailbox import Mailbox, get_mailbox, reset_mailbox
from darkmatter.identity import generate_keypair
from darkmatter.mcp.schemas import (
    AntimatterAction,
    AntimatterInput,
    ContributionAction,
    ContributionInput,
)
from darkmatter.mcp.tools import antimatter, antimatter_contribution


class _Ctx:
    session = object()


@pytest.fixture(autouse=True)
def _reset():
    reset_mailbox()
    yield
    reset_mailbox()


def _connected(tmp_path) -> tuple[Mailbox, Mailbox]:
    a = Mailbox(tmp_path / "a")
    b = Mailbox(tmp_path / "b")
    assert a.introduce(b.remote)["success"]
    assert b.introduce(a.remote)["success"]
    b.sync()
    assert b.accept(a.agent_id)["success"]
    a.sync()
    assert a.store.get_relationship(b.agent_id).state == "active"
    assert b.store.get_relationship(a.agent_id).state == "active"
    return a, b


def _connect_existing(a: Mailbox, b: Mailbox) -> None:
    assert a.introduce(b.remote)["success"]
    assert b.introduce(a.remote)["success"]
    b.sync()
    assert b.accept(a.agent_id)["success"]
    a.sync()


def test_bilateral_settlement_lifecycle_does_not_create_a_trust_score(tmp_path):
    payer, payee = _connected(tmp_path)

    offered = payer.antimatter_offer(
        payee.agent_id,
        "Review pull request 42",
        "25.00",
        "USD",
        "manual",
        terms={"deliverable": "review.md"},
        metadata={"project": "darkmatter"},
    )
    assert offered["success"]
    settlement_id = offered["settlement"]["settlement_id"]
    assert offered["settlement"]["terms"]["amount"] == "25"
    public_envelope = (payer.work / "outbox" / f"{offered['envelope_id']}.json").read_text()
    assert "Review pull request 42" not in public_envelope
    assert "deliverable" not in public_envelope

    sync = payee.sync()
    assert any(item["type"] == "antimatter_offer" for item in sync["ingested"])
    received_offer = payee.get_settlement(settlement_id)
    assert received_offer["status"] == "offered"
    assert received_offer["payer_id"] == payer.agent_id
    assert received_offer["payee_id"] == payee.agent_id
    assert payee.store.unconsumed_messages()[0]["type"] == "antimatter_offer"

    accepted = payee.antimatter_accept(payer.agent_id, settlement_id, "Agreed")
    assert accepted["success"]
    payer.sync()
    assert payer.get_settlement(settlement_id)["status"] == "accepted"

    invoiced = payee.antimatter_invoice(
        payer.agent_id,
        settlement_id,
        destination={"kind": "manual", "handle": "invoice-42"},
        memo="Review complete",
    )
    assert invoiced["success"]
    payer.sync()
    assert payer.get_settlement(settlement_id)["status"] == "invoiced"

    receipt = payer.antimatter_receipt(
        payee.agent_id,
        settlement_id,
        "manual:payment-42",
        proof={"reference": "payment-42"},
    )
    assert receipt["success"]
    receipt_id = receipt["envelope_id"]
    payee.sync()
    assert payee.get_settlement(settlement_id)["status"] == "receipt_submitted"
    assert payer.store.get_relationship(payee.agent_id).trust == 0
    assert payee.store.get_relationship(payer.agent_id).trust == 0

    confirmed = payee.antimatter_confirm(
        payer.agent_id,
        settlement_id,
        receipt_id,
        verification={"method": "manual", "matched": True},
    )
    assert confirmed["success"]
    assert confirmed["trust_delta"] == 0
    assert confirmed["contribution"]["status"] == "unroutable"
    assert confirmed["contribution"]["proof_package"]["ticket"]["contribution"]["amount"] == "0.25"
    assert payee.store.get_relationship(payer.agent_id).trust == 0

    payer.sync()
    payer_record = payer.get_settlement(settlement_id)
    payee_record = payee.get_settlement(settlement_id)
    assert payer_record["status"] == payee_record["status"] == "settled"
    assert payer.store.get_relationship(payee.agent_id).trust == 0
    assert payer.store.get_relationship(payee.agent_id).last_settlement == {
        "timestamp": payer.store.get_relationship(payee.agent_id).last_settlement["timestamp"],
        "tx_id": "manual:payment-42",
        "protocol": PROTOCOL,
        "settlement_id": settlement_id,
        "status": "settled",
        "receipt_id": receipt_id,
        "verification": "bilateral_confirmation",
        "trust_delta": 0,
    }


def test_contribution_routes_to_older_live_agent_and_publishes_proof(tmp_path):
    elder = Mailbox(tmp_path / "elder")
    payer, payee = _connected(tmp_path)

    assert payee.introduce(elder.remote)["success"]
    assert elder.introduce(payee.remote)["success"]
    elder.sync()
    assert elder.accept(payee.agent_id)["success"]
    payee.sync()

    offered = payer.antimatter_offer(payee.agent_id, "Network work", "10", "credit", "manual")
    settlement_id = offered["settlement"]["settlement_id"]
    payee.sync()
    assert payee.antimatter_accept(payer.agent_id, settlement_id)["success"]
    payer.sync()
    receipt = payer.antimatter_receipt(
        payee.agent_id, settlement_id, "manual:primary", {"verified": True},
    )
    assert receipt["success"]
    payee.sync()

    routed = payee.antimatter_contribute(settlement_id)
    assert routed["success"]
    contribution_id = routed["contribution_id"]
    assert routed["hop_count"] == 1
    assert routed["proof_package"]["path"][0]["to"] == elder.agent_id

    elder_sync = elder.sync()
    assert elder_sync["antimatter_actions"][0]["success"]
    payee_sync = payee.sync()
    assert payee_sync["antimatter_actions"][0]["success"]
    resolved = payee.get_contribution(contribution_id)
    assert resolved["status"] == "resolved"
    assert resolved["package"]["resolution"]["beneficiary"]["agent_id"] == elder.agent_id
    assert resolved["package"]["resolution"]["reason"] == "no_older_live_relationship"

    fulfilled = payee.antimatter_fulfill_contribution(
        contribution_id, "manual:contribution", {"amount": "0.1"},
    )
    assert fulfilled["success"]
    elder.sync()
    final = elder.get_contribution(contribution_id)
    assert final["status"] == "fulfilled"
    assert verify_contribution_package(final["package"])["fulfillment"]["transaction_id"] == (
        "manual:contribution"
    )
    public_proof = elder.work / "antimatter" / f"{contribution_id}.json"
    assert public_proof.exists()
    assert json.loads(public_proof.read_text())["ticket"]["contribution"]["rate"] == "0.01"
    assert payee.store.get_relationship(payer.agent_id).trust == 0


def test_multihop_resolution_returns_and_fulfillment_reaches_beneficiary(tmp_path):
    elder = Mailbox(tmp_path / "elder-multihop")
    middle = Mailbox(tmp_path / "middle-multihop")
    payer, payee = _connected(tmp_path)
    _connect_existing(payee, middle)
    _connect_existing(middle, elder)

    offered = payer.antimatter_offer(payee.agent_id, "Multi-hop", "20", "credit", "manual")
    settlement_id = offered["settlement"]["settlement_id"]
    payee.sync()
    assert payee.antimatter_accept(payer.agent_id, settlement_id)["success"]
    payer.sync()
    assert payer.antimatter_receipt(payee.agent_id, settlement_id, "manual:source")["success"]
    payee.sync()

    started = payee.antimatter_contribute(settlement_id)
    contribution_id = started["contribution_id"]
    assert started["proof_package"]["path"][0]["to"] == middle.agent_id
    middle.sync()
    elder.sync()
    middle.sync()
    payee.sync()

    resolved = payee.get_contribution(contribution_id)
    assert [hop["to"] for hop in resolved["package"]["path"]] == [
        middle.agent_id,
        elder.agent_id,
    ]
    assert resolved["package"]["resolution"]["beneficiary"]["agent_id"] == elder.agent_id

    assert payee.antimatter_fulfill_contribution(
        contribution_id, "manual:network-contribution",
    )["success"]
    with pytest.raises(ValueError, match="roll back"):
        payee.contributions.put(started["proof_package"])
    middle.sync()
    elder.sync()
    assert elder.get_contribution(contribution_id)["status"] == "fulfilled"


def test_contribution_proof_enforces_older_hops_and_hard_42_limit():
    now = datetime.now(timezone.utc)
    identities = []
    for index in range(MAX_CONTRIBUTION_HOPS + 2):
        private, public = generate_keypair()
        created = (now - timedelta(days=index + 1)).isoformat()
        identities.append((private, public, create_passport_claim(private, public, created)))
    payer_private, payer_id = generate_keypair()
    origin_private, origin_id, _ = identities[0]
    source_receipt = create_source_receipt(
        payer_private,
        payer_id=payer_id,
        payee_id=origin_id,
        settlement_id="am-max-hop-test",
        receipt_id="receipt",
        timestamp=now.isoformat(),
        transaction_id="transaction",
        amount="100",
        currency="credit",
        rail="manual",
    )
    ticket = create_contribution_ticket(
        origin_private,
        origin_id,
        {
            "settlement_id": "am-max-hop-test",
            "payer_id": payer_id,
            "payee_id": origin_id,
            "receipt_id": "receipt",
            "transaction_id": "transaction",
            "amount": "100",
            "currency": "credit",
            "rail": "manual",
            "receipt_attestation": source_receipt,
        },
    )
    package = {
        "version": 1,
        "ticket": ticket,
        "path": [],
        "resolution": None,
        "fulfillment": None,
    }
    observed = now.isoformat()
    relationship_since = (now - timedelta(days=100)).isoformat()
    younger_private, younger_id = generate_keypair()
    younger_claim = create_passport_claim(
        younger_private, younger_id, (now - timedelta(hours=1)).isoformat(),
    )
    with pytest.raises(ValueError, match="older passport"):
        append_contribution_hop(
            origin_private,
            package,
            from_passport=identities[0][2],
            to_passport=younger_claim,
            observed_active_at=observed,
            relationship_since=relationship_since,
        )
    with pytest.raises(ValueError, match="liveness window"):
        append_contribution_hop(
            origin_private,
            package,
            from_passport=identities[0][2],
            to_passport=identities[1][2],
            observed_active_at=(now - timedelta(days=8)).isoformat(),
            relationship_since=relationship_since,
        )
    for index in range(MAX_CONTRIBUTION_HOPS):
        private, _, from_claim = identities[index]
        _, _, to_claim = identities[index + 1]
        package = append_contribution_hop(
            private,
            package,
            from_passport=from_claim,
            to_passport=to_claim,
            observed_active_at=observed,
            relationship_since=relationship_since,
        )
    assert len(verify_contribution_package(package)["path"]) == 42
    with pytest.raises(ValueError, match="hop limit"):
        append_contribution_hop(
            identities[42][0],
            package,
            from_passport=identities[42][2],
            to_passport=identities[43][2],
            observed_active_at=observed,
            relationship_since=relationship_since,
        )
    resolved = resolve_contribution(
        identities[42][0],
        package,
        passport=identities[42][2],
        reason="max_hops",
    )
    assert resolved["resolution"]["reason"] == "max_hops"

    tampered = json.loads(json.dumps(resolved))
    tampered["path"][0]["observed_active_at"] = (now - timedelta(days=20)).isoformat()
    with pytest.raises(ValueError):
        verify_contribution_package(tampered)


def test_roles_and_transitions_are_enforced(tmp_path):
    payer, payee = _connected(tmp_path)
    offered = payer.antimatter_offer(
        payee.agent_id, "One task", "1", "credit", "internal",
    )
    settlement_id = offered["settlement"]["settlement_id"]

    own_accept = payer.antimatter_accept(payee.agent_id, settlement_id)
    assert own_accept["success"] is False
    assert "counterparty" in own_accept["error"]

    payee.sync()
    assert payee.antimatter_accept(payer.agent_id, settlement_id)["success"]
    payer.sync()

    payer_invoice = payer.antimatter_invoice(payee.agent_id, settlement_id)
    assert payer_invoice["success"] is False
    assert "payee" in payer_invoice["error"]

    payee_receipt = payee.antimatter_receipt(
        payer.agent_id, settlement_id, "not-a-real-payment",
    )
    assert payee_receipt["success"] is False
    assert "payer" in payee_receipt["error"]


def test_dispute_is_terminal_and_does_not_change_trust(tmp_path):
    payer, payee = _connected(tmp_path)
    offered = payer.antimatter_offer(
        payee.agent_id, "Ambiguous task", "4", "credit", "internal",
    )
    settlement_id = offered["settlement"]["settlement_id"]
    payee.sync()

    disputed = payee.antimatter_dispute(
        payer.agent_id,
        settlement_id,
        "Deliverable is underspecified",
        reference_id=offered["envelope_id"],
        evidence={"requested": "clarification"},
    )
    assert disputed["success"]
    assert disputed["settlement"]["status"] == "disputed"
    assert payee.store.get_relationship(payer.agent_id).trust == 0

    payer.sync()
    assert payer.get_settlement(settlement_id)["status"] == "disputed"
    assert payer.store.get_relationship(payee.agent_id).trust == 0
    retry = payer.antimatter_accept(payee.agent_id, settlement_id)
    assert retry["success"] is False
    assert "disputed" in retry["error"]


def test_semantically_invalid_signed_event_is_visible_but_not_applied(tmp_path):
    sender, recipient = _connected(tmp_path)
    body = event_body(
        "receipt",
        "am-does-not-exist",
        acceptance_id="missing",
        tx_id="fake",
        proof={},
    )
    env = seal_envelope(
        sender.store.private_key_hex,
        sender.agent_id,
        recipient.agent_id,
        RECEIPT,
        body,
    )
    sender._write_outbox(env)
    sender._publish("test invalid antimatter")

    recipient.sync()
    item = next(item for item in recipient.store.load_inbox() if item["id"] == env.id)
    assert item["type"] == RECEIPT
    assert item["consumed"] is False
    assert item["protocol_error"] == "Unknown settlement_id"
    assert recipient.get_settlement("am-does-not-exist") is None

    sender.sync()
    assert (sender.work / "readbox" / f"{env.id}.json").exists()


def test_amounts_are_exact_positive_decimals():
    assert normalize_amount("25.00") == "25"
    assert normalize_amount("0.0100") == "0.01"
    for invalid in (None, True, "0", "-1", "NaN", "Infinity", "hello"):
        with pytest.raises(AntimatterError):
            normalize_amount(invalid)


def test_mcp_offer_and_list(tmp_path, monkeypatch):
    peer = Mailbox(tmp_path / "peer")
    monkeypatch.setenv("DARKMATTER_PROJECT_DIR", str(tmp_path / "local"))
    local = get_mailbox()
    assert local.introduce(peer.remote)["success"]
    assert peer.introduce(local.remote)["success"]
    peer.sync()
    assert peer.accept(local.agent_id)["success"]
    local.sync()

    offered = json.loads(asyncio.run(antimatter(AntimatterInput(
        action=AntimatterAction.OFFER,
        peer_id=peer.agent_id,
        description="MCP task",
        amount="3.50",
        currency="credit",
        rail="internal",
    ), _Ctx())))
    assert offered["success"]
    assert offered["settlement"]["terms"]["amount"] == "3.5"

    listed = json.loads(asyncio.run(antimatter(AntimatterInput(
        action=AntimatterAction.LIST,
    ), _Ctx())))
    assert listed["success"]
    assert listed["count"] == 1
    assert listed["settlements"][0]["settlement_id"] == offered["settlement"]["settlement_id"]


def test_mcp_independently_verifies_portable_contribution(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    origin_private, origin_id = generate_keypair()
    payer_private, payer_id = generate_keypair()
    source_receipt = create_source_receipt(
        payer_private,
        payer_id=payer_id,
        payee_id=origin_id,
        settlement_id="am-mcp-proof",
        receipt_id="receipt",
        timestamp=now.isoformat(),
        transaction_id="tx",
        amount="5",
        currency="credit",
        rail="manual",
    )
    ticket = create_contribution_ticket(
        origin_private,
        origin_id,
        {
            "settlement_id": "am-mcp-proof",
            "payer_id": payer_id,
            "payee_id": origin_id,
            "receipt_id": "receipt",
            "transaction_id": "tx",
            "amount": "5",
            "currency": "credit",
            "rail": "manual",
            "receipt_attestation": source_receipt,
        },
    )
    package = resolve_contribution(
        origin_private,
        {"version": 1, "ticket": ticket, "path": [], "resolution": None, "fulfillment": None},
        passport=create_passport_claim(
            origin_private, origin_id, (now - timedelta(days=1)).isoformat(),
        ),
        reason="no_older_live_relationship",
    )
    monkeypatch.setenv("DARKMATTER_PROJECT_DIR", str(tmp_path / "verifier"))
    verified = json.loads(asyncio.run(antimatter_contribution(ContributionInput(
        action=ContributionAction.VERIFY,
        proof_package=package,
    ), _Ctx())))
    assert verified["success"] is True
    assert verified["valid"] is True
    assert verified["proof_package"]["ticket"]["contribution"]["amount"] == "0.05"

    package["ticket"]["contribution"]["amount"] = "4"
    rejected = json.loads(asyncio.run(antimatter_contribution(ContributionInput(
        action=ContributionAction.VERIFY,
        proof_package=package,
    ), _Ctx())))
    assert rejected["success"] is False
    assert rejected["valid"] is False
