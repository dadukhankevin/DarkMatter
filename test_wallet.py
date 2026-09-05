"""Solana wallet bindings and the usable AntiMatter payment workflow."""

from __future__ import annotations

import asyncio

import pytest

from darkmatter.gitbox.mailbox import Mailbox, reset_mailbox
from darkmatter.mcp.schemas import WalletAction, WalletInput
from darkmatter.mcp.tools import wallet as wallet_tool
from darkmatter.wallet.claims import create_wallet_claim, verify_wallet_claim
from darkmatter.wallet.payments import SolanaPaymentService
from darkmatter.wallet.solana import MAINNET_ACK, SolanaWallet, WalletError
from darkmatter.wallet.tokens import (
    DEVNET_USDC_MINT,
    MAINNET_DM_MINT,
    MAINNET_USDC_MINT,
    MAINNET_USDT_MINT,
    TokenInfo,
    list_tokens,
)

try:
    from solders.keypair import Keypair
    from solders.signature import Signature
except ImportError:
    Keypair = None
    Signature = None


class _Ctx:
    session = object()


@pytest.fixture(autouse=True)
def _reset():
    reset_mailbox()
    yield
    reset_mailbox()


def _connected(tmp_path) -> tuple[Mailbox, Mailbox]:
    payer = Mailbox(tmp_path / "payer")
    payee = Mailbox(tmp_path / "payee")
    assert payer.introduce(payee.remote)["success"]
    assert payee.introduce(payer.remote)["success"]
    payee.sync()
    assert payee.accept(payer.agent_id)["success"]
    payer.sync()
    return payer, payee


class FakeWallet:
    def __init__(self, root, address: str, chain: dict):
        self.root = root
        self.address = address
        self.chain = chain
        self.network = "devnet"
        self.key_path = root / ".darkmatter" / "wallets" / "fake.key"

    def resolve_asset(self, asset: str):
        assert asset == "SOL"
        return TokenInfo("SOL", "devnet", 9)

    def balance(self, asset: str):
        return {"asset": asset, "amount": "10"}

    def transfer(self, destination_wallet, amount, asset, *, allow_create_ata=False):
        index = len(self.chain) + 1
        signature = f"tx-{index}"
        transfer = {
            "network": "devnet",
            "signature": signature,
            "explorer_url": f"https://example.invalid/{signature}",
            "asset": self.resolve_asset(asset).to_dict(),
            "amount": str(amount),
            "amount_base_units": str(int(float(amount) * 1_000_000_000)),
            "source_wallet": self.address,
            "destination_wallet": destination_wallet,
            "source_account": self.address,
            "destination_account": destination_wallet,
        }
        self.chain[signature] = transfer
        return transfer

    def verify_transfer(
        self,
        signature,
        *,
        source_wallet,
        destination_wallet,
        amount,
        asset,
    ):
        transfer = self.chain[signature]
        if transfer["source_wallet"] != source_wallet:
            raise WalletError("wrong source")
        if transfer["destination_wallet"] != destination_wallet:
            raise WalletError("wrong destination")
        if transfer["amount"] != str(amount) or asset != "SOL":
            raise WalletError("wrong amount or asset")
        return {**transfer, "verified": True, "commitment": "confirmed"}


class FakeRPC:
    def __init__(self, transaction=None):
        self.transaction = transaction
        self.calls = []
        self.signature = str(Signature.default()) if Signature else ""

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "getAccountInfo":
            return {"value": None}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": "11111111111111111111111111111111"}}
        if method == "simulateTransaction":
            return {"value": {"err": None, "logs": []}}
        if method == "sendTransaction":
            return self.signature
        if method == "getSignatureStatuses":
            return {"value": [{"err": None, "confirmationStatus": "confirmed"}]}
        if method == "getTransaction":
            return self.transaction
        raise AssertionError(f"unexpected RPC method: {method}")


