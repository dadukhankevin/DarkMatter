"""DarkMatter 3 — durable, sealed agent correspondence over Git."""

from darkmatter.antimatter import AntimatterLedger
from darkmatter.contract import (
    Envelope,
    Relationship,
    create_contact_card,
    open_envelope,
    seal_envelope,
    validate_locator,
    verify_contact_card,
)
from darkmatter.gitbox import Mailbox

__version__ = "3.2.0"

__all__ = [
    "Envelope",
    "AntimatterLedger",
    "Mailbox",
    "Relationship",
    "__version__",
    "create_contact_card",
    "open_envelope",
    "seal_envelope",
    "validate_locator",
    "verify_contact_card",
]
