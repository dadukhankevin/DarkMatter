"""Small Solana client used only by explicitly confirmed AntiMatter actions.

The core mailbox has no blockchain dependency. Imports from solders/solana-py
stay lazy so messaging continues to work when the optional wallet extra is not
installed.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

from darkmatter.store.local import atomic_write_text
from darkmatter.wallet.tokens import TokenInfo, normalize_network, resolve_token


DEFAULT_RPC = {
    "devnet": "https://api.devnet.solana.com",
    "mainnet-beta": "https://api.mainnet-beta.solana.com",
}
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
MAINNET_ACK = "I_UNDERSTAND"


class WalletError(ValueError):
    """A wallet request is invalid, unsafe, unavailable, or failed on-chain."""


def network_context(network: str | None) -> dict:
    """Return an agent-facing banner that makes the selected network explicit."""
    selected = normalize_network(network)
    if selected == "devnet":
        return {
            "network": selected,
            "environment": "test",
            "real_assets": False,
            "alert": "SOLANA DEVNET — TEST NETWORK AND TEST ASSETS ONLY; NOT REAL VALUE.",
        }
    return {
        "network": selected,
        "environment": "live",
        "real_assets": True,
        "alert": "SOLANA MAINNET-BETA — LIVE NETWORK; TRANSACTIONS USE REAL ASSETS.",
    }


class SolanaRPC:
    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url
        self.timeout = timeout
        self._request_id = 0

    def call(self, method: str, params: list | None = None):
        self._request_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise WalletError(f"Solana RPC request failed: {exc}") from exc
        if decoded.get("error"):
            error = decoded["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise WalletError(f"Solana RPC {method} failed: {message}")
        if "result" not in decoded:
            raise WalletError(f"Solana RPC {method} returned no result")
        return decoded["result"]


def _dependencies():
    try:
        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.signature import Signature
        from solders.system_program import TransferParams, transfer
        from solders.transaction import VersionedTransaction
        from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
        from spl.token.instructions import (
            create_associated_token_account,
            get_associated_token_address,
            transfer_checked,
        )
        try:
            # solana-py <=0.36 exported parameter records here.
            from spl.token.instructions import TransferCheckedParams
        except ImportError:
            # solana-py 0.40 moved them to the models module.
            from spl.token.models import TransferCheckedParams
    except ImportError as exc:
        raise WalletError(
            "Solana wallet support is not installed; run: pip install 'dmagent[solana]'",
        ) from exc
    return {
        "Hash": Hash,
        "Keypair": Keypair,
        "MessageV0": MessageV0,
        "Pubkey": Pubkey,
        "Signature": Signature,
        "TransferParams": TransferParams,
        "transfer": transfer,
        "VersionedTransaction": VersionedTransaction,
        "TOKEN_PROGRAM_ID": TOKEN_PROGRAM_ID,
        "TOKEN_2022_PROGRAM_ID": TOKEN_2022_PROGRAM_ID,
        "TransferCheckedParams": TransferCheckedParams,
        "create_associated_token_account": create_associated_token_account,
        "get_associated_token_address": get_associated_token_address,
        "transfer_checked": transfer_checked,
    }


def amount_to_base_units(amount: object, decimals: int) -> tuple[str, int]:
    if isinstance(amount, bool) or decimals < 0:
        raise WalletError("Amount and token decimals must be known")
    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise WalletError("Amount must be a decimal number") from exc
    if not value.is_finite() or value <= 0:
        raise WalletError("Amount must be finite and greater than zero")
    scaled = value * (Decimal(10) ** decimals)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise WalletError(f"Amount has more than {decimals} decimal places")
    normalized = format(value, "f").rstrip("0").rstrip(".")
    return normalized or "0", int(integral)


def format_base_units(amount: int, decimals: int) -> str:
    rendered = format(Decimal(amount) / (Decimal(10) ** decimals), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


class SolanaWallet:
    def __init__(
        self,
        root: str | Path,
        *,
        network: str | None = None,
        rpc_url: str | None = None,
        rpc: SolanaRPC | None = None,
    ):
        self.root = Path(root)
        self.network = normalize_network(
            network or os.environ.get("DARKMATTER_SOLANA_NETWORK") or "devnet",
        )
        self.rpc_url = rpc_url or os.environ.get("DARKMATTER_SOLANA_RPC") or DEFAULT_RPC[self.network]
        self.rpc = rpc or SolanaRPC(self.rpc_url)
        self._keypair = None

    @property
    def key_path(self) -> Path:
        configured = os.environ.get("DARKMATTER_SOLANA_KEYPAIR_FILE", "").strip()
        if configured:
            return Path(configured).expanduser()
        return self.root / ".darkmatter" / "wallets" / f"solana-{self.network}.key"

    @property
    def network_context(self) -> dict:
        return network_context(self.network)

    def _load_or_create_keypair(self):
        if self._keypair is not None:
            return self._keypair
        deps = _dependencies()
        Keypair = deps["Keypair"]
        path = self.key_path
        try:
            if path.exists():
                raw = path.read_text().strip()
                os.chmod(path, 0o600)
                if raw.startswith("["):
                    values = json.loads(raw)
                    if not isinstance(values, list) or len(values) != 64:
                        raise WalletError("Solana CLI keypair must contain 64 bytes")
                    self._keypair = Keypair.from_bytes(bytes(values))
                elif len(raw) == 64:
                    self._keypair = Keypair.from_seed(bytes.fromhex(raw))
                else:
                    self._keypair = Keypair.from_base58_string(raw)
            else:
                if os.environ.get("DARKMATTER_SOLANA_KEYPAIR_FILE"):
                    raise WalletError(f"Configured Solana keypair does not exist: {path}")
                seed = secrets.token_bytes(32)
                atomic_write_text(path, seed.hex() + "\n", mode=0o600)
                self._keypair = Keypair.from_seed(seed)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WalletError(f"Could not load Solana keypair: {exc}") from exc
        return self._keypair

    @property
    def address(self) -> str:
        return str(self._load_or_create_keypair().pubkey())

    @property
    def explorer_cluster(self) -> str:
        return "devnet" if self.network == "devnet" else "mainnet-beta"

    def explorer_url(self, signature: str) -> str:
        return f"https://explorer.solana.com/tx/{signature}?cluster={self.explorer_cluster}"

    def _assert_spend_allowed(self) -> None:
        if self.network == "mainnet-beta" and os.environ.get(
            "DARKMATTER_SOLANA_ENABLE_MAINNET",
        ) != MAINNET_ACK:
            raise WalletError(
                "Mainnet spending is locked. Set "
                f"DARKMATTER_SOLANA_ENABLE_MAINNET={MAINNET_ACK} to opt in explicitly.",
            )

    def _pubkey(self, value: str, label: str = "address"):
        try:
            return _dependencies()["Pubkey"].from_string(value)
        except ValueError as exc:
            raise WalletError(f"Invalid Solana {label}") from exc

    def account_info(self, address: str, encoding: str = "jsonParsed"):
        self._pubkey(address)
        result = self.rpc.call("getAccountInfo", [
            address,
            {"encoding": encoding, "commitment": "confirmed"},
        ])
        return result.get("value") if isinstance(result, dict) else None

    def validate_wallet_address(self, address: str) -> dict:
        pubkey = self._pubkey(address, "wallet address")
        if not pubkey.is_on_curve():
            raise WalletError("Recipient is off-curve and cannot be a standard wallet")
        info = self.account_info(address)
        if info is None:
            return {"address": address, "kind": "unfunded_wallet", "safe": True}
        if info.get("executable") or info.get("owner") != SYSTEM_PROGRAM:
            raise WalletError("Recipient is not a System Program wallet account")
        return {"address": address, "kind": "system_wallet", "safe": True}

    def resolve_asset(self, asset: str) -> TokenInfo:
        token = resolve_token(asset, self.network)
        if token.native:
            return token
        mint_info = self.account_info(token.mint)
        if mint_info is None:
            raise WalletError("Token mint does not exist on the selected network")
        program = mint_info.get("owner")
        if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            raise WalletError("Asset address is not owned by a supported token program")
        supply = self.rpc.call("getTokenSupply", [token.mint, {"commitment": "confirmed"}])
        value = supply.get("value") if isinstance(supply, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("decimals"), int):
            raise WalletError("Could not discover token decimals")
        if token.decimals >= 0 and token.decimals != value["decimals"]:
            raise WalletError("Token catalog decimals do not match the chain")
        return TokenInfo(
            token.symbol,
            self.network,
            value["decimals"],
            token.mint,
            program,
            token.source,
        )

    def associated_token_address(self, owner: str, token: TokenInfo) -> str:
        if token.native or not token.token_program:
            raise WalletError("A resolved SPL token is required")
        deps = _dependencies()
        program = (
            deps["TOKEN_2022_PROGRAM_ID"]
            if token.token_program == TOKEN_2022_PROGRAM
            else deps["TOKEN_PROGRAM_ID"]
        )
        return str(deps["get_associated_token_address"](
            self._pubkey(owner, "owner"),
            self._pubkey(token.mint, "mint"),
            program,
        ))

    def balance(self, asset: str = "SOL") -> dict:
        token = self.resolve_asset(asset)
        if token.native:
            result = self.rpc.call("getBalance", [self.address, {"commitment": "confirmed"}])
            raw = int(result.get("value", 0))
            account = self.address
        else:
            account = self.associated_token_address(self.address, token)
            info = self.account_info(account)
            if info is None:
                raw = 0
            else:
                result = self.rpc.call("getTokenAccountBalance", [
                    account,
                    {"commitment": "confirmed"},
                ])
                raw = int(result["value"]["amount"])
        return {
            "network": self.network,
            "address": self.address,
            "asset": token.to_dict(),
            "account": account,
            "amount": format_base_units(raw, token.decimals),
            "amount_base_units": str(raw),
        }

    def _latest_blockhash(self):
        result = self.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return _dependencies()["Hash"].from_string(result["value"]["blockhash"])

    def _send_instructions(self, instructions: list) -> str:
        self._assert_spend_allowed()
        deps = _dependencies()
        keypair = self._load_or_create_keypair()
        message = deps["MessageV0"].try_compile(
            keypair.pubkey(),
            instructions,
            [],
            self._latest_blockhash(),
        )
        transaction = deps["VersionedTransaction"](message, [keypair])
        encoded = base64.b64encode(bytes(transaction)).decode("ascii")
        simulation = self.rpc.call("simulateTransaction", [encoded, {
            "encoding": "base64",
            "sigVerify": True,
            "commitment": "processed",
        }])
        simulation_value = simulation.get("value", {})
        if simulation_value.get("err") is not None:
            logs = simulation_value.get("logs") or []
            tail = "; ".join(str(line) for line in logs[-3:])
            raise WalletError(f"Transaction simulation failed: {simulation_value['err']} {tail}".strip())
        signature = self.rpc.call("sendTransaction", [encoded, {
            "encoding": "base64",
            "skipPreflight": False,
            "preflightCommitment": "confirmed",
            "maxRetries": 5,
        }])
        self.wait_for_confirmation(signature)
        return signature

    def wait_for_confirmation(self, signature: str, timeout: float = 45.0) -> dict:
        _dependencies()["Signature"].from_string(signature)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.rpc.call("getSignatureStatuses", [[signature], {
                "searchTransactionHistory": True,
            }])
            status = (result.get("value") or [None])[0]
            if status:
                if status.get("err") is not None:
                    raise WalletError(f"Transaction failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return status
            time.sleep(0.5)
        raise WalletError("Timed out waiting for Solana transaction confirmation")

    def transfer(
        self,
        destination_wallet: str,
        amount: object,
        asset: str = "SOL",
        *,
        allow_create_ata: bool = False,
    ) -> dict:
        self._assert_spend_allowed()
        self.validate_wallet_address(destination_wallet)
        token = self.resolve_asset(asset)
        normalized, raw = amount_to_base_units(amount, token.decimals)
        deps = _dependencies()
        keypair = self._load_or_create_keypair()
        if token.native:
            source = self.address
            destination = destination_wallet
            instructions = [deps["transfer"](deps["TransferParams"](
                from_pubkey=keypair.pubkey(),
                to_pubkey=self._pubkey(destination_wallet),
                lamports=raw,
            ))]
        else:
            program = (
                deps["TOKEN_2022_PROGRAM_ID"]
                if token.token_program == TOKEN_2022_PROGRAM
                else deps["TOKEN_PROGRAM_ID"]
            )
            source = self.associated_token_address(self.address, token)
            destination = self.associated_token_address(destination_wallet, token)
            if self.account_info(source) is None:
                raise WalletError("Payer has no associated token account for this asset")
            instructions = []
            if self.account_info(destination) is None:
                if not allow_create_ata:
                    raise WalletError(
                        "Recipient token account does not exist; repeat with allow_create_ata=true "
                        "to fund its rent explicitly",
                    )
                instructions.append(deps["create_associated_token_account"](
                    keypair.pubkey(),
                    self._pubkey(destination_wallet),
                    self._pubkey(token.mint),
                    program,
                ))
            instructions.append(deps["transfer_checked"](deps["TransferCheckedParams"](
                program_id=program,
                source=self._pubkey(source),
                mint=self._pubkey(token.mint),
                dest=self._pubkey(destination),
                owner=keypair.pubkey(),
                amount=raw,
                decimals=token.decimals,
                signers=[],
            )))
        signature = self._send_instructions(instructions)
        return {
            "network": self.network,
            "signature": signature,
            "explorer_url": self.explorer_url(signature),
            "asset": token.to_dict(),
            "amount": normalized,
            "amount_base_units": str(raw),
            "source_wallet": self.address,
            "destination_wallet": destination_wallet,
            "source_account": source,
            "destination_account": destination,
        }

    @staticmethod
    def _instructions(transaction: dict) -> list[dict]:
        message = transaction.get("transaction", {}).get("message", {})
        instructions = list(message.get("instructions") or [])
        for group in transaction.get("meta", {}).get("innerInstructions") or []:
            instructions.extend(group.get("instructions") or [])
        return [item for item in instructions if isinstance(item, dict)]

    def verify_transfer(
        self,
        signature: str,
        *,
        source_wallet: str,
        destination_wallet: str,
        amount: object,
        asset: str = "SOL",
    ) -> dict:
        _dependencies()["Signature"].from_string(signature)
        token = self.resolve_asset(asset)
        normalized, raw = amount_to_base_units(amount, token.decimals)
        transaction = self.rpc.call("getTransaction", [signature, {
            "encoding": "jsonParsed",
            "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0,
        }])
        if not transaction:
            raise WalletError("Transaction was not found at confirmed commitment")
        if transaction.get("meta", {}).get("err") is not None:
            raise WalletError("Transaction exists but failed")
        if token.native:
            expected_source = source_wallet
            expected_destination = destination_wallet
            programs = {"system"}
        else:
            expected_source = self.associated_token_address(source_wallet, token)
            expected_destination = self.associated_token_address(destination_wallet, token)
            programs = {"spl-token", "spl-token-2022"}
        matched = None
        for instruction in self._instructions(transaction):
            parsed = instruction.get("parsed")
            if instruction.get("program") not in programs or not isinstance(parsed, dict):
                continue
            info = parsed.get("info") or {}
            if token.native:
                candidate_amount = info.get("lamports")
                source = info.get("source")
                destination = info.get("destination")
            else:
                token_amount = info.get("tokenAmount") or {}
                candidate_amount = token_amount.get("amount", info.get("amount"))
                source = info.get("source")
                destination = info.get("destination")
                if info.get("mint") and info.get("mint") != token.mint:
                    continue
                if info.get("authority") != source_wallet:
                    continue
            try:
                candidate_amount = int(candidate_amount)
            except (TypeError, ValueError):
                continue
            if (
                source == expected_source
                and destination == expected_destination
                and candidate_amount == raw
            ):
                matched = instruction
                break
        if matched is None:
            raise WalletError("Transaction does not contain the exact expected transfer")
        return {
            "verified": True,
            "commitment": "confirmed",
            "network": self.network,
            "signature": signature,
            "explorer_url": self.explorer_url(signature),
            "asset": token.to_dict(),
            "amount": normalized,
            "amount_base_units": str(raw),
            "source_wallet": source_wallet,
            "destination_wallet": destination_wallet,
            "source_account": expected_source,
            "destination_account": expected_destination,
            "slot": transaction.get("slot"),
            "block_time": transaction.get("blockTime"),
        }

    def request_airdrop(self, amount: object = "1") -> dict:
        if self.network != "devnet":
            raise WalletError("Airdrops are available only on devnet")
        normalized, lamports = amount_to_base_units(amount, 9)
        if lamports > 2_000_000_000:
            raise WalletError("A single devnet airdrop request is limited to 2 SOL")
        signature = self.rpc.call("requestAirdrop", [
            self.address,
            lamports,
            {"commitment": "confirmed"},
        ])
        self.wait_for_confirmation(signature)
        return {
            "success": True,
            "network": self.network,
            "address": self.address,
            "amount": normalized,
            "amount_base_units": str(lamports),
            "signature": signature,
            "explorer_url": self.explorer_url(signature),
        }


__all__ = [
    "DEFAULT_RPC",
    "MAINNET_ACK",
    "SolanaRPC",
    "SolanaWallet",
    "WalletError",
    "amount_to_base_units",
    "format_base_units",
    "network_context",
]
