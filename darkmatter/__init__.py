"""DarkMatter 3 — durable, sealed agent correspondence over Git."""

from darkmatter.antimatter import AntimatterLedger
from darkmatter.contributions import ContributionLedger
from darkmatter.contract import (
    Envelope,
    Relationship,
    MAX_CONTRIBUTION_HOPS,
    append_contribution_hop,
    create_contact_card,
    create_contribution_ticket,
    create_source_receipt,
    create_forward_package,
    create_message_record,
    create_passport_claim,
    fulfill_contribution,
    open_envelope,
    resolve_contribution,
    seal_envelope,
    validate_locator,
    verify_contact_card,
    verify_contribution_package,
    verify_source_receipt,
    verify_forward_package,
    verify_message_record,
    verify_passport_claim,
)
from darkmatter.gitbox import Mailbox

__version__ = "3.4.0"

__all__ = [
    "Envelope",
    "AntimatterLedger",
    "ContributionLedger",
    "Mailbox",
    "Relationship",
    "MAX_CONTRIBUTION_HOPS",
    "__version__",
    "append_contribution_hop",
    "create_contact_card",
    "create_contribution_ticket",
    "create_source_receipt",
    "create_forward_package",
    "create_message_record",
    "create_passport_claim",
    "fulfill_contribution",
    "open_envelope",
    "resolve_contribution",
    "seal_envelope",
    "validate_locator",
    "verify_contact_card",
    "verify_contribution_package",
    "verify_source_receipt",
    "verify_forward_package",
    "verify_message_record",
    "verify_passport_claim",
]
