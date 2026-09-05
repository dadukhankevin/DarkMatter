"""Read-only accountability from retained, transaction-bound evidence."""
from copy import deepcopy

from darkmatter.contract.contribution import contribution_state, verify_contribution_package
from darkmatter.contract.obligation import verify_agreement, source_matches


def project_obligation(record, contributions, include_proofs=False):
    proposal = record.get("contribution_agreement")
    acceptance = record.get("contribution_acceptance")
    agreement = verify_agreement(proposal, acceptance) if proposal and acceptance else None
    receipts = record.get("receipts") or []
    packages = []
    for candidate in contributions:
        if candidate.get("settlement_id") != record["settlement_id"]:
            continue
        package = verify_contribution_package(candidate["package"])
        source = package["ticket"]["source"]
        if not source_matches(record, source):
            continue
        packages.append(package)
    discussions = record.get("contribution_discussions") or []
    withdrawn = {e["reference"] for e in discussions if e["action"] == "withdraw"}
    open_disputes = [e for e in discussions if e["action"] == "dispute" and e["id"] not in withdrawn]
    states = [contribution_state(p) for p in packages]
    underlying = ("legacy" if not proposal else "offered" if not agreement else
                  "not_committed" if proposal["mode"] != "participate" else
                  "fulfilled" if "fulfilled" in states else "pending")
    result = {
        "settlement_id": record["settlement_id"], "peer_id": record["peer_id"],
        "payment_status": record["status"], "mode": proposal["mode"] if proposal else None,
        "status": "disputed" if open_disputes else underlying, "underlying_status": underlying,
        "payment_evidence": "counterparty_confirmation" if record.get("confirmation") else "payer_assertion" if receipts else "none",
        "contribution_evidence": "signed_fulfillment" if "fulfilled" in states else "ticket" if states else "none",
        "rail_verified": False, "route_states": states,
        "pending_reason": ("awaiting_payment_evidence" if not receipts else "no_matching_ticket" if not packages else "awaiting_fulfillment") if underlying == "pending" else None,
        "open_disputes": deepcopy(open_disputes),
        "interpretation": "Fulfilled means signed evidence exists; no independent rail check was performed. Pending is not a finding of failure.",
    }
    if include_proofs:
        result["proofs"] = {"terms": deepcopy(record["terms"]), "agreement": deepcopy(agreement), "discussions": deepcopy(discussions),
                            "receipts": [deepcopy(r["body"].get("receipt_attestation")) for r in receipts],
                            "contributions": deepcopy(packages)}
    return result
