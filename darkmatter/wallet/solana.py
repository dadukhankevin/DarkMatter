"""
Solana wallet provider — balance, send SOL/SPL, derive keypair, identity attestation.

Depends on: config, wallet/__init__
"""

import hashlib as _hashlib
import struct as _struct
from typing import Optional

from solders.keypair import Keypair as SolanaKeypair
from solders.pubkey import Pubkey as SolanaPubkey
from solders.system_program import transfer as sol_transfer, TransferParams as SolTransferParams
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.instruction import Instruction as SolInstruction, AccountMeta
from solana.rpc.async_api import AsyncClient as SolanaClient

from darkmatter.config import (
    SOLANA_RPC_URL,
    LAMPORTS_PER_SOL,
    SPL_TOKENS,
)
from darkmatter.wallet import WalletProvider, register_provider

from solders.signature import Signature as SolSignature

# Well-known program IDs
TOKEN_PROGRAM_ID = SolanaPubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = SolanaPubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = SolanaPubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = SolanaPubkey.from_string("11111111111111111111111111111111")
SYSVAR_RENT_ID = SolanaPubkey.from_string("SysvarRent111111111111111111111111111111111")
MEMO_PROGRAM_ID = SolanaPubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")

# DarkMatter identity attestation prefix
ATTESTATION_PREFIX = "dm:passport:"


def _resolve_spl_token(token_or_mint: str) -> Optional[tuple[str, int]]:
    """Resolve a token name or mint address. Returns (mint, decimals) or None."""
    upper = token_or_mint.upper()
    if upper in SPL_TOKENS:
        return SPL_TOKENS[upper]
    if len(token_or_mint) >= 32:
        return None
    return None


def _get_associated_token_address(owner: SolanaPubkey, mint: SolanaPubkey,
                                   token_program_id: SolanaPubkey = TOKEN_PROGRAM_ID) -> SolanaPubkey:
    """Derive the associated token account address for an owner + mint."""
    return SolanaPubkey.find_program_address(
        [bytes(owner), bytes(token_program_id), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


def _create_associated_token_account_ix(payer: SolanaPubkey, owner: SolanaPubkey,
                                         mint: SolanaPubkey,
                                         token_program_id: SolanaPubkey = TOKEN_PROGRAM_ID) -> SolInstruction:
    """Build a CreateAssociatedTokenAccount instruction using raw solders."""
    ata = _get_associated_token_address(owner, mint, token_program_id)
    return SolInstruction(
        program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
            AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSVAR_RENT_ID, is_signer=False, is_writable=False),
        ],
        data=b"",
    )