def test_original_named_token_catalog_is_preserved():
    devnet = {item["symbol"]: item for item in list_tokens("devnet")}
    mainnet = {item["symbol"]: item for item in list_tokens("mainnet-beta")}
    assert devnet["USDC"]["mint"] == DEVNET_USDC_MINT
    assert set(devnet) == {"SOL", "USDC"}
    assert mainnet["DM"]["mint"] == MAINNET_DM_MINT
    assert mainnet["USDC"]["mint"] == MAINNET_USDC_MINT
    assert mainnet["USDT"]["mint"] == MAINNET_USDT_MINT
    assert mainnet["DM"]["token_program"].startswith("TokenzQ")


def test_mainnet_spending_requires_explicit_opt_in(tmp_path, monkeypatch):
    wallet = SolanaWallet(tmp_path, network="mainnet-beta")
    monkeypatch.delenv("DARKMATTER_SOLANA_ENABLE_MAINNET", raising=False)
    with pytest.raises(WalletError, match="Mainnet spending is locked"):
        wallet._assert_spend_allowed()
    monkeypatch.setenv("DARKMATTER_SOLANA_ENABLE_MAINNET", MAINNET_ACK)
    wallet._assert_spend_allowed()


@pytest.mark.skipif(Keypair is None, reason="optional Solana dependencies are not installed")
def test_native_transfer_is_built_simulated_sent_and_verified(tmp_path):
    sender = Keypair()
    recipient = Keypair()
    rpc = FakeRPC()
    wallet = SolanaWallet(tmp_path, network="devnet", rpc=rpc)
    wallet._keypair = sender
    sent = wallet.transfer(str(recipient.pubkey()), "0.25", "SOL")
    assert sent["amount_base_units"] == "250000000"
    assert [method for method, _ in rpc.calls][-4:] == [
        "getLatestBlockhash",
        "simulateTransaction",
        "sendTransaction",
        "getSignatureStatuses",
    ]

    rpc.transaction = {
        "slot": 42,
        "blockTime": 1_700_000_000,
        "meta": {"err": None, "innerInstructions": []},
        "transaction": {
            "message": {
                "instructions": [{
                    "program": "system",
                    "parsed": {
                        "type": "transfer",
                        "info": {
                            "source": str(sender.pubkey()),
                            "destination": str(recipient.pubkey()),
                            "lamports": 250_000_000,
                        },
                    },
                }],
            },
        },
    }
    verified = wallet.verify_transfer(
        sent["signature"],
        source_wallet=str(sender.pubkey()),
        destination_wallet=str(recipient.pubkey()),
        amount="0.25",
        asset="SOL",
    )
    assert verified["verified"] is True
    assert verified["slot"] == 42
    with pytest.raises(WalletError, match="exact expected transfer"):
        wallet.verify_transfer(
            sent["signature"],
            source_wallet=str(sender.pubkey()),
            destination_wallet=str(recipient.pubkey()),
            amount="0.2",
            asset="SOL",
        )


@pytest.mark.skipif(Keypair is None, reason="optional Solana dependencies are not installed")
def test_wallet_claim_is_passport_signed_and_tamper_evident(tmp_path):
    mailbox = Mailbox(tmp_path)
    address = str(Keypair().pubkey())
    claim = create_wallet_claim(
        mailbox.store.private_key_hex,
        mailbox.agent_id,
        address,
        network="devnet",
    )
    assert verify_wallet_claim(
        claim,
        expected_agent_id=mailbox.agent_id,
        network="devnet",
    )["address"] == address
    claim["address"] = str(Keypair().pubkey())
    with pytest.raises(ValueError, match="signature"):
        verify_wallet_claim(claim)


