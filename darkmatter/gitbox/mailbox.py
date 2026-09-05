"""Pull-based mailbox: publish to outbox, fetch peers, ack into readbox."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from darkmatter.config import MAX_CONTENT_LENGTH, MAX_ENVELOPE_FILE_SIZE, VISIBILITIES
from darkmatter.contract.contact import create_contact_card, validate_locator, verify_contact_card
from darkmatter.antimatter import (
    ACCEPT as ANTIMATTER_ACCEPT,
    CONFIRM as ANTIMATTER_CONFIRM,
    DISPUTE as ANTIMATTER_DISPUTE,
    INVOICE as ANTIMATTER_INVOICE,
    OFFER as ANTIMATTER_OFFER,
    RECEIPT as ANTIMATTER_RECEIPT,
    AntimatterLedger,
    event_body as antimatter_event_body,
    new_settlement_id,
    normalize_terms,
    offer_body as antimatter_offer_body,
    summarize_event as summarize_antimatter_event,
)
from darkmatter.contract.envelope import (
    ACTIONABLE_ENVELOPE_TYPES,
    ANTIMATTER_CONTRIBUTION_ENVELOPE_TYPES,
    ANTIMATTER_SETTLEMENT_ENVELOPE_TYPES,
    is_expired,
    is_expired_at,
    open_envelope,
    seal_envelope,
)
from darkmatter.contract.contribution import (
    MAX_CONTRIBUTION_HOPS,
    append_contribution_hop,
    contribution_state,
    create_contribution_ticket,
    create_source_receipt,
    fulfill_contribution,
    resolve_contribution,
    verify_contribution_package,
)
from darkmatter.contract.forwarding import (
    create_forward_package,
    create_message_record,
    verify_forward_package,
    verify_message_record,
)
from darkmatter.contract.liveness import create_liveness_claim, verify_liveness_claim
from darkmatter.contract.types import REL_ACTIVE, REL_CLOSED, REL_PENDING, Envelope, Relationship
from darkmatter.contract.tenure import (
    create_passport_claim,
    parse_timestamp,
    verify_passport_claim,
)
from darkmatter.contract.obligation import create_proposal, create_acceptance, create_discussion
from darkmatter.commitment import read_commitment
from darkmatter.contributions import ContributionLedger
from darkmatter.gitbox.gitutil import (
    clone_or_update,
    commit_all,
    ensure_origin,
    git,
    init_repo,
    is_git_url,
    push_url,
    resolve_remote,
    rev_parse,
)
from darkmatter.gitbox.serve import GitHTTPServer
from darkmatter.nearby import LANNearbyResponder, LocalNearbyRegistry, discover_lan
from darkmatter.policy import load_policy
from darkmatter.store import LocalStore
from darkmatter.store.local import atomic_write_text

_MAILBOX: Optional["Mailbox"] = None


def get_mailbox(root: Optional[str | Path] = None) -> "Mailbox":
    global _MAILBOX
    if _MAILBOX is None:
        base = root or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd()
        _MAILBOX = Mailbox(base)
    return _MAILBOX


def reset_mailbox() -> None:
    global _MAILBOX
    if _MAILBOX is not None:
        _MAILBOX.shutdown()
    _MAILBOX = None


def _parse_ts(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _expires_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _locked(method):
    """Serialize a complete mailbox mutation across threads and processes."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.store.locked():
            return method(self, *args, **kwargs)
    return wrapper


