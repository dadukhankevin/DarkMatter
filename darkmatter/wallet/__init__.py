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

    @abstractmethod
    async def send(self, private_key_hex: str, wallets: dict, recipient: str,
                   amount: float, token: Optional[str] = None,
                   decimals: int = 9) -> dict:
        ...

    @abstractmethod
    def derive_address(self, private_key_hex: str) -> str:
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
