"""Named Solana assets supported by the original DarkMatter wallet."""

from __future__ import annotations

from dataclasses import asdict, dataclass


LAMPORTS_PER_SOL = 1_000_000_000
NATIVE_SOL = "SOL"

# Historical DarkMatter token shortcuts. These are mainnet mint addresses from
# the original wallet; keeping them here makes the restored behavior explicit.
MAINNET_DM_MINT = "5DxioZwEeAKpBaYC5veTHArKE55qRDSmb5RZ6VwApump"
MAINNET_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MAINNET_USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
DEVNET_USDC_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"

NETWORK_ALIASES = {
    "devnet": "devnet",
    "mainnet": "mainnet-beta",
    "mainnet-beta": "mainnet-beta",
}


@dataclass(frozen=True)
class TokenInfo:
    symbol: str
    network: str
    decimals: int
    mint: str | None = None
    token_program: str | None = None
    source: str = "catalog"

    @property
    def native(self) -> bool:
        return self.mint is None

    def to_dict(self) -> dict:
        return {**asdict(self), "native": self.native}


_CATALOG = {
    "devnet": {
        "SOL": TokenInfo("SOL", "devnet", 9),
        "USDC": TokenInfo("USDC", "devnet", 6, DEVNET_USDC_MINT),
    },
    "mainnet-beta": {
        "SOL": TokenInfo("SOL", "mainnet-beta", 9),
        # DM was originally issued through the Token-2022 program.
        "DM": TokenInfo(
            "DM",
            "mainnet-beta",
            6,
            MAINNET_DM_MINT,
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        ),
        "USDC": TokenInfo("USDC", "mainnet-beta", 6, MAINNET_USDC_MINT),
        "USDT": TokenInfo("USDT", "mainnet-beta", 6, MAINNET_USDT_MINT),
    },
}


def normalize_network(network: str | None) -> str:
    value = (network or "devnet").strip().lower()
    try:
        return NETWORK_ALIASES[value]
    except KeyError as exc:
        raise ValueError("Solana network must be devnet or mainnet-beta") from exc


def list_tokens(network: str | None = None) -> list[dict]:
    selected = normalize_network(network)
    return [token.to_dict() for token in _CATALOG[selected].values()]


def resolve_token(asset: str, network: str | None = None) -> TokenInfo:
    """Resolve a named token or retain an arbitrary mint for RPC discovery."""
    selected = normalize_network(network)
    value = (asset or "").strip()
    if not value:
        raise ValueError("asset is required")
    named = _CATALOG[selected].get(value.upper())
    if named:
        return named
    # A mint's decimals and owning token program are discovered from the chain.
    return TokenInfo(value, selected, -1, value, source="mint")


__all__ = [
    "DEVNET_USDC_MINT",
    "LAMPORTS_PER_SOL",
    "MAINNET_DM_MINT",
    "MAINNET_USDC_MINT",
    "MAINNET_USDT_MINT",
    "NATIVE_SOL",
    "TokenInfo",
    "list_tokens",
    "normalize_network",
    "resolve_token",
]
