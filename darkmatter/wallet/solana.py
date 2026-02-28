"""
Solana wallet provider — balance, send SOL/SPL, derive keypair.

Depends on: config, wallet/__init__
"""

from typing import Optional

from darkmatter.config import (
    SOLANA_AVAILABLE,
    SOLANA_RPC_URL,
    LAMPORTS_PER_SOL,
    SPL_TOKENS,
)
from darkmatter.wallet import WalletProvider, register_provider


def _resolve_spl_token(token_or_mint: str) -> Optional[tuple[str, int]]:
    """Resolve a token name or mint address. Returns (mint, decimals) or None."""
    upper = token_or_mint.upper()
    if upper in SPL_TOKENS:
        return SPL_TOKENS[upper]
    if len(token_or_mint) >= 32:
        return None
    return None


if SOLANA_AVAILABLE:
    import hashlib as _hashlib
    from solders.keypair import Keypair as SolanaKeypair
    from solders.pubkey import Pubkey as SolanaPubkey
    from solders.system_program import transfer as sol_transfer, TransferParams as SolTransferParams
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    from solana.rpc.async_api import AsyncClient as SolanaClient
    from spl.token.instructions import transfer_checked, TransferCheckedParams
    from spl.token.constants import TOKEN_PROGRAM_ID
    from spl.token.instructions import create_associated_token_account

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
                    ata = SolanaPubkey.find_program_address(
                        [bytes(pubkey), bytes(TOKEN_PROGRAM_ID), bytes(mint_pubkey)],
                        SolanaPubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"),
                    )[0]
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
        """Send SPL tokens to a recipient wallet."""
        sol_addr = wallets.get("solana")
        if not sol_addr:
            return {"success": False, "error": "Solana wallet not available"}

        sender_kp = _derive_solana_keypair(private_key_hex)
        sender_pubkey = sender_kp.pubkey()
        recipient_pubkey = SolanaPubkey.from_string(recipient_wallet)
        mint_pubkey = SolanaPubkey.from_string(mint)

        ata_program = SolanaPubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
        sender_ata = SolanaPubkey.find_program_address(
            [bytes(sender_pubkey), bytes(TOKEN_PROGRAM_ID), bytes(mint_pubkey)],
            ata_program,
        )[0]
        recipient_ata = SolanaPubkey.find_program_address(
            [bytes(recipient_pubkey), bytes(TOKEN_PROGRAM_ID), bytes(mint_pubkey)],
            ata_program,
        )[0]

        raw_amount = int(amount * (10 ** decimals))

        try:
            async with SolanaClient(SOLANA_RPC_URL) as client:
                instructions = []
                created_ata = False

                ata_info = await client.get_account_info(recipient_ata)
                if ata_info.value is None:
                    create_ata_ix = create_associated_token_account(
                        payer=sender_pubkey,
                        owner=recipient_pubkey,
                        mint=mint_pubkey,
                    )
                    instructions.append(create_ata_ix)
                    created_ata = True

                instructions.append(transfer_checked(TransferCheckedParams(
                    program_id=TOKEN_PROGRAM_ID,
                    source=sender_ata,
                    mint=mint_pubkey,
                    dest=recipient_ata,
                    owner=sender_pubkey,
                    amount=raw_amount,
                    decimals=decimals,
                )))

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

    class SolanaWalletProvider(WalletProvider):
        """Solana wallet provider implementation."""
        chain = "solana"

        def derive_address(self, private_key_hex: str) -> str:
            return _get_solana_wallet_address(private_key_hex)

        async def get_balance(self, address: str, mint: Optional[str] = None) -> dict:
            return await get_solana_balance({"solana": address}, mint)

        async def send(self, private_key_hex: str, wallets: dict, recipient: str,
                       amount: float, token: Optional[str] = None,
                       decimals: int = 9) -> dict:
            if token is None or token.upper() == "SOL":
                return await send_solana_sol(private_key_hex, wallets, recipient, amount)
            else:
                return await send_solana_token(
                    private_key_hex, wallets, recipient, token, amount, decimals
                )

    # Auto-register
    register_provider(SolanaWalletProvider())

else:
    # Stubs when Solana is not available
    def _derive_solana_keypair(private_key_hex: str):
        raise RuntimeError("Solana SDK not installed")

    def _get_solana_wallet_address(private_key_hex: str) -> str:
        raise RuntimeError("Solana SDK not installed")

    async def get_solana_balance(wallets: dict, mint: str = None) -> dict:
        return {"success": False, "error": "Solana wallet not available"}

    async def send_solana_sol(private_key_hex: str, wallets: dict,
                              recipient_wallet: str, amount: float) -> dict:
        return {"success": False, "error": "Solana wallet not available"}

    async def send_solana_token(private_key_hex: str, wallets: dict,
                                recipient_wallet: str, mint: str,
                                amount: float, decimals: int) -> dict:
        return {"success": False, "error": "Solana wallet not available"}
