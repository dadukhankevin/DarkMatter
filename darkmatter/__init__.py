"""DarkMatter 3 — durable, sealed agent correspondence over Git."""

from darkmatter.antimatter import AntimatterLedger
from darkmatter.contributions import ContributionLedger
from darkmatter.collaboration import Collaboration
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
    create_liveness_claim,
    create_passport_claim,
    create_passport_succession,
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
    verify_liveness_claim,
    verify_passport_claim,
    verify_passport_succession,
)
from darkmatter.gitbox import Mailbox

__version__ = "3.6.0"

__all__ = [
    "Envelope",
    "AntimatterLedger",
    "ContributionLedger",
    "Mailbox",
    "Relationship",
    "MAX_CONTRIBUTION_HOPS",
    "__version__",
    "Collaboration",
    "append_contribution_hop",
    "create_contact_card",
    "create_contribution_ticket",
    "create_source_receipt",
    "create_forward_package",
    "create_message_record",
    "create_liveness_claim",
    "create_passport_claim",
    "create_passport_succession",
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
    "verify_liveness_claim",
    "verify_passport_claim",
    "verify_passport_succession",
]
