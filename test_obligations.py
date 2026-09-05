"""Bilateral promises survive policy changes; peer claims never grant authority."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from darkmatter.antimatter import ACCEPT, event_body
from darkmatter.commitment import declare_commitment
from darkmatter.contract.obligation import create_acceptance, verify_agreement, verify_proposal
from test_antimatter import _connected


def _accepted(payer, payee, mode="participate", proposer="payer"):
    sender, receiver = (payer, payee) if proposer == "payer" else (payee, payer)
    result = sender.antimatter_offer(receiver.agent_id, "Review", "25.00", "USD", "manual",
                                     proposer_role=proposer, contribution_mode=mode)
    assert result["success"], result
    sid = result["settlement"]["settlement_id"]
    receiver.sync()
    assert receiver.antimatter_accept(sender.agent_id, sid)["success"]
    sender.sync()
    return sid


@pytest.mark.parametrize("proposer", ["payer", "payee"])
def test_bound_terms_survive_policy_change_and_restart(tmp_path, proposer):
    payer, payee = _connected(tmp_path)
    original = declare_commitment(payee, "participate")["commitment"]
    sid = _accepted(payer, payee, proposer=proposer)
    before = payer.obligations(sid, True)["obligations"][0]
    proof = before["proofs"]["agreement"]
    assert verify_agreement(**proof) == proof
    assert before["status"] == "pending"
    if proposer == "payee":
        assert proof["proposal"]["commitment"] == original
    declare_commitment(payee, "decline", "Future offers only")
    assert payer.obligations(sid, True)["obligations"][0] == before
    from darkmatter.antimatter import AntimatterLedger
    assert AntimatterLedger(payee.store).get(sid)["contribution_agreement"] == proof["proposal"]
    assert payee.obligations(sid)["obligations"][0]["mode"] == "participate"
    with pytest.raises(ValueError):
        verify_proposal({**proof["proposal"], "mode": "decline"})
    with pytest.raises(ValueError):
        create_acceptance((payer if proposer == "payer" else payee).store.private_key_hex,
                          proof["proposal"], datetime.now(timezone.utc).isoformat())


def test_acceptance_cannot_strip_or_substitute_agreement(tmp_path):
    payer, payee = _connected(tmp_path)
    first = payer.antimatter_offer(payee.agent_id, "First", "1", "USD", "manual")["settlement"]
    second = payer.antimatter_offer(payee.agent_id, "Second", "2", "USD", "manual")["settlement"]
    payee.sync()
    timestamp = datetime.now(timezone.utc).isoformat()
    body = event_body("accept", first["settlement_id"], offer_id=first["offer"]["id"])
    assert not payee.antimatter.apply_event(ACCEPT, payee.agent_id, payer.agent_id, "stripped", timestamp, body)["success"]
    body["contribution_acceptance"] = create_acceptance(payee.store.private_key_hex, second["contribution_agreement"], timestamp)
    assert not payee.antimatter.apply_event(ACCEPT, payee.agent_id, payer.agent_id, "substituted", timestamp, body)["success"]
    assert payee.get_settlement(first["settlement_id"])["status"] == "offered"


def test_dispute_after_payment_is_attributed_and_does_not_block_mail(tmp_path):
    payer, payee = _connected(tmp_path)
    sid = _accepted(payer, payee)
    assert payer.antimatter_receipt(payee.agent_id, sid, "manual:paid")["success"]
    payee.sync()
    assert payee.antimatter_confirm(payer.agent_id, sid)["success"]
    payer.sync()
    pending = payee.obligations(sid)["obligations"][0]
    assert pending["status"] == "pending" and pending["route_states"] == ["unroutable"]
    dispute = payer.obligation_discuss(sid, "dispute", "Please explain the missing contribution")
    assert dispute["success"], dispute
    payee.sync()
    assert payee.obligations(sid)["obligations"][0]["status"] == "disputed"
    assert payee.get_settlement(sid)["status"] == "settled"
    ref = dispute["statement"]["id"]
    assert not payee.obligation_discuss(sid, "withdraw", "Cannot withdraw another's claim", ref)["success"]
    tampered = deepcopy(dispute["statement"])
    tampered["reason"] = "Forged"
    assert not payee.antimatter.apply_discussion(sid, payer.agent_id, tampered)["success"]
    assert payee.antimatter.apply_discussion(sid, payer.agent_id, dispute["statement"])["duplicate"]
    assert payer.obligation_discuss(sid, "withdraw", "Route unavailable; understood", ref)["success"]
    payee.sync()
    assert payee.obligations(sid)["obligations"][0]["status"] == "pending"
    assert len(payee.get_settlement(sid)["contribution_discussions"]) == 2
    assert payee.store.get_relationship(payer.agent_id).state == "active"
    assert payee.store.get_relationship(payer.agent_id).trust == 0


@pytest.mark.parametrize("mode", ["observe", "decline"])
def test_nonparticipating_agreement_never_starts_automatic_route(tmp_path, mode):
    payer, payee = _connected(tmp_path)
    sid = _accepted(payer, payee, mode=mode)
    assert payer.antimatter_receipt(payee.agent_id, sid, "manual:paid")["success"]
    payee.sync()
    confirmed = payee.antimatter_confirm(payer.agent_id, sid)
    assert confirmed["success"] and "contribution" not in confirmed
    assert payee.contributions.for_settlement(sid) is None
    assert payee.obligations(sid)["obligations"][0]["status"] == "not_committed"
    assert not payee.antimatter_contribute(sid)["success"]


def test_unrelated_valid_payment_proof_cannot_fulfill_an_obligation(tmp_path):
    from darkmatter.obligations import project_obligation
    payer, payee = _connected(tmp_path)
    first = _accepted(payer, payee)
    second = _accepted(payer, payee)
    assert payer.antimatter_receipt(payee.agent_id, second, "manual:other")["success"]
    payee.sync()
    assert payee.antimatter_confirm(payer.agent_id, second)["success"]
    other = payee.contributions.for_settlement(second)
    # Even a matching outer lookup key cannot make a valid, unrelated signed ticket apply.
    other["settlement_id"] = first
    result = project_obligation(payee.get_settlement(first), [other], True)
    assert result["proofs"]["contributions"] == []
    assert result["status"] == "pending" and not result["rail_verified"]


def test_legacy_offer_is_visible_without_inventing_a_promise(tmp_path):
    from darkmatter.antimatter import OFFER, offer_body
    payer, payee = _connected(tmp_path)
    body = offer_body("legacy", payer_id=payer.agent_id, payee_id=payee.agent_id,
                      proposer_role="payer", description="Old offer", amount="1", currency="USD", rail="manual")
    assert payer._send_envelope(payee.agent_id, OFFER, body)["success"]
    payee.sync()
    assert payee.antimatter_accept(payer.agent_id, "legacy")["success"]
    payer.sync()
    assert payer.obligations("legacy")["obligations"][0]["status"] == "legacy"


def test_dispute_quota_preserves_counterparty_and_withdrawal_capacity(tmp_path):
    from darkmatter.contract.obligation import create_discussion
    payer, payee = _connected(tmp_path)
    sid = _accepted(payer, payee)
    agreement = payer.obligations(sid, True)["obligations"][0]["proofs"]["agreement"]

    def statement(agent, ident, action="dispute", reference=""):
        return create_discussion(agent.store.private_key_hex, agreement, event_id=ident, action=action,
                                 reference=reference, reason="A reason", timestamp=datetime.now(timezone.utc).isoformat())

    for i in range(128):
        assert payee.antimatter.apply_discussion(sid, payer.agent_id, statement(payer, str(i)))["success"]
    assert not payee.antimatter.apply_discussion(sid, payer.agent_id, statement(payer, "overflow"))["success"]
    assert payee.antimatter.apply_discussion(sid, payee.agent_id, statement(payee, "counterparty"))["success"]
    assert payee.antimatter.apply_discussion(sid, payer.agent_id, statement(payer, "withdraw", "withdraw", "0"))["success"]
    assert not payee.antimatter.apply_discussion(sid, payer.agent_id, statement(payer, "withdraw-again", "withdraw", "0"))["success"]


def test_confirmed_receipt_is_the_ticket_source(tmp_path):
    payer, payee = _connected(tmp_path)
    sid = _accepted(payer, payee)
    first = payer.antimatter_receipt(payee.agent_id, sid, "manual:first")
    assert first["success"]
    assert payer.antimatter_receipt(payee.agent_id, sid, "manual:second")["success"]
    payee.sync()
    result = payee.antimatter_confirm(payer.agent_id, sid, first["envelope_id"])
    assert result["success"]
    source = result["contribution"]["package"]["ticket"]["source"]
    assert source["transaction_id"] == "manual:first"
    assert source["receipt_id"] == first["envelope_id"]
    audit = payer.audit(payee.agent_id, True)
    assert audit["success"]
    obligation = audit["retained_obligations"][0]
    assert obligation["proofs"]["contributions"][0]["ticket"]["source"] == source


def test_valid_foreign_ticket_with_same_settlement_id_is_not_reused(tmp_path):
    from darkmatter.contract.contribution import create_source_receipt, create_contribution_ticket
    from darkmatter.identity import generate_keypair
    payer, payee = _connected(tmp_path)
    sid = _accepted(payer, payee)
    attacker_private, attacker_id = generate_keypair()
    recipient_private, recipient_id = generate_keypair()
    source = {"settlement_id": sid, "payer_id": attacker_id, "payee_id": recipient_id,
              "receipt_id": "other-receipt", "transaction_id": "other-payment", "amount": "25", "currency": "USD", "rail": "manual"}
    source["receipt_attestation"] = create_source_receipt(attacker_private, **source, timestamp=datetime.now(timezone.utc).isoformat())
    ticket = create_contribution_ticket(recipient_private, recipient_id, source)
    foreign = payee.contributions.put({"version": 1, "ticket": ticket, "path": [], "resolution": None, "fulfillment": None})
    assert payee.contributions.for_settlement(sid)["contribution_id"] == foreign["contribution_id"]
    assert payee.contributions.for_settlement(sid, settlement=payee.get_settlement(sid)) is None
    assert payer.antimatter_receipt(payee.agent_id, sid, "manual:real")["success"]
    payee.sync()
    legitimate = payee.antimatter_contribute(sid)
    assert legitimate["success"] and legitimate["contribution_id"] != foreign["contribution_id"]
    assert legitimate["package"]["ticket"]["source"]["payer_id"] == payer.agent_id
