"""
Abstract wallet provider interface and registry.

Depends on: (nothing — leaf module)
"""

from abc import ABC, abstractmethod
from typing import Optional


class WalletProvider(ABC):
    """Abstract wallet provider. Implement for each chain."""

    chain: str

    @abstractmethod
    async def get_balance(self, address: str, mint: Optional[str] = None) -> dict:
        ...

    async def get_all_balances(self, address: str, limit: int = 100) -> dict:
        """Get native balance plus all token holdings for this wallet.

        Returns {success, native: {token, balance}, tokens: [{mint, balance, symbol?, decimals}]}.
        Default implementation returns just the native balance. Override for token enumeration.
        """
        native = await self.get_balance(address)
        return {
            "success": native.get("success", False),
            "native": native if native.get("success") else {"error": native.get("error")},
            "tokens": [],
        }

    @abstractmethod
    async def send(self, private_key_hex: str, wallets: dict, recipient: str,
                   amount: float, token: Optional[str] = None,
                   decimals: int = 9) -> dict:
        ...

    @abstractmethod
    def derive_address(self, private_key_hex: str) -> str:
        ...

    @abstractmethod
    async def get_inbound_transfers(self, address: str, sender: Optional[str] = None,
                                     token: Optional[str] = None,
                                     after_signature: Optional[str] = None,
                                     limit: int = 20) -> list[dict]:
        """Get recent inbound transfers to an address.

        Returns list of dicts with keys: sender, amount, signature, timestamp.
        If sender is specified, only returns transfers from that address.
        If after_signature is specified, only returns transfers after that tx.
        """
        ...

    @abstractmethod
    async def get_wallet_age(self, address: str) -> Optional[str]:
        """Get the ISO timestamp of the wallet's earliest known transaction.

        Returns ISO datetime string, or None if the wallet has no history
        or the lookup fails.
        """
        ...

    @abstractmethod
    async def attest_identity(self, private_key_hex: str, wallets: dict,
                               agent_id: str) -> dict:
        """Create an on-chain transaction embedding the agent's passport public key.

        Should be called once when the wallet is first used. The attestation
        ties this wallet address to a specific DarkMatter agent_id immutably.
        Uses chain-specific mechanisms (Solana memo, EVM calldata, etc.).

        Returns {success, tx_signature} or {success: False, error}.
        """
        ...

    @abstractmethod
    async def verify_identity_attestation(self, address: str,
                                           agent_id: str) -> dict:
        """Verify that a wallet's history contains an identity attestation
        matching the given agent_id.

        Returns a dict with:
          - status: "match" (attestation found, agent_id matches)
                    "mismatch" (attestation found, different agent_id)
                    "none" (no attestation found on this wallet)
          - timestamp: ISO datetime of attestation tx (only if status != "none")
          - attested_agent_id: the agent_id in the attestation (only if status == "mismatch")
        """
        ...


# Registry
_providers: dict[str, WalletProvider] = {}


def register_provider(provider: WalletProvider) -> None:
    """Register a wallet provider for a chain."""
    _providers[provider.chain] = provider


def get_provider(chain: str) -> Optional[WalletProvider]:
    """Get the wallet provider for a chain, or None."""
    return _providers.get(chain)


def get_all_providers() -> dict[str, WalletProvider]:
    """Get all registered wallet providers."""
    return dict(_providers)


def resolve_provider(wallets: dict) -> Optional[tuple[str, 'WalletProvider', str]]:
    """Find the first available provider for a peer's wallets.

    Returns (chain, provider, address) or None.
    """
    for chain, address in wallets.items():
        provider = _providers.get(chain)
        if provider and address:
            return (chain, provider, address)
    return None
