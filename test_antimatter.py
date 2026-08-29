"""AntiMatter protocol state, trust, transport, and MCP tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from darkmatter.antimatter import (
    PROTOCOL,
    RECEIPT,
    AntimatterError,
    event_body,
    normalize_amount,
)
from darkmatter.contract.envelope import seal_envelope
from darkmatter.gitbox.mailbox import Mailbox, get_mailbox, reset_mailbox
from darkmatter.mcp.schemas import AntimatterAction, AntimatterInput
from darkmatter.mcp.tools import antimatter


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


def test_bilateral_settlement_lifecycle_updates_local_trust(tmp_path):
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
    assert confirmed["trust_delta"] == 0.05
    assert payee.store.get_relationship(payer.agent_id).trust == 0.05

    payer.sync()
    payer_record = payer.get_settlement(settlement_id)
    payee_record = payee.get_settlement(settlement_id)
    assert payer_record["status"] == payee_record["status"] == "settled"
    assert payer.store.get_relationship(payee.agent_id).trust == 0.05
    assert payer.store.get_relationship(payee.agent_id).last_settlement == {
        "timestamp": payer.store.get_relationship(payee.agent_id).last_settlement["timestamp"],
        "tx_id": "manual:payment-42",
        "protocol": PROTOCOL,
        "settlement_id": settlement_id,
        "status": "settled",
        "receipt_id": receipt_id,
        "verification": "bilateral_confirmation",
        "trust_delta": 0.05,
    }


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
