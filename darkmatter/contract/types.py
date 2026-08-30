"""Contract data types — no I/O."""

from dataclasses import dataclass, field
from typing import Optional


REL_PENDING = "pending"
REL_ACTIVE = "active"
REL_CLOSED = "closed"


def _opt_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


@dataclass
class Relationship:
    """A local record that A and B agreed to talk (or are introducing)."""

    peer_id: str
    peer_locator: str
    advertised_locator: str = ""
    state: str = REL_PENDING
    trust: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    note: str = ""
    last_settlement: Optional[dict] = None
    negative_since: Optional[str] = None
    fetch_every: Optional[float] = None
    last_fetched_at: str = ""
    last_seen_at: str = ""
    peer_passport: Optional[dict] = None
    outbox_tip: str = ""

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id,
            "peer_locator": self.peer_locator,
            "advertised_locator": self.advertised_locator,
            "state": self.state,
            "trust": self.trust,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "note": self.note,
            "last_settlement": self.last_settlement,
            "negative_since": self.negative_since,
            "fetch_every": self.fetch_every,
            "last_fetched_at": self.last_fetched_at,
            "last_seen_at": self.last_seen_at,
            "peer_passport": self.peer_passport,
            "outbox_tip": self.outbox_tip,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Relationship":
        return cls(
            peer_id=data["peer_id"],
            # v3 prereleases used remote/origin; accept them during migration.
            peer_locator=data.get("peer_locator", data.get("remote", "")),
            advertised_locator=data.get("advertised_locator", data.get("origin", "")),
            state=data.get("state", REL_PENDING),
            trust=float(data.get("trust", 0.0)),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            note=data.get("note", ""),
            last_settlement=data.get("last_settlement"),
            negative_since=data.get("negative_since"),
            fetch_every=_opt_float(data.get("fetch_every")),
            last_fetched_at=data.get("last_fetched_at", ""),
            last_seen_at=data.get("last_seen_at", ""),
            peer_passport=data.get("peer_passport"),
            outbox_tip=data.get("outbox_tip", ""),
        )


@dataclass
class Envelope:
    """Public git object: clear metadata, sealed body, domain-separated signature."""

    id: str
    type: str
    from_id: str
    to_id: str
    timestamp: str
    ciphertext: dict
    signature: str
    expires_at: Optional[str] = None
    body: Optional[dict] = field(default=None, repr=False)

    def to_public_dict(self) -> dict:
        data = {
            "id": self.id,
            "type": self.type,
            "from": self.from_id,
            "to": self.to_id,
            "timestamp": self.timestamp,
            "ciphertext": self.ciphertext,
            "signature": self.signature,
        }
        if self.expires_at:
            data["expires_at"] = self.expires_at
        return data

    @classmethod
    def from_public_dict(cls, data: dict) -> "Envelope":
        return cls(
            id=data["id"],
            type=data["type"],
            from_id=data["from"],
            to_id=data["to"],
            timestamp=data["timestamp"],
            ciphertext=data["ciphertext"],
            signature=data["signature"],
            expires_at=data.get("expires_at"),
        )
