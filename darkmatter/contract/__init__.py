"""DarkMatter 3 social contract: passport identity, relationships, sealed envelopes."""

from darkmatter.contract.envelope import (
    ENVELOPE_TYPES,
    Envelope,
    is_expired_at,
    open_envelope,
    seal_envelope,
)
from darkmatter.contract.contact import create_contact_card, validate_locator, verify_contact_card
from darkmatter.contract.types import Relationship

__all__ = [
    "ENVELOPE_TYPES",
    "Envelope",
    "Relationship",
    "create_contact_card",
    "is_expired_at",
    "open_envelope",
    "seal_envelope",
    "validate_locator",
    "verify_contact_card",
]