def _transfer_checked_ix(source: SolanaPubkey, mint: SolanaPubkey,
                          dest: SolanaPubkey, owner: SolanaPubkey,
                          amount: int, decimals: int,
                          token_program_id: SolanaPubkey = TOKEN_PROGRAM_ID) -> SolInstruction:
    """Build a TransferChecked instruction (SPL Token instruction #12)."""
    data = _struct.pack("<BQB", 12, amount, decimals)
    return SolInstruction(
        program_id=token_program_id,
        accounts=[
            AccountMeta(pubkey=source, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=dest, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
        ],
        data=data,
    )


async def _detect_token_program(client: SolanaClient, mint: SolanaPubkey) -> SolanaPubkey:
    """Detect whether a mint uses Token or Token-2022 by checking its owner program."""
    resp = await client.get_account_info(mint)
    if resp.value and resp.value.owner == TOKEN_2022_PROGRAM_ID:
        return TOKEN_2022_PROGRAM_ID
    return TOKEN_PROGRAM_ID


def _derive_solana_keypair(private_key_hex: str) -> SolanaKeypair:
    """Derive a Solana keypair from the passport private key with domain separation."""
    seed = _hashlib.sha256(bytes.fromhex(private_key_hex) + b"darkmatter-solana-v1").digest()
    return SolanaKeypair.from_seed(seed)


def _get_solana_wallet_address(private_key_hex: str) -> str:
    """Get the Solana wallet address (base58 public key) for this agent."""
    return str(_derive_solana_keypair(private_key_hex).pubkey())


async def get_solana_balance(wallets: dict, mint: str = None) -> dict:
    """Get SOL or SPL token balance."""
    sol_addr = wallets.get("solana")
    if not sol_addr:
        return {"success": False, "error": "Solana wallet not available"}

    pubkey = SolanaPubkey.from_string(sol_addr)

    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            if mint is None:
                resp = await client.get_balance(pubkey)
                lamports = resp.value
                return {
                    "success": True,
                    "token": "SOL",
                    "balance": lamports / LAMPORTS_PER_SOL,
                    "lamports": lamports,
                    "wallet_address": sol_addr,
                }
            else:
                mint_pubkey = SolanaPubkey.from_string(mint)
                token_prog = await _detect_token_program(client, mint_pubkey)
                ata = _get_associated_token_address(pubkey, mint_pubkey, token_prog)
                resp = await client.get_token_account_balance(ata)
                if resp.value is None:
                    return {
                        "success": True,
                        "token": mint,
                        "balance": 0,
                        "wallet_address": sol_addr,
                        "note": "No token account found",
                    }
                return {
                    "success": True,
                    "token": mint,
                    "balance": float(resp.value.ui_amount_string),
                    "decimals": resp.value.decimals,
                    "wallet_address": sol_addr,
                }
    except Exception as e:
        err_str = str(e)
        if "could not find account" in err_str.lower() or "invalid param" in err_str.lower():
            return {
                "success": True,
                "token": mint or "SOL",
                "balance": 0,
                "wallet_address": sol_addr,
                "note": "No token account found",
            }
        return {"success": False, "error": f"RPC error: {err_str}"}


async def send_solana_sol(private_key_hex: str, wallets: dict,
                          recipient_wallet: str, amount: float) -> dict:
    """Send SOL to a recipient wallet."""
    sol_addr = wallets.get("solana")
    if not sol_addr:
        return {"success": False, "error": "Solana wallet not available"}

    sender_kp = _derive_solana_keypair(private_key_hex)
    sender_pubkey = sender_kp.pubkey()
    recipient_pubkey = SolanaPubkey.from_string(recipient_wallet)
    lamports = int(amount * LAMPORTS_PER_SOL)

    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            ix = sol_transfer(SolTransferParams(
                from_pubkey=sender_pubkey,
                to_pubkey=recipient_pubkey,
                lamports=lamports,
            ))
            bh_resp = await client.get_latest_blockhash()
            blockhash = bh_resp.value.blockhash
            msg = MessageV0.try_compile(
                payer=sender_pubkey,
                instructions=[ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [sender_kp])
            tx_resp = await client.send_transaction(tx)
            tx_signature = str(tx_resp.value)

        return {
            "success": True,
            "tx_signature": tx_signature,
            "amount": amount,
            "from_wallet": str(sender_pubkey),
            "to_wallet": recipient_wallet,
        }
    except Exception as e:
        return {"success": False, "error": f"Transaction failed: {str(e)}"}


async def send_solana_token(private_key_hex: str, wallets: dict,
                            recipient_wallet: str, mint: str,
                            amount: float, decimals: int) -> dict:
    """Send SPL tokens (legacy Token or Token-2022) to a recipient wallet."""
    sol_addr = wallets.get("solana")
    if not sol_addr:
        return {"success": False, "error": "Solana wallet not available"}

    sender_kp = _derive_solana_keypair(private_key_hex)
    sender_pubkey = sender_kp.pubkey()
    recipient_pubkey = SolanaPubkey.from_string(recipient_wallet)
    mint_pubkey = SolanaPubkey.from_string(mint)

    raw_amount = int(amount * (10 ** decimals))

    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            # Detect which token program this mint uses
            token_program_id = await _detect_token_program(client, mint_pubkey)

            sender_ata = _get_associated_token_address(sender_pubkey, mint_pubkey, token_program_id)
            recipient_ata = _get_associated_token_address(recipient_pubkey, mint_pubkey, token_program_id)

            instructions = []
            created_ata = False

            ata_info = await client.get_account_info(recipient_ata)
            if ata_info.value is None:
                instructions.append(_create_associated_token_account_ix(
                    payer=sender_pubkey,
                    owner=recipient_pubkey,
                    mint=mint_pubkey,
                    token_program_id=token_program_id,
                ))
                created_ata = True

            instructions.append(_transfer_checked_ix(
                source=sender_ata,
                mint=mint_pubkey,
                dest=recipient_ata,
                owner=sender_pubkey,
                amount=raw_amount,
                decimals=decimals,
                token_program_id=token_program_id,
            ))

            bh_resp = await client.get_latest_blockhash()
            blockhash = bh_resp.value.blockhash
            msg = MessageV0.try_compile(
                payer=sender_pubkey,
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [sender_kp])
            tx_resp = await client.send_transaction(tx)
            tx_signature = str(tx_resp.value)

        return {
            "success": True,
            "tx_signature": tx_signature,
            "amount": amount,
            "token_mint": mint,
            "decimals": decimals,
            "from_wallet": str(sender_pubkey),
            "to_wallet": recipient_wallet,
            "created_recipient_ata": created_ata,
        }
    except Exception as e:
        return {"success": False, "error": f"Transaction failed: {str(e)}"}


async def attest_solana_identity(private_key_hex: str, wallets: dict,
                                  agent_id: str) -> dict:
    """Create a Memo transaction embedding the agent's passport public key on Solana.

    The memo contains "dm:passport:{agent_id}" and is signed by the wallet keypair,
    creating an immutable on-chain proof that this wallet belongs to this agent.
    """
    sol_addr = wallets.get("solana")
    if not sol_addr:
        return {"success": False, "error": "Solana wallet not available"}

    sender_kp = _derive_solana_keypair(private_key_hex)
    sender_pubkey = sender_kp.pubkey()
    memo_data = f"{ATTESTATION_PREFIX}{agent_id}".encode("utf-8")

    memo_ix = SolInstruction(
        program_id=MEMO_PROGRAM_ID,
        accounts=[AccountMeta(pubkey=sender_pubkey, is_signer=True, is_writable=True)],
        data=memo_data,
    )

    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            bh_resp = await client.get_latest_blockhash()
            blockhash = bh_resp.value.blockhash
            msg = MessageV0.try_compile(
                payer=sender_pubkey,
                instructions=[memo_ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [sender_kp])
            tx_resp = await client.send_transaction(tx)
            tx_signature = str(tx_resp.value)

        return {
            "success": True,
            "tx_signature": tx_signature,
            "memo": memo_data.decode("utf-8"),
            "wallet_address": sol_addr,
        }
    except Exception as e:
        return {"success": False, "error": f"Attestation failed: {str(e)}"}


async def verify_solana_identity_attestation(address: str, agent_id: str) -> dict:
    """Search a Solana wallet's tx history for a DarkMatter passport attestation.

    Looks for a Memo instruction containing "dm:passport:" prefix.
    Returns dict with status: "match", "mismatch", or "none".
    """
    pubkey = SolanaPubkey.from_string(address)
    expected_memo = f"{ATTESTATION_PREFIX}{agent_id}"

    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            before = None
            while True:
                kwargs = {"limit": 1000}
                if before:
                    kwargs["before"] = before
                resp = await client.get_signatures_for_address(pubkey, **kwargs)
                if not resp.value:
                    break

                for sig_info in resp.value:
                    if sig_info.err is not None:
                        continue
                    if sig_info.memo and ATTESTATION_PREFIX in sig_info.memo:
                        from datetime import datetime, timezone
                        ts = None
                        if sig_info.block_time is not None:
                            ts = datetime.fromtimestamp(
                                sig_info.block_time, tz=timezone.utc
                            ).isoformat()

                        if expected_memo in sig_info.memo:
                            return {"status": "match", "timestamp": ts}
                        else:
                            # Attestation exists but for a different agent
                            attested_id = sig_info.memo.split(ATTESTATION_PREFIX, 1)[-1].strip()
                            return {
                                "status": "mismatch",
                                "timestamp": ts,
                                "attested_agent_id": attested_id,
                            }

                if len(resp.value) < 1000:
                    break
                before = resp.value[-1].signature
    except Exception:
        pass
    return {"status": "none"}


async def get_latest_signature(address: str) -> Optional[str]:
    """Get the most recent transaction signature for an address."""
    pubkey = SolanaPubkey.from_string(address)
    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            resp = await client.get_signatures_for_address(pubkey, limit=1)
            if resp.value:
                return str(resp.value[0].signature)
    except Exception:
        pass
    return None


async def get_inbound_sol_transfers(address: str, sender: Optional[str] = None,
                                     after_signature: Optional[str] = None,
                                     limit: int = 20) -> list[dict]:
    """Get recent inbound SOL transfers to address, optionally filtered by sender."""
    pubkey = SolanaPubkey.from_string(address)
    transfers = []
    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            kwargs = {"limit": limit}
            if after_signature:
                kwargs["until"] = SolSignature.from_string(after_signature)
            resp = await client.get_signatures_for_address(pubkey, **kwargs)
            if not resp.value:
                return []

            for sig_info in resp.value:
                if sig_info.err is not None:
                    continue
                sig = sig_info.signature
                tx_resp = await client.get_transaction(
                    sig, max_supported_transaction_version=0,
                )
                if not tx_resp.value:
                    continue

                tx_val = tx_resp.value
                meta = tx_val.transaction.meta
                msg = tx_val.transaction.transaction.message
                if meta is None:
                    continue

                account_keys = list(msg.account_keys)
                pre_balances = list(meta.pre_balances)
                post_balances = list(meta.post_balances)

                # Find which account is our target
                target_idx = None
                for i, key in enumerate(account_keys):
                    if str(key) == address:
                        target_idx = i
                        break
                if target_idx is None:
                    continue

                # Check if target received SOL
                diff = post_balances[target_idx] - pre_balances[target_idx]
                if diff <= 0:
                    continue

                # Identify the sender (account that lost SOL, excluding fee payer overhead)
                tx_sender = None
                for i, key in enumerate(account_keys):
                    if i == target_idx:
                        continue
                    loss = pre_balances[i] - post_balances[i]
                    if loss > 0:
                        tx_sender = str(key)
                        break

                if sender and tx_sender != sender:
                    continue

                transfers.append({
                    "sender": tx_sender,
                    "amount": diff / LAMPORTS_PER_SOL,
                    "signature": str(sig),
                    "timestamp": sig_info.block_time,
                })
    except Exception:
        pass
    return transfers


class SolanaWalletProvider(WalletProvider):
    """Solana wallet provider implementation."""
    chain = "solana"

    def derive_address(self, private_key_hex: str) -> str:
        return _get_solana_wallet_address(private_key_hex)

    async def get_balance(self, address: str, mint: Optional[str] = None) -> dict:
        return await get_solana_balance({"solana": address}, mint)

    async def get_all_balances(self, address: str, limit: int = 100) -> dict:
        """Get SOL balance + all SPL token holdings."""
        pubkey = SolanaPubkey.from_string(address)
        native = await self.get_balance(address)
        tokens = []
        # Reverse lookup: mint → symbol from SPL_TOKENS config
        mint_to_symbol = {mint: name for name, (mint, _dec) in SPL_TOKENS.items()}
        try:
            from solana.rpc.types import TokenAccountOpts
            async with SolanaClient(SOLANA_RPC_URL) as client:
                # Query both legacy Token and Token-2022 programs
                for program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
                    resp = await client.get_token_accounts_by_owner_json_parsed(
                        pubkey, TokenAccountOpts(program_id=program_id),
                    )
                    if resp.value:
                        for acct in resp.value[:limit]:
                            try:
                                info = acct.account.data.parsed["info"]
                                mint = info["mint"]
                                amount = info["tokenAmount"]
                                ui_amount = float(amount.get("uiAmountString", "0"))
                                if ui_amount <= 0:
                                    continue
                                entry = {
                                    "mint": mint,
                                    "balance": ui_amount,
                                    "decimals": amount.get("decimals", 0),
                                }
                                symbol = mint_to_symbol.get(mint)
                                if symbol:
                                    entry["symbol"] = symbol
                                tokens.append(entry)
                            except (KeyError, TypeError, ValueError):
                                continue
        except Exception:
            pass
        # Sort by balance descending
        tokens.sort(key=lambda t: t["balance"], reverse=True)
        return {
            "success": native.get("success", False),
            "native": native if native.get("success") else {"error": native.get("error")},
            "tokens": tokens,
        }

    async def send(self, private_key_hex: str, wallets: dict, recipient: str,
                   amount: float, token: Optional[str] = None,
                   decimals: int = 9) -> dict:
        if token is None or token.upper() == "SOL":
            return await send_solana_sol(private_key_hex, wallets, recipient, amount)
        else:
            return await send_solana_token(
                private_key_hex, wallets, recipient, token, amount, decimals
            )

    async def get_inbound_transfers(self, address: str, sender: Optional[str] = None,
                                     token: Optional[str] = None,
                                     after_signature: Optional[str] = None,
                                     limit: int = 20) -> list[dict]:
        # TODO: SPL token transfer tracking (requires parsing token program instructions)
        # For now, SOL transfers are fully tracked; SPL falls back to balance diff
        return await get_inbound_sol_transfers(
            address, sender=sender, after_signature=after_signature, limit=limit,
        )

    async def attest_identity(self, private_key_hex: str, wallets: dict,
                               agent_id: str) -> dict:
        return await attest_solana_identity(private_key_hex, wallets, agent_id)

    async def verify_identity_attestation(self, address: str,
                                           agent_id: str) -> dict:
        return await verify_solana_identity_attestation(address, agent_id)

    async def get_wallet_age(self, address: str) -> Optional[str]:
        """Get the timestamp of the wallet's earliest transaction on Solana.

        Paginates backwards through getSignaturesForAddress until the oldest
        signature is found. For typical DarkMatter agent wallets with modest
        tx history, this is fast.
        """
        pubkey = SolanaPubkey.from_string(address)
        try:
            async with SolanaClient(SOLANA_RPC_URL) as client:
                oldest_sig = None
                oldest_time = None
                before = None

                while True:
                    kwargs = {"limit": 1000}
                    if before:
                        kwargs["before"] = before
                    resp = await client.get_signatures_for_address(pubkey, **kwargs)
                    if not resp.value:
                        break
                    last = resp.value[-1]
                    oldest_sig = last.signature
                    oldest_time = last.block_time
                    if len(resp.value) < 1000:
                        break  # reached the end
                    before = oldest_sig

                if oldest_time is not None:
                    from datetime import datetime, timezone
                    return datetime.fromtimestamp(oldest_time, tz=timezone.utc).isoformat()
        except Exception:
            pass
        return None


# Auto-register
register_provider(SolanaWalletProvider())