class Mailbox:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.store = LocalStore(self.root)
        self.antimatter = AntimatterLedger(self.store)
        self.contributions = ContributionLedger(self.store)
        self.work = self.root / ".darkmatter" / "mailbox"
        self.bare = self.root / ".darkmatter" / "mailbox.git"
        self.peers_dir = self.root / ".darkmatter" / "peers"
        self._http: Optional[GitHTTPServer] = None
        self._nearby_registry = LocalNearbyRegistry(self.agent_id)
        self._nearby_responder: Optional[LANNearbyResponder] = None
        self._nearby_error = ""
        self._last_publish_errors: list[str] = []
        with self.store.locked():
            self._ensure_mailbox()
            self._apply_visibility()

    @property
    def agent_id(self) -> str:
        return self.store.agent_id

    @property
    def lan_url(self) -> str:
        return self._http.url if self._http else ""

    @property
    def remote(self) -> str:
        """Compatibility alias for the primary locator."""
        return self.locator

    @property
    def locator(self) -> str:
        return self.locators()["primary"]

    def contact_card(self, locator: Optional[str] = None) -> dict:
        profile = self.store.profile
        return create_contact_card(
            self.store.private_key_hex,
            self.agent_id,
            locator or self.locator,
            display_name=profile.get("display_name", ""),
            bio=profile.get("bio", ""),
            passport=self.passport_claim(),
        )

    def passport_claim(self) -> dict:
        return create_passport_claim(
            self.store.private_key_hex,
            self.agent_id,
            self.store.passport_created_at,
        )

    def locators(self) -> dict:
        s = self.store.load_settings()
        local = str(self.bare.resolve())
        lan = self.lan_url
        internet = s.get("origin") or ""
        vis = s["visibility"]
        if vis == "internet" and internet:
            primary = internet
        elif vis in ("lan", "internet") and lan:
            primary = lan
        else:
            primary = local
        return {"visibility": vis, "local": local, "lan": lan, "internet": internet, "primary": primary}

    def _hooks(self):
        return load_policy(self.root)

    def _interval(self, rel: Relationship) -> float:
        hooks = self._hooks()
        if hooks and hasattr(hooks, "fetch_interval"):
            try:
                return max(2.0, float(hooks.fetch_interval(rel)))
            except Exception:
                pass
        return max(2.0, float(rel.fetch_every or 30))

    def shutdown(self) -> None:
        self._stop_lan()
        self._nearby_registry.unregister()

    def _ensure_mailbox(self) -> None:
        if not (self.work / ".git").exists():
            init_repo(self.work)
        if not (self.bare / "HEAD").exists():
            init_repo(self.bare, bare=True)
        git(self.bare, "config", "http.uploadpack", "true")
        git(self.bare, "config", "http.receivepack", "false")
        atomic_write_text(self.bare / "git-daemon-export-ok", "")
        self._write_agent_json()
        (self.work / "outbox").mkdir(exist_ok=True)
        (self.work / "readbox").mkdir(exist_ok=True)
        commit_all(self.work, "init mailbox")
        ensure_origin(self.work, self.bare)

    def _write_agent_json(self) -> None:
        profile = self.store.profile
        data = {
            "agent_id": self.agent_id,
            "display_name": profile.get("display_name", ""),
            "bio": profile.get("bio", ""),
            "passport": self.passport_claim(),
            "contact_card": self.contact_card(),
            "capabilities": {
                "contact_card": 4,
                "envelope": 3,
                "antimatter_contribution": 1,
                "liveness": 1,
                "passport_succession": 1,
                "public_connection_knock": 1,
                "referral": 1,
            },
        }
        path = self.work / "agent.json"
        atomic_write_text(path, json.dumps(data, indent=2) + "\n")

    def _apply_visibility(self) -> None:
        self._stop_lan()
        s = self.store.load_settings()
        if s["visibility"] == "lan":
            self._http = GitHTTPServer(self.bare, s.get("lan_bind", "0.0.0.0"), int(s["lan_port"])).start()
            try:
                self._nearby_responder = LANNearbyResponder(
                    lambda: self.contact_card(self.lan_url),
                ).start()
                self._nearby_error = ""
            except OSError as exc:
                self._nearby_responder = None
                self._nearby_error = str(exc)
        self._refresh_nearby()

    def _refresh_nearby(self) -> None:
        """Publish a same-host card with a local locator; never creates a relationship."""
        self._nearby_registry.register(self.contact_card(str(self.bare.resolve())))

    def _stop_lan(self) -> None:
        if self._nearby_responder:
            self._nearby_responder.stop()
            self._nearby_responder = None
        if self._http:
            self._http.stop()
            self._http = None

    def nearby(self, timeout_seconds: float = 1.0) -> dict:
        """Return verified local/LAN cards without fetching or auto-connecting."""
        self._refresh_nearby()
        sightings = self._nearby_registry.discover()
        sightings.extend(discover_lan(self.agent_id, timeout_seconds))
        relationships = self.store.load_relationships()
        merged: dict[str, dict] = {}
        for sighting in sightings:
            card = sighting["card"]
            peer_id = card["agent_id"]
            current = merged.get(peer_id)
            if current is None:
                current = {
                    "agent_id": peer_id,
                    "display_name": card.get("display_name", ""),
                    "bio": card.get("bio", ""),
                    "locator": card["locator"],
                    "contact_card": card,
                    "scopes": [],
                    "relationship_state": None,
                }
                merged[peer_id] = current
            scope = sighting["scope"]
            if scope not in current["scopes"]:
                current["scopes"].append(scope)
            if scope == "lan":
                current["locator"] = card["locator"]
                current["contact_card"] = card
            rel = relationships.get(peer_id)
            if rel is not None:
                current["relationship_state"] = rel.state
        peers = sorted(
            merged.values(),
            key=lambda item: (item.get("display_name") or "", item["agent_id"]),
        )
        result = {"success": True, "count": len(peers), "nearby": peers}
        if self._nearby_error:
            result["lan_discovery_error"] = self._nearby_error
        return result

    @_locked
    def configure(
        self,
        visibility: Optional[str] = None,
        origin: Optional[str] = None,
        lan_port: Optional[int] = None,
        lan_bind: Optional[str] = None,
        peer_id: Optional[str] = None,
        fetch_every: Optional[float] = None,
        peer_locator: Optional[str] = None,
        note: Optional[str] = None,
        antimatter_auto_route: Optional[bool] = None,
    ) -> dict:
        if visibility is not None:
            if visibility not in VISIBILITIES:
                return {"success": False, "error": f"visibility must be {', '.join(VISIBILITIES)}"}
            next_origin = self.store.load_settings().get("origin") if origin is None else origin
            if visibility == "internet" and not next_origin:
                return {
                    "success": False,
                    "error": "internet visibility needs origin (a git URL you can push)",
                }
            if visibility == "internet":
                try:
                    validate_locator(next_origin)
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
        if origin:
            try:
                validate_locator(origin)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
        if peer_locator:
            try:
                peer_locator = validate_locator(peer_locator)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
        if (
            visibility is not None
            or origin is not None
            or lan_port is not None
            or lan_bind is not None
            or antimatter_auto_route is not None
        ):
            previous = self.store.load_settings()
            self.store.save_settings(
                visibility=visibility,
                origin=origin,
                lan_port=lan_port,
                lan_bind=lan_bind,
                antimatter_auto_route=antimatter_auto_route,
            )
            try:
                self._apply_visibility()
            except Exception as exc:
                self.store.save_settings(**previous)
                try:
                    self._apply_visibility()
                except Exception:
                    self._stop_lan()
                return {"success": False, "error": f"Could not apply visibility: {exc}"}
        rel = None
        if peer_id:
            rel = self.store.upsert_relationship(
                peer_id,
                peer_locator=peer_locator or "",
                fetch_every=fetch_every,
                note=note,
            )
        return {
            "success": True,
            "locators": self.locators(),
            "relationship": rel.to_dict() if rel else None,
            "antimatter_auto_route": self.store.load_settings()["antimatter_auto_route"],
        }

    def _publish_urls(self, extra: Optional[str] = None) -> list[str]:
        s = self.store.load_settings()
        urls = {str(self.bare.resolve())}
        if extra:
            urls.add(extra)
        if s["visibility"] == "internet" and s.get("origin"):
            urls.add(s["origin"])
        for rel in self.store.load_relationships().values():
            if (
                rel.advertised_locator
                and rel.advertised_locator != self.lan_url
                and rel.state != REL_CLOSED
            ):
                urls.add(rel.advertised_locator)
        return sorted(urls)

    def _publish(self, message: str) -> list[str]:
        errors: list[str] = []
        commit_all(self.work, message)
        ensure_origin(self.work, self.bare)
        local = self.bare.resolve()
        for url in self._publish_urls():
            if not is_git_url(url) and Path(url).resolve() == local:
                continue
            try:
                push_url(self.work, url)
            except Exception as e:
                errors.append(f"{url}: {e}")
        self._last_publish_errors = errors
        return errors

    def _with_publish(self, result: dict) -> dict:
        if self._last_publish_errors:
            result["publish_errors"] = list(self._last_publish_errors)
        return result

    def _write_outbox(self, env: Envelope) -> None:
        dest = self.work / "outbox" / f"{env.id}.json"
        atomic_write_text(dest, json.dumps(env.to_public_dict(), indent=2) + "\n")

    def _move_to_readbox(self, envelope_id: str, receipt_sender: str) -> bool:
        from darkmatter.contract.envelope import validate_envelope_id
        try:
            validate_envelope_id(envelope_id)
        except ValueError:
            return False
        src = self.work / "outbox" / f"{envelope_id}.json"
        if not src.is_file() or src.is_symlink():
            return False
        try:
            original = json.loads(src.read_text())
            if original.get("to") != receipt_sender or original.get("from") != self.agent_id:
                return False
        except (ValueError, OSError, AttributeError):
            return False
        dest = self.work / "readbox" / f"{envelope_id}.json"
        atomic_write_text(dest, src.read_text())
        src.unlink()
        return True

    def _read_outbox_files(self, repo: Path) -> list[dict]:
        outbox = repo / "outbox"
        if not outbox.is_dir():
            return []
        items = []
        for path in sorted(outbox.glob("*.json")):
            try:
                if path.stat().st_size > MAX_ENVELOPE_FILE_SIZE:
                    continue
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    items.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        return items

    def _read_peer_agent(self, repo: Path) -> Optional[dict]:
        path = repo / "agent.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    def _fetch_remote(self, remote: str, peer_id: Optional[str] = None) -> Path:
        ident = remote if is_git_url(remote) else resolve_remote(remote)
        key = hashlib.sha256((peer_id or ident).encode()).hexdigest()[:24]
        dest = self.peers_dir / key
        return clone_or_update(ident, dest)

    def peek_remote(self, remote: str, expected_peer_id: Optional[str] = None) -> dict:
        repo = self._fetch_remote(remote, expected_peer_id)
        agent = self._read_peer_agent(repo)
        if not agent or not agent.get("agent_id"):
            raise ValueError(f"No agent.json at remote {remote}")
        return agent

    def _seal_envelope(
        self,
        peer_id: str,
        env_type: str,
        body: dict,
        expires_at: Optional[str] = None,
        *,
        envelope_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Envelope:
        """Seal mail with a portable passport-signed liveness checkpoint."""
        timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        body = dict(body)
        body.setdefault(
            "_liveness",
            create_liveness_claim(
                self.store.private_key_hex,
                self.agent_id,
                timestamp,
            ),
        )
        return seal_envelope(
            self.store.private_key_hex,
            self.agent_id,
            peer_id,
            env_type,
            body,
            expires_at=expires_at,
            envelope_id=envelope_id,
            timestamp=timestamp,
        )

    @_locked
    def introduce(
        self,
        target_locator: str,
        advertised_locator: Optional[str] = None,
        expected_peer_id: Optional[str] = None,
    ) -> dict:
        try:
            target_locator = validate_locator(target_locator)
            if advertised_locator:
                advertised_locator = validate_locator(advertised_locator)
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        try:
            agent = self.peek_remote(target_locator, expected_peer_id)
        except Exception as exc:
            return {"success": False, "error": f"Could not fetch contact mailbox: {exc}"}
        peer_id = agent["agent_id"]
        if expected_peer_id and peer_id != expected_peer_id:
            return {"success": False, "error": "Contact card does not match the mailbox identity"}
        if peer_id == self.agent_id:
            return {"success": False, "error": "Cannot introduce yourself"}
        try:
            peer_passport = verify_passport_claim(agent.get("passport"), peer_id)
        except ValueError as exc:
            return {"success": False, "error": f"Mailbox has no valid passport tenure claim: {exc}"}
        advertised_locator = (advertised_locator or "").strip() or self.locator
        self.store.upsert_relationship(
            peer_id,
            peer_locator=target_locator,
            advertised_locator=advertised_locator,
            state=REL_PENDING,
            peer_passport=peer_passport,
        )
        env = self._seal_envelope(
            peer_id,
            "introduce",
            {
                "locator": advertised_locator,
                "display_name": self.store.profile.get("display_name", ""),
                "bio": self.store.profile.get("bio", ""),
                "passport": self.passport_claim(),
            },
        )
        self._write_outbox(env)
        self._publish(f"introduce {peer_id[:12]}")
        return self._with_publish({
            "success": True,
            "peer_id": peer_id,
            "display_name": agent.get("display_name", ""),
            "envelope_id": env.id,
            "advertised_locator": advertised_locator,
            "contact_card": self.contact_card(advertised_locator),
            "state": REL_PENDING,
        })

    def introduce_contact(self, card: dict, advertised_locator: Optional[str] = None) -> dict:
        try:
            verified = verify_contact_card(card)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return self.introduce(
            verified["locator"],
            advertised_locator=advertised_locator,
            expected_peer_id=verified["agent_id"],
        )

    @_locked
    def send(self, peer_id: str, content: str, expires_at: Optional[str] = None,
             env_type: str = "message", extra: Optional[dict] = None, *,
             envelope_id: Optional[str] = None, timestamp: Optional[str] = None) -> dict:
        if not isinstance(content, str) or not content:
            return {"success": False, "error": "Message content is required"}
        if len(content) > MAX_CONTENT_LENGTH:
            return {"success": False, "error": f"Message exceeds {MAX_CONTENT_LENGTH} characters"}
        if envelope_id is not None and (
            not isinstance(envelope_id, str)
            or len(envelope_id) != 32
            or any(char not in "0123456789abcdef" for char in envelope_id)
        ):
            return {"success": False, "error": "Explicit envelope_id must be 32 lowercase hex characters"}
        if envelope_id:
            for folder, delivered in (("readbox", True), ("outbox", False)):
                path = self.work / folder / f"{envelope_id}.json"
                if not path.exists():
                    continue
                try:
                    existing = Envelope.from_public_dict(json.loads(path.read_text()))
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    return {"success": False, "error": "Existing idempotent envelope is malformed"}
                if existing.to_id != peer_id or existing.type != env_type:
                    return {"success": False, "error": "Explicit envelope_id is already in use"}
                return {
                    "success": True,
                    "existing": True,
                    "delivered": delivered,
                    "envelope_id": envelope_id,
                    "to": peer_id,
                }
        body = {"content": content}
        if extra:
            body["metadata"] = dict(extra)
        if env_type != "message":
            return self._send_envelope(
                peer_id,
                env_type,
                body,
                expires_at,
                envelope_id=envelope_id,
                timestamp=timestamp,
            )
        envelope_id = envelope_id or uuid.uuid4().hex
        timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        try:
            body["provenance"] = create_message_record(
                self.store.private_key_hex,
                self.agent_id,
                peer_id,
                envelope_id,
                timestamp,
                content,
                metadata=body.get("metadata", {}),
                expires_at=expires_at,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        return self._send_envelope(
            peer_id,
            env_type,
            body,
            expires_at,
            envelope_id=envelope_id,
            timestamp=timestamp,
        )

    def _send_envelope(
        self,
        peer_id: str,
        env_type: str,
        body: dict,
        expires_at: Optional[str] = None,
        *,
        envelope_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> dict:
        """Seal, project, and publish a relationship-scoped envelope."""
        rel = self.store.get_relationship(peer_id)
        if rel is None or rel.state != REL_ACTIVE:
            return {"success": False, "error": "No active relationship"}
        try:
            env = self._seal_envelope(
                peer_id,
                env_type,
                body,
                expires_at=expires_at,
                envelope_id=envelope_id,
                timestamp=timestamp,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        projection = None
        if env_type in ANTIMATTER_SETTLEMENT_ENVELOPE_TYPES:
            projection = self.antimatter.apply_event(
                env_type,
                self.agent_id,
                peer_id,
                env.id,
                env.timestamp,
                body,
            )
            if not projection.get("success"):
                return projection
        if env_type == "antimatter_obligation":
            outcome = self.antimatter.apply_discussion(body.get("settlement_id"), self.agent_id, body.get("statement"))
            if not outcome.get("success"):
                return outcome
        self._write_outbox(env)
        self._publish(f"{env_type} {env.id[:12]}")
        result = {"success": True, "envelope_id": env.id, "to": peer_id}
        if projection:
            result["settlement"] = projection["settlement"]
            result["trust_delta"] = projection.get("trust_delta", 0.0)
        return self._with_publish(result)

    @_locked
    def refer_contact(
        self,
        peer_id: str,
        contact_card: dict,
        note: str = "",
    ) -> dict:
        """Explicitly introduce one peer's untouched signed card to another."""
        if not isinstance(note, str) or len(note) > 4000:
            return {"success": False, "error": "Referral note exceeds 4000 characters"}
        try:
            card = verify_contact_card(contact_card)
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        if card["agent_id"] == self.agent_id:
            return {"success": False, "error": "Use your own contact card directly"}
        if card["agent_id"] == peer_id:
            return {"success": False, "error": "Cannot refer a recipient to itself"}
        result = self._send_envelope(
            peer_id,
            "referral",
            {"contact_card": card, "note": note.strip()},
            _expires_in(30 * 24 * 60 * 60),
        )
        if result.get("success"):
            result.update({
                "referred_agent_id": card["agent_id"],
                "contact_card": card,
            })
        return result

    @_locked
    def forward(
        self,
        envelope_id: str,
        peer_id: str,
        note: str = "",
        max_hops: int = 3,
        ttl_seconds: float = 24 * 60 * 60,
    ) -> dict:
        """Explicitly forward one message while preserving signed provenance."""
        if not isinstance(note, str) or len(note) > 4000:
            return {"success": False, "error": "forward note exceeds 4000 characters"}
        if ttl_seconds < 60 or ttl_seconds > 30 * 24 * 60 * 60:
            return {"success": False, "error": "ttl_seconds must be between 60 and 2592000"}
        rel = self.store.get_relationship(peer_id)
        if rel is None or rel.state != REL_ACTIVE:
            return {"success": False, "error": "No active relationship"}
        item = next(
            (candidate for candidate in self.store.load_inbox() if candidate.get("id") == envelope_id),
            None,
        )
        if item is None:
            return {"success": False, "error": "Message is not present in the local inbox"}
        if item.get("type") == "message":
            original_envelope = item.get("envelope")
            message_record = (item.get("body") or {}).get("provenance")
            path = []
        elif item.get("type") == "forward":
            prior = (item.get("body") or {}).get("forward")
            try:
                verified = verify_forward_package(
                    prior,
                    envelope_from=item.get("from"),
                    envelope_to=self.agent_id,
                    envelope_expires_at=item.get("expires_at"),
                )
            except ValueError as exc:
                return {"success": False, "error": f"Invalid prior forward: {exc}"}
            original_envelope = verified["original_envelope"]
            message_record = verified["message"]
            path = verified["path"]
        else:
            return {"success": False, "error": "Only messages and prior forwards may be forwarded"}
        try:
            message_record = verify_message_record(message_record, original_envelope)
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "error": f"Message lacks transferable signed provenance: {exc}",
            }
        hooks = self._hooks()
        if hooks and hasattr(hooks, "should_forward"):
            try:
                allowed = bool(hooks.should_forward(dict(item), rel))
            except Exception:
                allowed = False
            if not allowed:
                return {"success": False, "error": "Local policy declined this forward"}

        expires_at = _expires_in(float(ttl_seconds))
        previous_expiry = None
        if path:
            previous_expiry = path[-1].get("expires_at")
        elif original_envelope:
            previous_expiry = original_envelope.get("expires_at")
        if previous_expiry:
            previous_ts = _parse_ts(previous_expiry)
            candidate_ts = _parse_ts(expires_at)
            if previous_ts is None or previous_ts <= time.time():
                return {"success": False, "error": "Message forwarding window has expired"}
            if candidate_ts is not None and previous_ts < candidate_ts:
                expires_at = previous_expiry
        try:
            package = create_forward_package(
                self.store.private_key_hex,
                self.agent_id,
                peer_id,
                original_envelope,
                message_record,
                path=path,
                note=note,
                max_hops=max_hops,
                expires_at=expires_at,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        original_content = package["message"]["content"]
        rendered = (
            (note.strip() + "\n\n") if note.strip() else ""
        ) + f"[Forwarded from {package['message']['from'][:12]}]\n{original_content}"
        result = self._send_envelope(
            peer_id,
            "forward",
            {"content": rendered, "forward": package},
            expires_at,
        )
        if result.get("success"):
            result.update({
                "original_envelope_id": package["message"]["envelope_id"],
                "hops_remaining": package["path"][-1]["hops_remaining"],
                "expires_at": expires_at,
            })
        return result

    def _write_contribution_audit(self, package: dict) -> None:
        """Publish the portable proof, never the local ledger or private keys."""
        verified = verify_contribution_package(package)
        contribution_id = verified["ticket"]["contribution_id"]
        path = self.work / "antimatter" / f"{contribution_id}.json"
        atomic_write_text(path, json.dumps(verified, indent=2, sort_keys=True) + "\n")

    def _eligible_older_relationships(self, package: dict) -> list[Relationship]:
        verified = verify_contribution_package(package, require_unexpired=True)
        ticket = verified["ticket"]
        current_claim = self.passport_claim()
        current_created = parse_timestamp(current_claim["created_at"], "current passport created_at")
        excluded = {
            ticket["source"]["payer_id"],
            ticket["source"]["payee_id"],
        }
        for hop in verified["path"]:
            excluded.add(hop["from"])
            excluded.add(hop["to"])
        now = datetime.now(timezone.utc).timestamp()
        candidates = []
        for rel in self.store.load_relationships().values():
            if (
                rel.state != REL_ACTIVE
                or rel.peer_id in excluded
                or not rel.peer_liveness
            ):
                continue
            try:
                claim = verify_passport_claim(rel.peer_passport, rel.peer_id)
                liveness = verify_liveness_claim(rel.peer_liveness, rel.peer_id)
                peer_created = parse_timestamp(claim["created_at"], "peer passport created_at")
                last_seen = _parse_ts(liveness["timestamp"])
                relationship_since = _parse_ts(rel.created_at)
            except (TypeError, ValueError):
                continue
            if (
                peer_created >= current_created
                or last_seen is None
                or relationship_since is None
                or now - last_seen > ticket["liveness_window_seconds"]
                or last_seen > now + 300
            ):
                continue
            candidates.append(rel)
        seed = ticket["contribution_id"] + ":" + self.agent_id + ":"
        return sorted(
            candidates,
            key=lambda rel: (
                _parse_ts(rel.created_at) or now,
                hashlib.sha256((seed + rel.peer_id).encode()).hexdigest(),
            ),
        )

    def _select_older_relationship(
        self,
        package: dict,
        target_agent_id: Optional[str] = None,
    ) -> Optional[Relationship]:
        candidates = self._eligible_older_relationships(package)
        if target_agent_id:
            selected = next((rel for rel in candidates if rel.peer_id == target_agent_id), None)
            if selected is None:
                raise ValueError(
                    "target_agent_id is not an older, recently observed active relationship",
                )
            return selected
        return candidates[0] if candidates else None

    def _append_contribution_to(self, package: dict, rel: Relationship) -> dict:
        liveness = verify_liveness_claim(rel.peer_liveness, rel.peer_id)
        return append_contribution_hop(
            self.store.private_key_hex,
            package,
            from_passport=self.passport_claim(),
            to_passport=verify_passport_claim(rel.peer_passport, rel.peer_id),
            observed_active_at=liveness["timestamp"],
            liveness=liveness,
            relationship_since=rel.created_at,
        )

    def _default_contribution_destination(self, package: dict) -> dict:
        """Offer a passport-bound rail destination when the adapter is available."""
        rail = package["ticket"]["contribution"]["rail"]
        if not rail.startswith("solana:"):
            return {}
        try:
            from darkmatter.wallet.payments import SolanaPaymentService

            network = rail.split(":", 1)[1]
            return {"rail": rail, "wallet_claim": SolanaPaymentService(
                self, network=network,
            ).claim()}
        except Exception:
            return {}

    def _store_contribution(self, package: dict) -> dict:
        record = self.contributions.put(package)
        self._write_contribution_audit(package)
        return record

    def _contribution_delivery_id(self, peer_id: str, env_type: str, package: dict) -> str:
        contribution_id = package["ticket"]["contribution_id"]
        material = ":".join((
            "darkmatter-contribution-delivery-v1",
            contribution_id,
            env_type,
            self.agent_id,
            peer_id,
        ))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @_locked
    def _send_contribution_package(self, peer_id: str, env_type: str, package: dict) -> dict:
        expires_at = package["ticket"]["expires_at"]
        envelope_id = self._contribution_delivery_id(peer_id, env_type, package)
        read_path = self.work / "readbox" / f"{envelope_id}.json"
        out_path = self.work / "outbox" / f"{envelope_id}.json"
        if read_path.exists():
            return {
                "success": True,
                "delivered": True,
                "existing": True,
                "envelope_id": envelope_id,
                "to": peer_id,
                "contribution_id": package["ticket"]["contribution_id"],
                "status": self.contributions.get(
                    package["ticket"]["contribution_id"],
                )["status"],
                "hop_count": len(package["path"]),
                "proof_package": package,
            }
        if out_path.exists():
            self._publish(f"retry {env_type} {package['ticket']['contribution_id'][:12]}")
            return self._with_publish({
                "success": True,
                "queued": True,
                "existing": True,
                "envelope_id": envelope_id,
                "to": peer_id,
                "contribution_id": package["ticket"]["contribution_id"],
                "status": self.contributions.get(
                    package["ticket"]["contribution_id"],
                )["status"],
                "hop_count": len(package["path"]),
                "proof_package": package,
            })
        result = self._send_envelope(
            peer_id,
            env_type,
            {"package": package},
            expires_at,
            envelope_id=envelope_id,
        )
        if result.get("success"):
            result.update({
                "contribution_id": package["ticket"]["contribution_id"],
                "status": self.contributions.get(
                    package["ticket"]["contribution_id"],
                )["status"],
                "hop_count": len(package["path"]),
                "proof_package": package,
            })
        return result

    def reconcile_contributions(self) -> dict:
        """Resume every locally actionable nonterminal route idempotently."""
        actions = []
        for record in self.contributions.list():
            package = record["package"]
            contribution_id = record["contribution_id"]
            status = record["status"]
            if status == "expired":
                continue
            path = package["path"]
            nodes = [package["ticket"]["origin_id"]] + [hop["to"] for hop in path]
            result = None
            action = None
            if package.get("fulfillment"):
                if self.agent_id in nodes and nodes.index(self.agent_id) < len(nodes) - 1:
                    action = "relay_fulfillment"
                    result = self.antimatter_relay_fulfillment(contribution_id)
            elif package.get("resolution"):
                if self.agent_id in nodes and nodes.index(self.agent_id) > 0:
                    action = "relay_resolution"
                    result = self.antimatter_relay_resolution(contribution_id)
                elif (
                    self.agent_id == package["ticket"]["origin_id"]
                    and package["resolution"].get("beneficiary")
                ):
                    actions.append({
                        "contribution_id": contribution_id,
                        "action": "awaiting_fulfillment",
                        "success": True,
                    })
            elif not path and self.agent_id == package["ticket"]["origin_id"]:
                action = "advance"
                result = self.antimatter_advance_contribution(contribution_id)
            elif path and self.agent_id == path[-1]["to"]:
                action = "advance"
                result = self.antimatter_advance_contribution(contribution_id)
            elif path and self.agent_id == path[-1]["from"]:
                action = "retry_route_delivery"
                result = self._send_contribution_package(
                    path[-1]["to"], "antimatter_contribution", package,
                )
            if result is not None:
                actions.append({
                    "contribution_id": contribution_id,
                    "action": action,
                    "success": bool(result.get("success")),
                    "error": result.get("error"),
                    "publish_errors": result.get("publish_errors", []),
                })
        return {
            "success": all(item.get("success") for item in actions),
            "count": len(actions),
            "actions": actions,
        }

    @_locked
    def retry_publication(self) -> dict:
        errors = self._publish("maintenance publication retry")
        return {"success": not errors, "publish_errors": errors}

    def _maintenance_state_path(self) -> Path:
        return self.store.dir / "maintenance.json"

    def maintain_once(self, presence_interval_seconds: float = 86400) -> dict:
        """Perform one transparent sync, recovery, publication, and presence pass."""
        presence_interval_seconds = max(60.0, float(presence_interval_seconds))
        sync = self.sync()
        from darkmatter.public import poll_public_invitations

        try:
            invitations = poll_public_invitations(self)
        except Exception as exc:  # polling is advisory; never block maintenance
            invitations = {"success": False, "error": str(exc), "count": 0, "invitations": []}
        recovery = self.reconcile_contributions()
        state_path = self._maintenance_state_path()
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            state = {}
        last_presence = _parse_ts(state.get("last_presence_at", ""))
        now = datetime.now(timezone.utc)
        presence = None
        if last_presence is None or now.timestamp() - last_presence >= presence_interval_seconds:
            presence = self.antimatter_presence()
            if presence.get("success") or presence.get("count") == 0:
                state["last_presence_at"] = now.isoformat()
                atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        publication = self.retry_publication()
        return {
            "success": (
                bool(sync.get("success"))
                and recovery.get("success")
                and publication.get("success")
            ),
            "sync": sync,
            "invitations": invitations,
            "warnings": (
                [f"Public invitation polling failed: {invitations.get('error')}"]
                if not invitations.get("success") else []
            ),
            "recovery": recovery,
            "presence": presence,
            "publication": publication,
            "unread": len(self.store.unconsumed_messages()),
        }

    @_locked
    def antimatter_contribute(
        self,
        settlement_id: str,
        *,
        target_agent_id: Optional[str] = None,
        max_hops: int = MAX_CONTRIBUTION_HOPS,
        ttl_seconds: int = 7 * 24 * 60 * 60,
        liveness_window_seconds: int = 7 * 24 * 60 * 60,
        receipt_id: Optional[str] = None,
    ) -> dict:
        """Turn a received settlement into a public 1% contribution route."""
        settlement = self.antimatter.get(settlement_id)
        if settlement is None:
            return {"success": False, "error": "Unknown settlement_id"}
        if settlement.get("payee_id") != self.agent_id:
            return {"success": False, "error": "Only the payee that received value may originate"}
        receipts = settlement.get("receipts") or []
        if not receipts:
            return {"success": False, "error": "A payment receipt is required before contribution routing"}
        if (settlement.get("contribution_agreement") or {}).get("mode", "participate") != "participate":
            return {"success": False, "error": "This settlement did not agree to a contribution"}
        confirmed_id = (settlement.get("confirmation") or {}).get("body", {}).get("receipt_id")
        if receipt_id and confirmed_id and receipt_id != confirmed_id:
            return {"success": False, "error": "Receipt differs from the confirmed primary receipt"}
        selected_id = receipt_id or confirmed_id or receipts[-1]["id"]
        receipt = next((r for r in receipts if r["id"] == selected_id), None)
        if receipt is None:
            return {"success": False, "error": "Unknown receipt_id"}
        existing = self.contributions.for_settlement(settlement_id, settlement=settlement)
        if existing:
            if existing["package"]["ticket"]["source"]["receipt_id"] != receipt["id"]:
                return {"success": False, "error": "Existing contribution refers to a different receipt"}
            return {"success": True, "existing": True, **existing, "proof_package": existing["package"]}
        terms = settlement["terms"]
        source = {
            "settlement_id": settlement_id,
            "payer_id": settlement["payer_id"],
            "payee_id": settlement["payee_id"],
            "receipt_id": receipt["id"],
            "transaction_id": receipt["body"]["tx_id"],
            "amount": terms["amount"],
            "currency": terms["currency"],
            "rail": terms["rail"],
            "receipt_attestation": receipt["body"].get("receipt_attestation"),
        }
        try:
            ticket = create_contribution_ticket(
                self.store.private_key_hex,
                self.agent_id,
                source,
                max_hops=max_hops,
                ttl_seconds=ttl_seconds,
                liveness_window_seconds=liveness_window_seconds,
            )
            package = {
                "version": 1,
                "ticket": ticket,
                "path": [],
                "resolution": None,
                "fulfillment": None,
            }
            rel = self._select_older_relationship(package, target_agent_id)
            if rel is None:
                package = resolve_contribution(
                    self.store.private_key_hex,
                    package,
                    passport=self.passport_claim(),
                    reason="no_older_live_relationship",
                )
                record = self._store_contribution(package)
                self._publish(f"antimatter unroutable {ticket['contribution_id'][:12]}")
                return {
                    "success": True,
                    **record,
                    "proof_package": package,
                    "explanation": "No older, recently observed active relationship was eligible.",
                }
            package = self._append_contribution_to(package, rel)
            self._store_contribution(package)
            return self._send_contribution_package(
                rel.peer_id, "antimatter_contribution", package,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    @_locked
    def antimatter_advance_contribution(
        self,
        contribution_id: str,
        *,
        target_agent_id: Optional[str] = None,
        resolve_here: bool = False,
        decline: bool = False,
        destination: Optional[dict] = None,
    ) -> dict:
        """Apply the default: route older until terminal, then resolve transparently."""
        record = self.contributions.get(contribution_id)
        if not record:
            return {"success": False, "error": "Unknown contribution_id"}
        package = record["package"]
        if package.get("resolution"):
            return {"success": True, "existing": True, **record, "proof_package": package}
        path = package["path"]
        expected = path[-1]["to"] if path else package["ticket"]["origin_id"]
        if expected != self.agent_id:
            return {"success": False, "error": "This agent is not the current route recipient"}
        try:
            at_limit = len(path) >= package["ticket"]["max_hops"]
            rel = None if (resolve_here or decline or at_limit) else self._select_older_relationship(
                package, target_agent_id,
            )
            if rel is not None:
                package = self._append_contribution_to(package, rel)
                self._store_contribution(package)
                return self._send_contribution_package(
                    rel.peer_id, "antimatter_contribution", package,
                )
            reason = (
                "declined" if decline
                else "max_hops" if at_limit
                else "voluntary_acceptance" if resolve_here
                else "no_older_live_relationship"
            )
            selected_destination = destination or self._default_contribution_destination(package)
            package = resolve_contribution(
                self.store.private_key_hex,
                package,
                passport=self.passport_claim(),
                reason=reason,
                destination=selected_destination,
            )
            self._store_contribution(package)
            if not path:
                self._publish(f"antimatter resolve {contribution_id[:12]}")
                return {
                    "success": True,
                    **self.contributions.get(contribution_id),
                    "proof_package": package,
                }
            previous = path[-1]["from"]
            return self._send_contribution_package(
                previous, "antimatter_resolution", package,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    @_locked
    def antimatter_relay_resolution(self, contribution_id: str) -> dict:
        record = self.contributions.get(contribution_id)
        if not record or not record["package"].get("resolution"):
            return {"success": False, "error": "Contribution has no resolution"}
        package = record["package"]
        nodes = [package["ticket"]["origin_id"]] + [hop["to"] for hop in package["path"]]
        if self.agent_id not in nodes:
            return {"success": False, "error": "This agent is not on the route"}
        index = nodes.index(self.agent_id)
        if index == 0:
            return {"success": True, "arrived": True, **record, "proof_package": package}
        return self._send_contribution_package(
            nodes[index - 1], "antimatter_resolution", package,
        )

    @_locked
    def antimatter_fulfill_contribution(
        self,
        contribution_id: str,
        transaction_id: str,
        proof: Optional[dict] = None,
    ) -> dict:
        record = self.contributions.get(contribution_id)
        if not record:
            return {"success": False, "error": "Unknown contribution_id"}
        try:
            package = fulfill_contribution(
                self.store.private_key_hex,
                record["package"],
                transaction_id,
                proof or {},
            )
            self._store_contribution(package)
            first = package["path"][0]["to"]
            return self._send_contribution_package(
                first, "antimatter_fulfillment", package,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    @_locked
    def antimatter_relay_fulfillment(self, contribution_id: str) -> dict:
        record = self.contributions.get(contribution_id)
        if not record or not record["package"].get("fulfillment"):
            return {"success": False, "error": "Contribution has no fulfillment"}
        package = record["package"]
        nodes = [package["ticket"]["origin_id"]] + [hop["to"] for hop in package["path"]]
        if self.agent_id not in nodes:
            return {"success": False, "error": "This agent is not on the route"}
        index = nodes.index(self.agent_id)
        if index == len(nodes) - 1:
            return {"success": True, "arrived": True, **record, "proof_package": package}
        return self._send_contribution_package(
            nodes[index + 1], "antimatter_fulfillment", package,
        )

    @_locked
    def antimatter_presence(self, peer_id: Optional[str] = None) -> dict:
        """Publish a signed liveness pulse to one or every active relationship."""
        relationships = self.store.load_relationships()
        targets = [peer_id] if peer_id else [
            rel.peer_id for rel in relationships.values() if rel.state == REL_ACTIVE
        ]
        results = []
        for target in targets:
            rel = relationships.get(target)
            if rel is None or rel.state != REL_ACTIVE:
                results.append({"success": False, "to": target, "error": "No active relationship"})
                continue
            env = self._seal_envelope(
                target,
                "presence",
                {"passport": self.passport_claim(), "purpose": "antimatter_liveness"},
                _expires_in(7 * 24 * 60 * 60),
            )
            self._write_outbox(env)
            results.append({"success": True, "to": target, "envelope_id": env.id})
        publish_errors = self._publish(f"presence {len(results)}") if results else []
        return {
            "success": bool(results) and all(item.get("success") for item in results),
            "count": len(results),
            "results": results,
            "publish_errors": publish_errors,
        }

    def get_contribution(self, contribution_id: str) -> Optional[dict]:
        return self.contributions.get(contribution_id)

    def list_contributions(self, status: Optional[str] = None) -> list[dict]:
        return self.contributions.list(status)

    def audit(self, peer_id: Optional[str] = None, include_proofs: bool = False) -> dict:
        """Report verifiable public AntiMatter facts without deriving a score."""
        audited_agent_id = self.agent_id
        repo = self.work
        if peer_id:
            rel = self.store.get_relationship(peer_id)
            if rel is None or not rel.peer_locator:
                return {"success": False, "error": "Unknown or unfetchable peer"}
            try:
                repo = self._fetch_remote(rel.peer_locator, peer_id)
                agent = self._read_peer_agent(repo)
            except Exception as exc:
                return {"success": False, "error": f"Could not fetch peer audit: {exc}"}
            if not agent or agent.get("agent_id") != peer_id:
                return {"success": False, "error": "Peer mailbox identity does not match"}
            audited_agent_id = peer_id

        records = []
        verified_contributions = []
        invalid = []
        audit_dir = repo / "antimatter"
        for path in sorted(audit_dir.glob("*.json")) if audit_dir.is_dir() else []:
            try:
                if path.stat().st_size > MAX_ENVELOPE_FILE_SIZE * 8:
                    raise ValueError("audit proof exceeds size limit")
                package = verify_contribution_package(json.loads(path.read_text()))
                ticket = package["ticket"]
                verified_contributions.append({"settlement_id": ticket["source"]["settlement_id"], "package": package})
                resolution = package.get("resolution")
                fulfillment = package.get("fulfillment")
                route = [ticket["origin_id"]] + [hop["to"] for hop in package["path"]]
                item = {
                    "contribution_id": ticket["contribution_id"],
                    "created_at": ticket["created_at"],
                    "status": contribution_state(package),
                    "origin_id": ticket["origin_id"],
                    "source_payer_id": ticket["source"]["payer_id"],
                    "source_payee_id": ticket["source"]["payee_id"],
                    "source_amount": ticket["source"]["amount"],
                    "contribution_amount": ticket["contribution"]["amount"],
                    "currency": ticket["contribution"]["currency"],
                    "rail": ticket["contribution"]["rail"],
                    "route": route,
                    "hop_count": len(package["path"]),
                    "resolution_reason": resolution.get("reason") if resolution else None,
                    "beneficiary_id": (
                        (resolution.get("beneficiary") or {}).get("agent_id")
                        if resolution else None
                    ),
                    "transaction_id": fulfillment.get("transaction_id") if fulfillment else None,
                }
                if include_proofs:
                    item["proof_package"] = package
                records.append(item)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                invalid.append({"file": path.name, "error": str(exc)})

        counts: dict[str, int] = {}
        for record in records:
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        facts = {
            "originated": sum(item["origin_id"] == audited_agent_id for item in records),
            "route_hops_signed": sum(
                audited_agent_id in item["route"][:-1] for item in records
            ),
            "beneficiary": sum(
                item["beneficiary_id"] == audited_agent_id for item in records
            ),
            "fulfilled_as_origin": sum(
                item["origin_id"] == audited_agent_id and bool(item["transaction_id"])
                for item in records
            ),
        }
        from darkmatter.commitment import accountability, read_commitment
        try:
            commitment = read_commitment(repo, audited_agent_id)
        except (ValueError, TypeError, OSError) as exc:
            invalid.append({"file": "commitment.json", "error": str(exc)})
            commitment = None
        from darkmatter.obligations import project_obligation
        retained = self.antimatter.list(peer_id=peer_id) if peer_id else self.antimatter.list()
        return {
            "success": True,
            "retained_obligations": [project_obligation(r, verified_contributions, include_proofs) for r in retained],
            "agent_id": audited_agent_id,
            "count": len(records),
            "counts": counts,
            "facts": facts,
            "accountability": accountability(commitment, records, audited_agent_id),
            "records": records,
            "invalid": invalid,
            "interpretation": "Raw signed evidence only; DarkMatter does not compute a trust score.",
        }

    @_locked
    def antimatter_offer(
        self,
        peer_id: str,
        description: str,
        amount,
        currency: str,
        rail: str,
        proposer_role: str = "payer",
        terms: Optional[dict] = None,
        metadata: Optional[dict] = None,
        valid_until: Optional[str] = None,
        settlement_id: Optional[str] = None,
        contribution_mode: Optional[str] = None,
    ) -> dict:
        """Offer exact settlement terms to an active relationship."""
        settlement_id = settlement_id or new_settlement_id()
        payer_id = self.agent_id if proposer_role == "payer" else peer_id
        payee_id = self.agent_id if proposer_role == "payee" else peer_id
        body = antimatter_offer_body(
            settlement_id,
            payer_id=payer_id,
            payee_id=payee_id,
            proposer_role=proposer_role,
            description=description,
            amount=amount,
            currency=currency,
            rail=rail,
            terms=terms,
            metadata=metadata,
            valid_until=valid_until,
        )
        envelope_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            body["terms"] = normalize_terms(body["terms"])
            commitment = read_commitment(self.work, self.agent_id) if payee_id == self.agent_id else None
            mode = contribution_mode or (commitment["mode"] if commitment else "participate")
            body["contribution_agreement"] = create_proposal(
                self.store.private_key_hex, settlement_id=settlement_id, offer_id=envelope_id,
                payer_id=payer_id, payee_id=payee_id, terms=body["terms"], mode=mode,
                commitment=commitment, timestamp=timestamp,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        return self._send_envelope(peer_id, ANTIMATTER_OFFER, body, valid_until,
                                   envelope_id=envelope_id, timestamp=timestamp)

    def _antimatter_record(self, peer_id: str, settlement_id: str) -> tuple[Optional[dict], Optional[dict]]:
        record = self.antimatter.get(settlement_id)
        if record is None:
            return None, {"success": False, "error": "Unknown settlement_id"}
        if record.get("peer_id") != peer_id:
            return None, {"success": False, "error": "settlement belongs to a different relationship"}
        return record, None

    @_locked
    def antimatter_accept(
        self,
        peer_id: str,
        settlement_id: str,
        note: str = "",
        metadata: Optional[dict] = None,
    ) -> dict:
        record, error = self._antimatter_record(peer_id, settlement_id)
        if error:
            return error
        if record["status"] in ("settled", "disputed"):
            return {"success": False, "error": f"settlement is {record['status']}"}
        body = antimatter_event_body(
            "accept",
            settlement_id,
            offer_id=record["offer"]["id"],
            note=note,
            metadata=metadata or {},
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        if record.get("contribution_agreement"):
            try:
                body["contribution_acceptance"] = create_acceptance(
                    self.store.private_key_hex, record["contribution_agreement"], timestamp,
                )
            except (TypeError, ValueError) as exc:
                return {"success": False, "error": str(exc)}
        return self._send_envelope(peer_id, ANTIMATTER_ACCEPT, body, timestamp=timestamp)

    @_locked
    def antimatter_invoice(
        self,
        peer_id: str,
        settlement_id: str,
        destination: Optional[dict] = None,
        memo: str = "",
        due_at: Optional[str] = None,
    ) -> dict:
        record, error = self._antimatter_record(peer_id, settlement_id)
        if error:
            return error
        acceptance = record.get("acceptance")
        if not acceptance:
            return {"success": False, "error": "settlement has not been accepted"}
        body = antimatter_event_body(
            "invoice",
            settlement_id,
            acceptance_id=acceptance["id"],
            destination=destination or {},
            memo=memo,
            due_at=due_at,
        )
        return self._send_envelope(peer_id, ANTIMATTER_INVOICE, body)

    @_locked
    def antimatter_receipt(
        self,
        peer_id: str,
        settlement_id: str,
        tx_id: str,
        proof: Optional[dict] = None,
        note: str = "",
    ) -> dict:
        record, error = self._antimatter_record(peer_id, settlement_id)
        if error:
            return error
        acceptance = record.get("acceptance")
        if not acceptance:
            return {"success": False, "error": "settlement has not been accepted"}
        invoice = record.get("invoice")
        envelope_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        terms = record["terms"]
        try:
            source_receipt = create_source_receipt(
                self.store.private_key_hex,
                payer_id=record["payer_id"],
                payee_id=record["payee_id"],
                settlement_id=settlement_id,
                receipt_id=envelope_id,
                timestamp=timestamp,
                transaction_id=tx_id,
                amount=terms["amount"],
                currency=terms["currency"],
                rail=terms["rail"],
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        body = antimatter_event_body(
            "receipt",
            settlement_id,
            acceptance_id=acceptance["id"],
            invoice_id=invoice["id"] if invoice else None,
            tx_id=tx_id,
            proof=proof or {},
            note=note,
            receipt_attestation=source_receipt,
        )
        return self._send_envelope(
            peer_id,
            ANTIMATTER_RECEIPT,
            body,
            envelope_id=envelope_id,
            timestamp=timestamp,
        )

    @_locked
    def antimatter_confirm(
        self,
        peer_id: str,
        settlement_id: str,
        receipt_id: Optional[str] = None,
        verification: Optional[dict] = None,
        note: str = "",
    ) -> dict:
        record, error = self._antimatter_record(peer_id, settlement_id)
        if error:
            return error
        receipts = record.get("receipts") or []
        if not receipts:
            return {"success": False, "error": "settlement has no receipt"}
        receipt_id = receipt_id or receipts[-1]["id"]
        body = antimatter_event_body(
            "confirm",
            settlement_id,
            receipt_id=receipt_id,
            verification=verification or {},
            note=note,
        )
        result = self._send_envelope(peer_id, ANTIMATTER_CONFIRM, body)
        if result.get("success") and (record.get("contribution_agreement") or {}).get("mode", "participate") == "participate":
            contribution = self.antimatter_contribute(settlement_id)
            result["contribution"] = contribution
            if not contribution.get("success"):
                result["contribution_warning"] = (
                    "Settlement was confirmed, but its AntiMatter ticket was not created: "
                    + contribution.get("error", "unknown error")
                )
        return result

    @_locked
    def antimatter_dispute(
        self,
        peer_id: str,
        settlement_id: str,
        reason: str,
        reference_id: Optional[str] = None,
        evidence: Optional[dict] = None,
    ) -> dict:
        record, error = self._antimatter_record(peer_id, settlement_id)
        if error:
            return error
        body = antimatter_event_body(
            "dispute",
            settlement_id,
            reason=reason,
            reference_id=reference_id,
            evidence=evidence or {},
        )
        return self._send_envelope(peer_id, ANTIMATTER_DISPUTE, body)

    @_locked
    def obligation_discuss(self, settlement_id, action, reason, reference=""):
        record = self.antimatter.get(settlement_id)
        if record is None:
            return {"success": False, "error": "Unknown settlement_id"}
        try:
            statement = create_discussion(
                self.store.private_key_hex,
                {"proposal": record.get("contribution_agreement"), "acceptance": record.get("contribution_acceptance")},
                event_id=uuid.uuid4().hex, action=action, reference=reference, reason=reason,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        result = self._send_envelope(record["peer_id"], "antimatter_obligation",
                                     {"settlement_id": settlement_id, "statement": statement})
        if result.get("success"):
            result["statement"] = statement
        return result

    def obligations(self, settlement_id=None, include_proofs=False, peer_id=None):
        from darkmatter.obligations import project_obligation
        records = self.antimatter.list(peer_id=peer_id)
        if settlement_id is not None:
            records = [r for r in records if r["settlement_id"] == settlement_id]
            if not records:
                return {"success": False, "error": "Unknown settlement_id"}
        contributions = self.contributions.list()
        return {"success": True, "obligations": [project_obligation(r, contributions, include_proofs) for r in records],
                "evidence_boundary": "Retained bilateral evidence, not a global score. Missing evidence is unknown; signatures do not verify payment.",
                "privacy": "Proof export includes private settlement and payment details. Sharing requires explicit authorization."}

    def list_settlements(
        self,
        peer_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        return self.antimatter.list(peer_id=peer_id, status=status)

    def get_settlement(self, settlement_id: str) -> Optional[dict]:
        return self.antimatter.get(settlement_id)

    def _receive_contact_card(
        self,
        contact_card: dict,
        envelope_id: Optional[str] = None,
    ) -> dict:
        try:
            card = verify_contact_card(contact_card)
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        peer_id = card["agent_id"]
        if peer_id == self.agent_id:
            return {"success": False, "error": "Cannot receive an introduction from yourself"}
        try:
            agent = self.peek_remote(card["locator"], peer_id)
        except Exception as exc:
            return {"success": False, "error": f"Could not fetch contact mailbox: {exc}"}
        if agent.get("agent_id") != peer_id:
            return {"success": False, "error": "Contact card does not match the mailbox identity"}
        repo = self._fetch_remote(card["locator"], peer_id)
        introductions = []
        for data in self._read_outbox_files(repo):
            if data.get("type") != "introduce" or data.get("from") != peer_id:
                continue
            if envelope_id and data.get("id") != envelope_id:
                continue
            kind = self._ingest(data, card["locator"])
            if kind == "introduce":
                introductions.append(data.get("id"))
        known_introductions = {
            item.get("id")
            for item in self.store.load_inbox()
            if item.get("type") == "introduce" and item.get("from") == peer_id
        }
        known_introductions.update(introductions)
        if envelope_id and envelope_id not in known_introductions:
            return {"success": False, "error": "The announced introduction was not found"}
        if not known_introductions:
            return {"success": False, "error": "No signed introduction from this contact is available"}
        existing = self.store.get_relationship(peer_id)
        state = REL_ACTIVE if existing and existing.state == REL_ACTIVE else REL_PENDING
        self.store.upsert_relationship(
            peer_id,
            peer_locator=card["locator"],
            state=state,
            peer_passport=card.get("passport"),
        )
        return {
            "success": True,
            "peer_id": peer_id,
            "state": state,
            "contact_card": card,
            "introduction_ids": sorted(known_introductions),
        }

    @_locked
    def receive_introduction(
        self,
        contact_card: dict,
        envelope_id: Optional[str] = None,
    ) -> dict:
        """Fetch and verify a public connection request without accepting it."""
        return self._receive_contact_card(contact_card, envelope_id)

    @_locked
    def accept(
        self,
        peer_id: Optional[str] = None,
        advertised_locator: Optional[str] = None,
        contact_card: Optional[dict] = None,
    ) -> dict:
        if contact_card is not None:
            received = self._receive_contact_card(contact_card)
            if not received.get("success"):
                return received
            if peer_id and peer_id != received["peer_id"]:
                return {"success": False, "error": "agent_id does not match the contact card"}
            peer_id = received["peer_id"]

        if not peer_id:
            return {"success": False, "error": "agent_id or contact_card is required"}
        rel = self.store.get_relationship(peer_id)
        if rel is None:
            return {"success": False, "error": "Unknown peer"}
        has_introduction = any(
            item.get("type") == "introduce" and item.get("from") == peer_id
            for item in self.store.load_inbox()
        )
        if rel.state != REL_ACTIVE and not has_introduction:
            return {
                "success": False,
                "error": "No signed introduction from this contact is available",
            }
        advertised_locator = (
            (advertised_locator or "").strip()
            or rel.advertised_locator
            or self.locator
        )
        self.store.upsert_relationship(
            peer_id, advertised_locator=advertised_locator, state=REL_ACTIVE,
        )
        env = self._seal_envelope(
            peer_id,
            "accept",
            {
                "locator": advertised_locator,
                "display_name": self.store.profile.get("display_name", ""),
                "passport": self.passport_claim(),
            },
        )
        self._write_outbox(env)
        self._publish(f"accept {peer_id[:12]}")
        return self._with_publish({
            "success": True,
            "peer_id": peer_id,
            "state": REL_ACTIVE,
            "contact_card": self.contact_card(advertised_locator),
        })

    @_locked
    def ignore(self, peer_id: str) -> dict:
        rel = self.store.get_relationship(peer_id)
        if rel is None:
            return {"success": False, "error": "Unknown peer"}
        self.store.upsert_relationship(peer_id, state=REL_CLOSED)
        env = self._seal_envelope(
            peer_id,
            "ignore",
            {"reason": "ignored"},
        )
        self._write_outbox(env)
        self._publish(f"ignore {peer_id[:12]}")
        return self._with_publish({"success": True, "peer_id": peer_id, "state": REL_CLOSED})

    def close(self, peer_id: str) -> dict:
        return self.ignore(peer_id)

    def _receipt(self, peer_id: str, envelope_id: str) -> None:
        env = self._seal_envelope(
            peer_id,
            "receipt",
            {"envelope_id": envelope_id},
            expires_at=_expires_in(30 * 24 * 60 * 60),
        )
        self._write_outbox(env)

    def _introduced_locally(self, peer_id: str) -> bool:
        """Acceptance must answer an introduction this mailbox actually sent."""
        for folder in ("outbox", "readbox"):
            for path in (self.work / folder).glob("*.json"):
                if path.is_symlink():
                    continue
                try:
                    data = json.loads(path.read_text())
                    if (isinstance(data, dict) and data.get("type") == "introduce"
                            and data.get("from") == self.agent_id and data.get("to") == peer_id):
                        return True
                except (ValueError, OSError):
                    continue
        return False

    def _ingest(self, data: dict, peer_remote: str) -> Optional[str]:
        if data.get("to") != self.agent_id:
            return None
        if data.get("id") and any(
            i.get("id") == data["id"] for i in self.store.load_inbox()
        ):
            return None
        try:
            env = open_envelope(data, self.store.private_key_hex)
        except ValueError:
            return None
        if is_expired(env):
            return None

        body = env.body or {}
        peer_liveness = None
        if body.get("_liveness") is not None:
            try:
                peer_liveness = verify_liveness_claim(body["_liveness"], env.from_id)
                if peer_liveness["timestamp"] != parse_timestamp(
                    env.timestamp, "envelope timestamp",
                ).isoformat():
                    return None
            except (TypeError, ValueError):
                return None
        if env.type == "receipt":
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state == REL_CLOSED:
                return None
            self.store.upsert_relationship(
                env.from_id,
                last_seen_at=env.timestamp,
                peer_liveness=peer_liveness,
            )
            if self._move_to_readbox(body.get("envelope_id", ""), env.from_id):
                return "receipt"
            return None

        antimatter_result = None
        if env.type in ANTIMATTER_SETTLEMENT_ENVELOPE_TYPES:
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state != REL_ACTIVE:
                return None
            antimatter_result = self.antimatter.apply_event(
                env.type,
                env.from_id,
                env.from_id,
                env.id,
                env.timestamp,
                body,
            )

        if env.type == "antimatter_obligation":
            rel = self.store.get_relationship(env.from_id)
            record = self.antimatter.get(body.get("settlement_id"))
            if rel is None or rel.state != REL_ACTIVE or not record or record["peer_id"] != env.from_id:
                return None
            outcome = self.antimatter.apply_discussion(body.get("settlement_id"), env.from_id, body.get("statement"))
            if not outcome.get("success"):
                return None

        if env.type == "accept":
            existing = self.store.get_relationship(env.from_id)
            if existing is None or existing.state == REL_CLOSED:
                return None
            if existing.state == REL_PENDING and not self._introduced_locally(env.from_id):
                return None
            peer_locator = body.get("locator") or body.get("remote") or peer_remote
            try:
                peer_locator = validate_locator(peer_locator)
                peer_passport = verify_passport_claim(body.get("passport"), env.from_id)
            except ValueError:
                return None
            self.store.upsert_relationship(
                env.from_id,
                peer_locator=peer_locator,
                state=REL_ACTIVE,
                peer_passport=peer_passport,
                last_seen_at=env.timestamp,
            )
        elif env.type == "ignore":
            if self.store.get_relationship(env.from_id) is None:
                return None
            self.store.upsert_relationship(env.from_id, state=REL_CLOSED)
        elif env.type == "introduce":
            peer_locator = body.get("locator") or body.get("remote") or peer_remote
            try:
                peer_locator = validate_locator(peer_locator)
                peer_passport = verify_passport_claim(body.get("passport"), env.from_id)
            except ValueError:
                return None
            existing = self.store.get_relationship(env.from_id)
            if existing is None or existing.state == REL_PENDING:
                self.store.upsert_relationship(
                    env.from_id,
                    peer_locator=peer_locator,
                    state=REL_PENDING,
                    peer_passport=peer_passport,
                    last_seen_at=env.timestamp,
                )
        elif env.type == "message":
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state != REL_ACTIVE:
                return None
        elif env.type == "forward":
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state != REL_ACTIVE:
                return None
            try:
                package = verify_forward_package(
                    body.get("forward"),
                    envelope_from=env.from_id,
                    envelope_to=env.to_id,
                    envelope_expires_at=env.expires_at,
                )
            except (TypeError, ValueError):
                return None
            body["forward"] = package
        elif env.type == "referral":
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state != REL_ACTIVE:
                return None
            try:
                card = verify_contact_card(body.get("contact_card"))
            except (TypeError, ValueError):
                return None
            if card["agent_id"] in (self.agent_id, env.from_id):
                return None
            note = body.get("note", "")
            if not isinstance(note, str) or len(note) > 4000:
                return None
            body["contact_card"] = card
        elif env.type == "presence":
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state != REL_ACTIVE:
                return None
            try:
                verify_passport_claim(body.get("passport"), env.from_id)
            except ValueError:
                return None
        elif env.type in ANTIMATTER_CONTRIBUTION_ENVELOPE_TYPES:
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state != REL_ACTIVE:
                return None
            try:
                package = verify_contribution_package(
                    body.get("package"),
                    require_unexpired=env.type == "antimatter_contribution",
                )
                path = package["path"]
                if env.type == "antimatter_contribution":
                    if not path or package["resolution"] or package["fulfillment"]:
                        raise ValueError("route package is not awaiting a recipient")
                    if path[-1]["from"] != env.from_id or path[-1]["to"] != env.to_id:
                        raise ValueError("route envelope does not match its final hop")
                elif env.type == "antimatter_resolution":
                    if not package["resolution"] or package["fulfillment"]:
                        raise ValueError("resolution envelope has the wrong package state")
                    nodes = [package["ticket"]["origin_id"]] + [hop["to"] for hop in path]
                    index = nodes.index(env.to_id)
                    if index + 1 >= len(nodes) or nodes[index + 1] != env.from_id:
                        raise ValueError("resolution did not follow the reverse route")
                else:
                    if not package["fulfillment"]:
                        raise ValueError("fulfillment envelope has no fulfillment proof")
                    nodes = [package["ticket"]["origin_id"]] + [hop["to"] for hop in path]
                    index = nodes.index(env.from_id)
                    if index + 1 >= len(nodes) or nodes[index + 1] != env.to_id:
                        raise ValueError("fulfillment did not follow the contribution route")
                body["package"] = package
                self.contributions.put(package)
                self._write_contribution_audit(package)
            except (TypeError, ValueError):
                return None
        elif env.type == "hint":
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state != REL_ACTIVE:
                return None
            about = body.get("agent_id") or ""
            if about and about != self.agent_id:
                existing = self.store.get_relationship(about)
                if existing is not None and existing.state != REL_CLOSED:
                    self.store.upsert_relationship(about, last_fetched_at="")

        content = body.get("content", "")
        forwardable = False
        if env.type == "message" and body.get("provenance"):
            try:
                body["provenance"] = verify_message_record(
                    body["provenance"], env.to_public_dict(),
                )
                forwardable = True
            except (TypeError, ValueError):
                forwardable = False
        elif env.type == "forward":
            package = body["forward"]
            hop = package["path"][-1]
            note = hop.get("note", "").strip()
            content = (
                (note + "\n\n") if note else ""
            ) + f"[Forwarded from {package['message']['from'][:12]}]\n{package['message']['content']}"
            forwardable = hop["hops_remaining"] > 0
        elif env.type == "referral":
            card = body["contact_card"]
            note = body.get("note", "").strip()
            content = (
                ((note + "\n\n") if note else "")
                + f"Contact referral: {card.get('display_name') or card['agent_id'][:12]} "
                + f"({card['agent_id']})\nSigned contact card:\n"
                + json.dumps(card, sort_keys=True)
            )
        if env.type == "antimatter_obligation":
            content = f"AntiMatter contribution {body['statement']['action']}: {body['statement']['reason']}"
        if env.type in ANTIMATTER_SETTLEMENT_ENVELOPE_TYPES:
            content = summarize_antimatter_event(env.type, body)
        elif env.type in ANTIMATTER_CONTRIBUTION_ENVELOPE_TYPES:
            package = body["package"]
            contribution_id = package["ticket"]["contribution_id"]
            content = f"AntiMatter {env.type.removeprefix('antimatter_')} {contribution_id}"
        item = {
            "id": env.id,
            "type": env.type,
            "from": env.from_id,
            "to": env.to_id,
            "timestamp": env.timestamp,
            "expires_at": env.expires_at,
            "content": content,
            "body": body,
            "envelope": env.to_public_dict(),
            "forwardable": forwardable,
            "consumed": env.type not in ACTIONABLE_ENVELOPE_TYPES,
        }
        if antimatter_result and not antimatter_result.get("success"):
            item["protocol_error"] = antimatter_result.get("error", "Invalid AntiMatter event")
        self.store.append_inbox(item)
        current = self.store.get_relationship(env.from_id)
        if current is not None and current.state != REL_CLOSED:
            self.store.upsert_relationship(
                env.from_id,
                last_seen_at=env.timestamp,
                peer_liveness=peer_liveness,
            )
        if env.type not in ("receipt", "hint", "presence"):
            self._receipt(env.from_id, env.id)
        return env.type

    def _due(self, rel: Relationship, now: float) -> bool:
        last = _parse_ts(rel.last_fetched_at)
        return last is None or (now - last) >= self._interval(rel)

    def next_fetch_wait(self) -> float:
        now = time.time()
        waits = []
        for rel in self.store.load_relationships().values():
            if not rel.peer_locator or rel.state == REL_CLOSED:
                continue
            last = _parse_ts(rel.last_fetched_at)
            if last is None:
                return 0.0
            waits.append(max(0.0, last + self._interval(rel) - now))
        return min(waits) if waits else 2.0

    def _new_message_recipients(self, repo: Path, old_tip: str, new_tip: str) -> set[str]:
        """Recipients of actionable envelopes added or changed since the last fetch."""
        if not old_tip or not new_tip or old_tip == new_tip:
            return set()
        changed = git(
            repo, "diff", "--name-only", f"{old_tip}..{new_tip}", "--", "outbox",
            check=False,
        )
        if changed.returncode != 0:
            return set()
        recipients: set[str] = set()
        for relative in changed.stdout.splitlines():
            if not relative.startswith("outbox/") or not relative.endswith(".json"):
                continue
            path = repo / relative
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("type") in ACTIONABLE_ENVELOPE_TYPES and data.get("to"):
                recipients.add(data["to"])
        return recipients

    def _emit_hints(self, about: Relationship, recipients: set[str]) -> int:
        if not about.outbox_tip or not recipients:
            return 0
        hooks = self._hooks()
        wrote = 0
        relationships = self.store.load_relationships()
        for peer_id in recipients:
            rel = relationships.get(peer_id)
            if rel is None or rel.state != REL_ACTIVE or peer_id == about.peer_id:
                continue
            allow = True
            if hooks and hasattr(hooks, "should_hint"):
                try:
                    allow = bool(hooks.should_hint(rel, about))
                except Exception:
                    allow = False
            if not allow:
                continue
            env = self._seal_envelope(
                peer_id,
                "hint",
                {
                    "agent_id": about.peer_id,
                    "locator": about.peer_locator,
                    "tip": about.outbox_tip,
                },
                expires_at=_expires_in(10 * 60),
            )
            self._write_outbox(env)
            wrote += 1
        return wrote

    def _process_contribution_events(self, ingested: list[dict]) -> list[dict]:
        """Follow the transparent default while remaining locally disableable."""
        if not self.store.load_settings().get("antimatter_auto_route", True):
            return []
        inbox = {item.get("id"): item for item in self.store.load_inbox()}
        processed = []
        seen = set()
        for event in ingested:
            env_type = event.get("type")
            if env_type not in ANTIMATTER_CONTRIBUTION_ENVELOPE_TYPES:
                continue
            item = inbox.get(event.get("id")) or {}
            package = (item.get("body") or {}).get("package") or {}
            contribution_id = (package.get("ticket") or {}).get("contribution_id")
            if not contribution_id or (env_type, contribution_id) in seen:
                continue
            seen.add((env_type, contribution_id))
            if env_type == "antimatter_contribution":
                result = self.antimatter_advance_contribution(contribution_id)
            elif env_type == "antimatter_resolution":
                result = self.antimatter_relay_resolution(contribution_id)
            else:
                result = self.antimatter_relay_fulfillment(contribution_id)
            processed.append({
                "type": env_type,
                "contribution_id": contribution_id,
                "success": bool(result.get("success")),
                "error": result.get("error"),
            })
        return processed

    @_locked
    def sync(self, only_due: bool = False) -> dict:
        self.expire()
        ingested = []
        errors = []
        hinted = 0
        now = time.time()
        stamp = datetime.now(timezone.utc).isoformat()
        rels = self.store.load_relationships()
        for peer_id, rel in rels.items():
            if not rel.peer_locator or rel.state == REL_CLOSED:
                continue
            if only_due and not self._due(rel, now):
                continue
            try:
                repo = self._fetch_remote(rel.peer_locator, peer_id)
                tip = rev_parse(repo)
            except Exception as e:
                errors.append({"peer_id": peer_id, "error": str(e)})
                continue
            changed = bool(tip and tip != rel.outbox_tip)
            hint_recipients = self._new_message_recipients(repo, rel.outbox_tip, tip)
            for data in self._read_outbox_files(repo):
                kind = self._ingest(data, rel.peer_locator)
                if kind:
                    ingested.append({"id": data.get("id"), "type": kind, "from": data.get("from")})
            updated = self.store.upsert_relationship(
                peer_id, last_fetched_at=stamp, outbox_tip=tip,
            )
            hooks = self._hooks()
            if hooks and hasattr(hooks, "on_fetched"):
                try:
                    hooks.on_fetched(updated, changed, tip)
                except Exception:
                    pass
            if changed:
                hinted += self._emit_hints(updated, hint_recipients)
        if ingested or hinted:
            self._publish("receipts")
        contribution_actions = self._process_contribution_events(ingested)
        return {
            "success": True,
            "ingested": ingested,
            "errors": errors,
            "hints": hinted,
            "antimatter_actions": contribution_actions,
            "inbox": self.store.unconsumed_messages(),
            "publish_errors": list(self._last_publish_errors),
        }

    @_locked
    def expire(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        removed = 0
        for folder in ("outbox", "readbox"):
            d = self.work / folder
            if not d.is_dir():
                continue
            for path in list(d.glob("*.json")):
                try:
                    env = Envelope.from_public_dict(json.loads(path.read_text()))
                except (json.JSONDecodeError, KeyError, OSError, TypeError):
                    continue
                if is_expired(env, now):
                    path.unlink()
                    removed += 1
        items = self.store.load_inbox()
        kept = []
        for item in items:
            exp = item.get("expires_at")
            if exp and is_expired_at(exp, now):
                removed += 1
                continue
            kept.append(item)
        if len(kept) != len(items):
            self.store.save_inbox(kept)
        if removed:
            self._publish("expire")
        return removed

    def list_relationships(self) -> list[dict]:
        return [r.to_dict() for r in self.store.load_relationships().values()]

    @_locked
    def update_profile(self, display_name: Optional[str] = None, bio: Optional[str] = None) -> dict:
        profile = self.store.save_profile(display_name=display_name, bio=bio)
        self._write_agent_json()
        self._publish("update profile")
        self._refresh_nearby()
        return profile