@pytest.mark.skipif(Keypair is None, reason="optional Solana dependencies are not installed")
def test_wallet_key_is_separate_from_passport_and_private(tmp_path):
    mailbox = Mailbox(tmp_path)
    wallet = SolanaWallet(tmp_path, network="devnet")
    assert wallet.address
    assert wallet.key_path != mailbox.store.passport_path()
    assert wallet.key_path.stat().st_mode & 0o777 == 0o600
    assert wallet.key_path.read_text().strip() != mailbox.store.private_key_hex


@pytest.mark.skipif(Keypair is None, reason="optional Solana dependencies are not installed")
def test_solana_antimatter_payment_and_routed_contribution(tmp_path):
    delegate = Mailbox(tmp_path / "delegate")
    delegate.store.save_settings(antimatter_auto_route=False)
    payer, payee = _connected(tmp_path)
    assert payee.introduce(delegate.remote)["success"]
    assert delegate.introduce(payee.remote)["success"]
    delegate.sync()
    assert delegate.accept(payee.agent_id)["success"]
    payee.sync()
    chain = {}
    payer_wallet = FakeWallet(payer.root, str(Keypair().pubkey()), chain)
    payee_wallet = FakeWallet(payee.root, str(Keypair().pubkey()), chain)
    delegate_address = str(Keypair().pubkey())
    delegate_claim = create_wallet_claim(
        delegate.store.private_key_hex,
        delegate.agent_id,
        delegate_address,
        network="devnet",
    )
    payer_service = SolanaPaymentService(payer, network="devnet", wallet=payer_wallet)
    payee_service = SolanaPaymentService(payee, network="devnet", wallet=payee_wallet)

    offered = payer_service.offer(
        payee.agent_id,
        "Ship the release",
        "1",
        "SOL",
    )
    settlement_id = offered["settlement"]["settlement_id"]
    payee.sync()
    assert payee.antimatter_accept(payer.agent_id, settlement_id)["success"]
    payer.sync()
    assert payee_service.invoice(settlement_id)["success"]
    payer.sync()

    with pytest.raises(WalletError, match="confirm_external"):
        payer_service.pay(settlement_id, confirm_external=False)
    paid = payer_service.pay(settlement_id, confirm_external=True)
    assert paid["payment_succeeded"]
    payee.sync()
    verified = payee_service.verify(settlement_id)
    assert verified["verification"]["amount"] == "1"
    assert payee.store.get_relationship(payer.agent_id).trust == 0

    pending = payee_service.settle(settlement_id, confirm_external=True)
    assert pending["settlement_pending"] is True
    contribution_id = pending["contribution_id"]
    delegate.sync()
    resolved = delegate.antimatter_advance_contribution(
        contribution_id,
        resolve_here=True,
        destination={"rail": "solana:devnet", "wallet_claim": delegate_claim},
    )
    assert resolved["success"]
    payee.sync()

    settled = payee_service.settle(settlement_id, confirm_external=True)
    assert settled["contribution"]["amount"] == "0.01"
    assert settled["contribution"]["destination_wallet"] == delegate_address
    assert settled["contribution"]["beneficiary_agent_id"] == delegate.agent_id
    assert len(chain) == 2
    assert payee.store.get_relationship(payer.agent_id).trust == 0
    delegate.sync()
    assert delegate.get_contribution(contribution_id)["status"] == "fulfilled"
    payer.sync()
    assert payer.get_settlement(settlement_id)["status"] == "settled"
    assert payer.store.get_relationship(payee.agent_id).trust == 0


def test_wallet_mcp_lists_safe_devnet_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKMATTER_PROJECT_DIR", str(tmp_path))
    params = WalletInput(action=WalletAction.TOKENS)
    result = asyncio.run(wallet_tool(params, _Ctx()))
    parsed = __import__("json").loads(result)
    assert parsed["success"] is True
    assert parsed["network"] == "devnet"
    assert parsed["network_context"] == {
        "network": "devnet",
        "environment": "test",
        "real_assets": False,
        "alert": "SOLANA DEVNET — TEST NETWORK AND TEST ASSETS ONLY; NOT REAL VALUE.",
    }
    assert parsed["network_alert"].startswith("SOLANA DEVNET")
    assert {item["symbol"] for item in parsed["tokens"]} == {"SOL", "USDC"}


