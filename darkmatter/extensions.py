"""
Optional feature loading for DarkMatter addons.

Core DarkMatter should boot without wallet or chain dependencies. Crypto support
is enabled explicitly by installing the optional extra and setting
DARKMATTER_ENABLE_CRYPTO=true.
"""

import os

from darkmatter.logging import get_logger


_log = get_logger("extensions")

TRUE_VALUES = {"1", "true", "yes", "on"}
CRYPTO_DISABLED_ERROR = (
    "Crypto addon disabled. Install dmagent[crypto] and set "
    "DARKMATTER_ENABLE_CRYPTO=true to enable wallets and payments."
)

_crypto_loaded = False


def crypto_enabled() -> bool:
    """Return whether the crypto addon has been explicitly enabled."""
    return os.environ.get("DARKMATTER_ENABLE_CRYPTO", "false").strip().lower() in TRUE_VALUES


def load_crypto_extensions() -> bool:
    """Load bundled crypto providers if the addon is enabled and available."""
    global _crypto_loaded

    if not crypto_enabled():
        return False
    if _crypto_loaded:
        return True

    try:
        import darkmatter.wallet.solana  # noqa: F401 - registers SolanaWalletProvider
    except Exception as e:
        _log.warning("Crypto addon enabled but Solana provider failed to load: %s", e)
        return False

    _crypto_loaded = True
    return True


def crypto_wallets(state) -> dict:
    """Return wallets that should be advertised by the running node."""
    if not load_crypto_extensions():
        return {}
    return dict(getattr(state, "wallets", {}) or {})
