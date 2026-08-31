"""On-disk passport, profile, relationships, and local inbox. Never git."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from darkmatter.config import VISIBILITIES
from darkmatter.contract.types import REL_ACTIVE, REL_CLOSED, REL_PENDING, Relationship
from darkmatter.filelock import ProjectLock
from darkmatter.identity import derive_public_key_hex, generate_keypair
from darkmatter.names import generate_agent_name

PASSPORT_NAMES = ("passport", "passport.key")


def _is_actionable(item: dict) -> bool:
    item_type = item.get("type")
    return item_type in ("message", "forward", "referral") or (
        isinstance(item_type, str) and item_type.startswith("antimatter_")
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dm_dir(root: Path) -> Path:
    return root / ".darkmatter"


def atomic_write_text(path: Path, content: str, mode: Optional[int] = None) -> None:
    """Replace a file atomically so concurrent readers see old or new, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class LocalStore:
    """Project-local secret store: passport + relationships + inbox index."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.dir = _dm_dir(self.root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = ProjectLock(self.dir / "mailbox.lock")
        with self.locked():
            self._gitignore()
            self.private_key_hex, self.agent_id = self._load_or_create_passport()
            self.profile = self._load_or_create_profile()

    def locked(self):
        return self._lock.acquire()

    def _gitignore(self) -> None:
        gi = self.dir / ".gitignore"
        if not gi.exists():
            atomic_write_text(gi, "*\n")

    def passport_path(self) -> Path:
        for name in PASSPORT_NAMES:
            path = self.dir / name
            if path.exists():
                return path
        return self.dir / "passport"

    def _load_or_create_passport(self) -> tuple[str, str]:
        path = self.passport_path()
        if path.exists():
            priv = path.read_text().strip()
            os.chmod(path, 0o600)
            return priv, derive_public_key_hex(priv)
        priv, pub = generate_keypair()
        atomic_write_text(path, priv + "\n", mode=0o600)
        return priv, pub

    def profile_path(self) -> Path:
        return self.dir / "profile.json"

    def _load_or_create_profile(self) -> dict:
        path = self.profile_path()
        display = os.environ.get("DARKMATTER_DISPLAY_NAME", "").strip()
        bio = os.environ.get("DARKMATTER_BIO", "").strip() or "A DarkMatter agent."
        if path.exists():
            data = json.loads(path.read_text())
            if not data.get("passport_created_at"):
                passport = self.passport_path()
                try:
                    created = datetime.fromtimestamp(
                        passport.stat().st_mtime, timezone.utc,
                    ).isoformat()
                except OSError:
                    created = _now()
                data["passport_created_at"] = created
            if display:
                data["display_name"] = display
            if os.environ.get("DARKMATTER_BIO"):
                data["bio"] = bio
            atomic_write_text(path, json.dumps(data, indent=2) + "\n")
            return data
        data = {
            "agent_id": self.agent_id,
            "display_name": display or generate_agent_name(),
            "bio": bio,
            "passport_created_at": _now(),
        }
        atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        return data

    def save_profile(self, display_name: Optional[str] = None, bio: Optional[str] = None) -> dict:
        with self.locked():
            if display_name:
                self.profile["display_name"] = display_name
            if bio:
                self.profile["bio"] = bio
            atomic_write_text(self.profile_path(), json.dumps(self.profile, indent=2) + "\n")
            return dict(self.profile)

    @property
    def passport_created_at(self) -> str:
        return self.profile["passport_created_at"]

    def settings_path(self) -> Path:
        return self.dir / "settings.json"

    def load_settings(self) -> dict:
        data = {
            "visibility": "local",
            "origin": "",
            "lan_port": 8741,
            "lan_bind": "0.0.0.0",
            "antimatter_auto_route": True,
        }
        path = self.settings_path()
        if path.exists():
            data.update(json.loads(path.read_text()))
        env_overrides = {
            "visibility": os.environ.get("DARKMATTER_VISIBILITY"),
            "origin": os.environ.get("DARKMATTER_ORIGIN"),
            "lan_bind": os.environ.get("DARKMATTER_LAN_BIND"),
        }
        data.update({key: value for key, value in env_overrides.items() if value is not None})
        if data.get("visibility") not in VISIBILITIES:
            data["visibility"] = "local"
        data["lan_port"] = int(data.get("lan_port") or 8741)
        data["antimatter_auto_route"] = bool(data.get("antimatter_auto_route", True))
        return data

    def save_settings(self, **kwargs) -> dict:
        with self.locked():
            data = self.load_settings()
            data.update({k: v for k, v in kwargs.items() if v is not None})
            atomic_write_text(self.settings_path(), json.dumps(data, indent=2) + "\n")
            return data

    def relationships_path(self) -> Path:
        return self.dir / "relationships.json"

    def load_relationships(self) -> dict[str, Relationship]:
        path = self.relationships_path()
        if not path.exists():
            return {}
        raw = json.loads(path.read_text())
        return {k: Relationship.from_dict(v) for k, v in raw.items()}

    def save_relationships(self, rels: dict[str, Relationship]) -> None:
        with self.locked():
            payload = {k: v.to_dict() for k, v in rels.items()}
            atomic_write_text(self.relationships_path(), json.dumps(payload, indent=2) + "\n")

    def get_relationship(self, peer_id: str) -> Optional[Relationship]:
        return self.load_relationships().get(peer_id)

    def upsert_relationship(
        self,
        peer_id: str,
        peer_locator: str = "",
        state: Optional[str] = None,
        **kwargs,
    ) -> Relationship:
        with self.locked():
            rels = self.load_relationships()
            now = _now()
            rel = rels.get(peer_id)
            if rel is None:
                rel = Relationship(
                    peer_id=peer_id,
                    peer_locator=peer_locator,
                    state=state or REL_PENDING,
                    created_at=now,
                    updated_at=now,
                )
            else:
                if peer_locator:
                    rel.peer_locator = peer_locator
                if state:
                    rel.state = state
                rel.updated_at = now
            for key, value in kwargs.items():
                if hasattr(rel, key) and value is not None:
                    setattr(rel, key, value)
            rels[peer_id] = rel
            self.save_relationships(rels)
            return rel

    def adjust_trust(self, peer_id: str, delta: float) -> Relationship:
        """Bounded trust adjustment stored on the relationship."""
        with self.locked():
            rels = self.load_relationships()
            rel = rels.get(peer_id)
            if rel is None:
                raise KeyError(peer_id)
            current = rel.trust
            if delta >= 0:
                effective = delta * (1.0 - current)
            else:
                effective = delta * (1.0 + current)
            new_score = max(-1.0, min(1.0, current + effective))
            rel.trust = round(new_score, 4)
            if rel.trust < 0 and current >= 0:
                rel.negative_since = _now()
            elif rel.trust >= 0 and current < 0:
                rel.negative_since = None
            rel.updated_at = _now()
            rels[peer_id] = rel
            self.save_relationships(rels)
            return rel

    def record_settlement(
        self,
        peer_id: str,
        *,
        trust_delta: float = 0.0,
        tx_id: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> Relationship:
        """Economy hook — AntiMatter writes here, mail does not."""
        with self.locked():
            if trust_delta:
                self.adjust_trust(peer_id, trust_delta)
            rels = self.load_relationships()
            rel = rels[peer_id]
            rel.last_settlement = {
                "timestamp": _now(),
                "tx_id": tx_id,
                **(extra or {}),
            }
            rel.updated_at = _now()
            rels[peer_id] = rel
            self.save_relationships(rels)
            return rel

    def inbox_path(self) -> Path:
        return self.dir / "inbox.json"

    def load_inbox(self) -> list[dict]:
        path = self.inbox_path()
        if not path.exists():
            return []
        return json.loads(path.read_text())

    def save_inbox(self, items: list[dict]) -> None:
        with self.locked():
            atomic_write_text(self.inbox_path(), json.dumps(items, indent=2) + "\n")

    def append_inbox(self, item: dict) -> bool:
        """Append if id is new. Returns True when inserted."""
        with self.locked():
            items = self.load_inbox()
            if any(i.get("id") == item.get("id") for i in items):
                return False
            items.append(item)
            self.save_inbox(items)
            return True

    def consume_inbox(self, from_agents: Optional[list[str]] = None) -> list[dict]:
        """Consume unread messages and AntiMatter events."""
        with self.locked():
            items = self.load_inbox()
            matched, kept = [], []
            for item in items:
                if item.get("consumed"):
                    kept.append(item)
                    continue
                if from_agents and item.get("from") not in from_agents:
                    kept.append(item)
                    continue
                if not _is_actionable(item):
                    kept.append(item)
                    continue
                item = dict(item)
                item["consumed"] = True
                matched.append(item)
                kept.append(item)
            self.save_inbox(kept)
            return matched

    def unconsumed_messages(self, from_agents: Optional[list[str]] = None) -> list[dict]:
        """Return unread actionable correspondence, including AntiMatter events."""
        out = []
        for item in self.load_inbox():
            if item.get("consumed") or not _is_actionable(item):
                continue
            if from_agents and item.get("from") not in from_agents:
                continue
            out.append(item)
        return out


# Re-export state names for callers
__all__ = ["LocalStore", "REL_ACTIVE", "REL_CLOSED", "REL_PENDING", "atomic_write_text"]