def test_wallet_mcp_alerts_on_mainnet_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKMATTER_PROJECT_DIR", str(tmp_path))
    listed = asyncio.run(wallet_tool(
        WalletInput(action=WalletAction.TOKENS, network="mainnet-beta"),
        _Ctx(),
    ))
    parsed = __import__("json").loads(listed)
    assert parsed["network"] == "mainnet-beta"
    assert parsed["network_context"]["environment"] == "live"
    assert parsed["network_context"]["real_assets"] is True
    assert "REAL ASSETS" in parsed["network_alert"]
    assert "DM" in {item["symbol"] for item in parsed["tokens"]}

    failed = asyncio.run(wallet_tool(
        WalletInput(action=WalletAction.QUOTE, network="devnet"),
        _Ctx(),
    ))
    error = __import__("json").loads(failed)
    assert error["success"] is False
    assert error["network_alert"].startswith("SOLANA DEVNET")


@pytest.mark.skipif(Keypair is None, reason="optional Solana dependencies are not installed")
@pytest.mark.parametrize("mode", ["observe", "decline"])
def test_nonparticipation_settles_without_contribution_spending(tmp_path, mode):
    payer, payee = _connected(tmp_path)
    chain = {}
    payer_service = SolanaPaymentService(payer, network="devnet", wallet=FakeWallet(payer.root, str(Keypair().pubkey()), chain))
    payee_service = SolanaPaymentService(payee, network="devnet", wallet=FakeWallet(payee.root, str(Keypair().pubkey()), chain))
    offered = payer.antimatter_offer(payee.agent_id, "Tiny payment", "0.000000001", "SOL", "solana:devnet", contribution_mode=mode)
    sid = offered["settlement"]["settlement_id"]
    payee.sync()
    assert payee.antimatter_accept(payer.agent_id, sid)["success"]
    payer.sync()
    assert payee_service.invoice(sid)["success"]
    payer.sync()
    assert payer_service.quote(sid)["contribution_base_units"] == "0"
    assert payer_service.pay(sid, confirm_external=True)["payment_succeeded"]
    payee.sync()
    before = dict(chain)
    settled = payee_service.settle(sid, confirm_external=False)
    assert settled["success"] and settled["primary_verified"]
    assert settled["contribution"]["status"] == "not_committed"
    assert chain == before


@pytest.mark.skipif(Keypair is None, reason="optional Solana dependencies are not installed")
def test_wallet_rejects_contribution_for_different_primary_receipt(tmp_path):
    payer, payee = _connected(tmp_path)
    chain = {}
    payer_service = SolanaPaymentService(payer, network="devnet", wallet=FakeWallet(payer.root, str(Keypair().pubkey()), chain))
    payee_service = SolanaPaymentService(payee, network="devnet", wallet=FakeWallet(payee.root, str(Keypair().pubkey()), chain))
    sid = payer_service.offer(payee.agent_id, "Review", "1", "SOL")["settlement"]["settlement_id"]
    payee.sync()
    assert payee.antimatter_accept(payer.agent_id, sid)["success"]
    payer.sync()
    assert payee_service.invoice(sid)["success"]
    payer.sync()
    assert payer_service.pay(sid, confirm_external=True)["payment_succeeded"]
    payee.sync()
    first = payee.get_settlement(sid)["receipts"][-1]
    assert payee.antimatter_contribute(sid)["success"]
    second = payer.antimatter_receipt(payee.agent_id, sid, first["body"]["tx_id"], first["body"]["proof"])
    assert second["success"]
    payee.sync()
    before = dict(chain)
    with pytest.raises(WalletError, match="selected primary receipt"):
        payee_service.settle(sid, confirm_external=True, receipt_id=second["envelope_id"])
    assert chain == before
