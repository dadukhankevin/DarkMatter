"""AntiMatter settlement workflow backed by exact Solana transfers."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from darkmatter.store.local import atomic_write_text
from darkmatter.wallet.claims import create_wallet_claim, verify_wallet_claim
from darkmatter.wallet.solana import (
    SolanaWallet,
    WalletError,
    amount_to_base_units,
    format_base_units,
)
from darkmatter.wallet.tokens import list_tokens, normalize_network


ANTIMATTER_RATE = Decimal("0.01")


class PaymentJournal:
    """Crash-safe local record preventing a retry from sending money twice."""

    def __init__(self, root: str | Path, store):
        self.path = Path(root) / ".darkmatter" / "wallet_payments.json"
        self.store = store

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "settlements": {}}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise WalletError(f"Could not read wallet payment journal: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("settlements"), dict):
            raise WalletError("Wallet payment journal is malformed")
        return data

    def get(self, settlement_id: str, phase: str) -> dict | None:
        with self.store.locked():
            entry = self._load()["settlements"].get(settlement_id, {}).get(phase)
            return deepcopy(entry) if entry else None

    def put(self, settlement_id: str, phase: str, entry: dict) -> None:
        with self.store.locked():
            data = self._load()
            settlement = data["settlements"].setdefault(settlement_id, {})
            existing = settlement.get(phase)
            if existing and existing.get("signature") != entry.get("signature"):
                raise WalletError(f"A different {phase} transaction is already journaled")
            settlement[phase] = deepcopy(entry)
            atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


class SolanaPaymentService:
    def __init__(self, mailbox, *, network: str | None = None, wallet=None):
        self.mailbox = mailbox
        self.network = normalize_network(network)
        self.wallet = wallet or SolanaWallet(mailbox.root, network=self.network)
        if self.wallet.network != self.network:
            raise WalletError("Wallet and payment service networks do not match")
        self.journal = PaymentJournal(mailbox.root, mailbox.store)

    def claim(self) -> dict:
        return create_wallet_claim(
            self.mailbox.store.private_key_hex,
            self.mailbox.agent_id,
            self.wallet.address,
            network=self.network,
        )

    def status(self, asset: str = "SOL") -> dict:
        return {
            "success": True,
            "network": self.network,
            "address": self.wallet.address,
            "key_path": str(self.wallet.key_path),
            "mainnet_spending_locked": self.network == "mainnet-beta",
            "balance": self.wallet.balance(asset),
            "wallet_claim": self.claim(),
        }

    def tokens(self) -> dict:
        return {
            "success": True,
            "network": self.network,
            "tokens": list_tokens(self.network),
            "arbitrary_mints": True,
        }

    def _record(self, settlement_id: str) -> dict:
        record = self.mailbox.get_settlement(settlement_id)
        if not record:
            raise WalletError("Unknown settlement_id")
        expected_rail = f"solana:{self.network}"
        if record.get("terms", {}).get("rail") != expected_rail:
            raise WalletError(f"Settlement rail must be {expected_rail}")
        return record

    @staticmethod
    def _asset(record: dict) -> str:
        value = record.get("terms", {}).get("currency")
        if not isinstance(value, str) or not value:
            raise WalletError("Settlement currency is missing")
        return value

    def _contribution_record(self, settlement_id: str) -> dict | None:
        return self.mailbox.contributions.for_settlement(settlement_id, settlement=self._record(settlement_id))

    def _beneficiary_claim(self, record: dict) -> dict | None:
        contribution = self._contribution_record(record["settlement_id"])
        if not contribution or contribution.get("status") not in ("resolved", "fulfilled"):
            return None
        resolution = contribution["package"].get("resolution") or {}
        beneficiary = resolution.get("beneficiary") or {}
        destination = resolution.get("destination") or {}
        claim = destination.get("wallet_claim")
        if claim is None:
            return None
        claim = verify_wallet_claim(
            claim,
            expected_agent_id=beneficiary.get("agent_id"),
            network=self.network,
        )
        if claim["agent_id"] in (record["payer_id"], record["payee_id"]):
            raise WalletError("AntiMatter beneficiary must be a third-party agent")
        return claim

    def _quote(self, record: dict) -> dict:
        token = self.wallet.resolve_asset(self._asset(record))
        amount, raw = amount_to_base_units(record["terms"]["amount"], token.decimals)
        beneficiary = self._beneficiary_claim(record)
        contribution_raw = int(
            (Decimal(raw) * ANTIMATTER_RATE).to_integral_value(rounding=ROUND_DOWN),
        )
        participating = (record.get("contribution_agreement") or {}).get("mode", "participate") == "participate"
        if not participating:
            contribution_raw = 0
        if participating and contribution_raw < 1:
            raise WalletError("Settlement is too small to represent its 1% contribution")
        return {
            "network": self.network,
            "asset": token.to_dict(),
            "amount": amount,
            "amount_base_units": str(raw),
            "contribution_rate": format(ANTIMATTER_RATE, "f") if participating else "0",
            "contribution_amount": format_base_units(contribution_raw, token.decimals),
            "contribution_base_units": str(contribution_raw),
            "beneficiary": beneficiary,
        }

    def quote(self, settlement_id: str) -> dict:
        record = self._record(settlement_id)
        return {"success": True, "settlement_id": settlement_id, **self._quote(record)}

    def offer(
        self,
        peer_id: str,
        description: str,
        amount: object,
        asset: str,
        *,
        delegate_claim: dict | None = None,
        metadata: dict | None = None,
        valid_until: str | None = None,
        settlement_id: str | None = None,
    ) -> dict:
        token = self.wallet.resolve_asset(asset)
        amount, _ = amount_to_base_units(amount, token.decimals)
        if delegate_claim is not None:
            raise WalletError(
                "Manual delegates are not AntiMatter; omit delegate_claim and let the signed route select one",
            )
        return self.mailbox.antimatter_offer(
            peer_id,
            description,
            amount,
            asset,
            f"solana:{self.network}",
            "payer",
            {"antimatter": {"version": 2, "rate": format(ANTIMATTER_RATE, "f")}},
            metadata or {},
            valid_until,
            settlement_id,
        )

    def invoice(self, settlement_id: str, *, memo: str = "", due_at: str | None = None) -> dict:
        record = self._record(settlement_id)
        if record["payee_id"] != self.mailbox.agent_id:
            raise WalletError("Only the local payee can create this Solana invoice")
        quote = self._quote(record)
        destination = {
            "rail": f"solana:{self.network}",
            "wallet_claim": self.claim(),
            "asset": quote["asset"],
            "amount": quote["amount"],
            "amount_base_units": quote["amount_base_units"],
        }
        return self.mailbox.antimatter_invoice(
            record["peer_id"],
            settlement_id,
            destination,
            memo,
            due_at,
        )

    def _invoice_claim(self, record: dict) -> dict:
        invoice = record.get("invoice")
        if not invoice:
            raise WalletError("Payee must issue a signed Solana invoice before payment")
        destination = invoice.get("body", {}).get("destination")
        if not isinstance(destination, dict) or destination.get("rail") != f"solana:{self.network}":
            raise WalletError("Invoice has an invalid Solana destination")
        return verify_wallet_claim(
            destination.get("wallet_claim"),
            expected_agent_id=record["payee_id"],
            network=self.network,
        )

    def pay(
        self,
        settlement_id: str,
        *,
        confirm_external: bool,
        allow_create_ata: bool = False,
        note: str = "",
    ) -> dict:
        if not confirm_external:
            raise WalletError("Set confirm_external=true to authorize the on-chain payment")
        record = self._record(settlement_id)
        if record["payer_id"] != self.mailbox.agent_id:
            raise WalletError("Only the local payer can pay this settlement")
        if record.get("receipts"):
            raise WalletError("This settlement already has a submitted receipt")
        payee_claim = self._invoice_claim(record)
        quote = self._quote(record)
        asset = self._asset(record)
        journaled = self.journal.get(settlement_id, "primary")
        if journaled:
            transfer = journaled["transfer"]
            self.wallet.verify_transfer(
                transfer["signature"],
                source_wallet=self.wallet.address,
                destination_wallet=payee_claim["address"],
                amount=quote["amount"],
                asset=asset,
            )
        else:
            transfer = self.wallet.transfer(
                payee_claim["address"],
                quote["amount"],
                asset,
                allow_create_ata=allow_create_ata,
            )
            self.journal.put(settlement_id, "primary", {
                "signature": transfer["signature"],
                "transfer": transfer,
            })
        proof = {
            "rail": f"solana:{self.network}",
            "network": self.network,
            "asset": quote["asset"],
            "amount": quote["amount"],
            "amount_base_units": quote["amount_base_units"],
            "payer_wallet": self.claim(),
            "payee_wallet": payee_claim,
            "source_account": transfer["source_account"],
            "destination_account": transfer["destination_account"],
            "explorer_url": transfer["explorer_url"],
        }
        receipt = self.mailbox.antimatter_receipt(
            record["peer_id"],
            settlement_id,
            transfer["signature"],
            proof,
            note,
        )
        if not receipt.get("success"):
            return {
                **receipt,
                "payment_succeeded": True,
                "transaction": transfer,
                "warning": "Payment is confirmed and journaled, but its receipt was not published; retry pay.",
            }
        return {**receipt, "payment_succeeded": True, "transaction": transfer}

    def _receipt(self, record: dict, receipt_id: str | None = None) -> dict:
        receipts = record.get("receipts") or []
        if not receipts:
            raise WalletError("Settlement has no payment receipt")
        if receipt_id:
            receipt = next((item for item in receipts if item.get("id") == receipt_id), None)
            if not receipt:
                raise WalletError("Unknown receipt_id")
            return receipt
        return receipts[-1]

    def verify(self, settlement_id: str, *, receipt_id: str | None = None) -> dict:
        record = self._record(settlement_id)
        receipt = self._receipt(record, receipt_id)
        proof = receipt.get("body", {}).get("proof")
        if not isinstance(proof, dict) or proof.get("rail") != f"solana:{self.network}":
            raise WalletError("Receipt does not contain a Solana proof")
        payer_claim = verify_wallet_claim(
            proof.get("payer_wallet"),
            expected_agent_id=record["payer_id"],
            network=self.network,
        )
        payee_claim = self._invoice_claim(record)
        proof_payee = verify_wallet_claim(
            proof.get("payee_wallet"),
            expected_agent_id=record["payee_id"],
            network=self.network,
        )
        if proof_payee["address"] != payee_claim["address"]:
            raise WalletError("Receipt payee wallet does not match the signed invoice")
        quote = self._quote(record)
        verification = self.wallet.verify_transfer(
            receipt["body"]["tx_id"],
            source_wallet=payer_claim["address"],
            destination_wallet=payee_claim["address"],
            amount=quote["amount"],
            asset=self._asset(record),
        )
        return {
            "success": True,
            "settlement_id": settlement_id,
            "receipt_id": receipt["id"],
            "verification": verification,
        }

    def settle(
        self,
        settlement_id: str,
        *,
        confirm_external: bool,
        allow_create_ata: bool = False,
        receipt_id: str | None = None,
        note: str = "",
    ) -> dict:
        record = self._record(settlement_id)
        if record["payee_id"] != self.mailbox.agent_id:
            raise WalletError("Only the local payee can settle this payment")
        receipt = self._receipt(record, receipt_id)
        primary = self.verify(settlement_id, receipt_id=receipt["id"])["verification"]
        payee_claim = self._invoice_claim(record)
        if payee_claim["address"] != self.wallet.address:
            raise WalletError("Current wallet does not match the wallet used in the invoice")
        if (record.get("contribution_agreement") or {}).get("mode", "participate") != "participate":
            confirmation = self.mailbox.antimatter_confirm(
                record["peer_id"], settlement_id, receipt["id"],
                {"rail": f"solana:{self.network}", "primary": primary, "contribution": {"status": "not_committed"}}, note,
            )
            return {**confirmation, "primary_verified": True, "contribution": {"status": "not_committed"}}
        quote = self._quote(record)
        contribution_record = self._contribution_record(settlement_id)
        if contribution_record is None:
            started = self.mailbox.antimatter_contribute(settlement_id, receipt_id=receipt["id"])
            if not started.get("success"):
                return {
                    **started,
                    "primary_verified": True,
                    "contribution_started": False,
                }
            contribution_record = self._contribution_record(settlement_id)
        if contribution_record["package"]["ticket"]["source"]["receipt_id"] != receipt["id"]:
            raise WalletError("Contribution does not match the selected primary receipt")
        contribution_id = contribution_record["contribution_id"]
        if contribution_record["status"] in ("created", "routing"):
            return {
                "success": True,
                "primary_verified": True,
                "settlement_pending": True,
                "contribution_started": True,
                "contribution_id": contribution_id,
                "contribution_status": contribution_record["status"],
                "message": "The signed contribution ticket is routing; sync and settle again after resolution.",
            }
        if contribution_record["status"] in ("unroutable", "declined"):
            verification = {
                "rail": f"solana:{self.network}",
                "primary": primary,
                "contribution": {
                    "status": contribution_record["status"],
                    "contribution_id": contribution_id,
                    "resolution": contribution_record["package"]["resolution"],
                },
            }
            confirmation = self.mailbox.antimatter_confirm(
                record["peer_id"], settlement_id, receipt["id"], verification, note,
            )
            return {
                **confirmation,
                "primary_verified": True,
                "contribution": verification["contribution"],
            }

        beneficiary = self._beneficiary_claim(record)
        if beneficiary is None:
            raise WalletError(
                "The resolved beneficiary did not provide a passport-bound Solana destination",
            )
        contribution = None
        if contribution_record["status"] == "fulfilled":
            fulfillment = contribution_record["package"]["fulfillment"]
            transfer = {"signature": fulfillment["transaction_id"]}
        else:
            if not confirm_external:
                raise WalletError(
                    "Set confirm_external=true to authorize the routed 1% contribution",
                )
            journaled = self.journal.get(settlement_id, f"contribution:{contribution_id}")
            if journaled:
                transfer = journaled["transfer"]
            else:
                transfer = self.wallet.transfer(
                    beneficiary["address"],
                    quote["contribution_amount"],
                    self._asset(record),
                    allow_create_ata=allow_create_ata,
                )
                self.journal.put(settlement_id, f"contribution:{contribution_id}", {
                    "signature": transfer["signature"],
                    "transfer": transfer,
                })
        contribution = self.wallet.verify_transfer(
            transfer["signature"],
            source_wallet=self.wallet.address,
            destination_wallet=beneficiary["address"],
            amount=quote["contribution_amount"],
            asset=self._asset(record),
        )
        contribution["beneficiary_agent_id"] = beneficiary["agent_id"]
        contribution["rate"] = format(ANTIMATTER_RATE, "f")
        contribution["contribution_id"] = contribution_id
        if contribution_record["status"] != "fulfilled":
            published = self.mailbox.antimatter_fulfill_contribution(
                contribution_id,
                transfer["signature"],
                {
                    "rail": f"solana:{self.network}",
                    "verified_transfer": contribution,
                },
            )
            if not published.get("success"):
                return {
                    **published,
                    "payment_succeeded": True,
                    "primary_verified": True,
                    "contribution": contribution,
                    "warning": "Contribution is confirmed and journaled, but its proof was not relayed; retry settle.",
                }
        verification = {
            "rail": f"solana:{self.network}",
            "primary": primary,
            "contribution": contribution,
        }
        confirmation = self.mailbox.antimatter_confirm(
            record["peer_id"],
            settlement_id,
            receipt["id"],
            verification,
            note,
        )
        return {
            **confirmation,
            "primary_verified": True,
            "contribution": contribution,
        }


__all__ = ["ANTIMATTER_RATE", "PaymentJournal", "SolanaPaymentService"]
