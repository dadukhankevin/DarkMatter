"""Pull-based mailbox: publish to outbox, fetch peers, ack into readbox."""

from __future__ import annotations

import hashlib
import json
import os
import time
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
    offer_body as antimatter_offer_body,
    summarize_event as summarize_antimatter_event,
)
from darkmatter.contract.envelope import (
    ACTIONABLE_ENVELOPE_TYPES,
    ANTIMATTER_ENVELOPE_TYPES,
    is_expired,
    is_expired_at,
    open_envelope,
    seal_envelope,
)
from darkmatter.contract.types import REL_ACTIVE, REL_CLOSED, REL_PENDING, Envelope, Relationship
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
        self.work = self.root / ".darkmatter" / "mailbox"
        self.bare = self.root / ".darkmatter" / "mailbox.git"
        self.peers_dir = self.root / ".darkmatter" / "peers"
        self._http: Optional[GitHTTPServer] = None
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
        }
        path = self.work / "agent.json"
        atomic_write_text(path, json.dumps(data, indent=2) + "\n")

    def _apply_visibility(self) -> None:
        self._stop_lan()
        s = self.store.load_settings()
        if s["visibility"] == "lan":
            self._http = GitHTTPServer(self.bare, s.get("lan_bind", "0.0.0.0"), int(s["lan_port"])).start()

    def _stop_lan(self) -> None:
        if self._http:
            self._http.stop()
            self._http = None

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
        if visibility is not None or origin is not None or lan_port is not None or lan_bind is not None:
            previous = self.store.load_settings()
            self.store.save_settings(
                visibility=visibility,
                origin=origin,
                lan_port=lan_port,
                lan_bind=lan_bind,
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

    def _move_to_readbox(self, envelope_id: str) -> bool:
        src = self.work / "outbox" / f"{envelope_id}.json"
        if not src.exists():
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
        advertised_locator = (advertised_locator or "").strip() or self.locator
        self.store.upsert_relationship(
            peer_id,
            peer_locator=target_locator,
            advertised_locator=advertised_locator,
            state=REL_PENDING,
        )
        env = seal_envelope(
            self.store.private_key_hex,
            self.agent_id,
            peer_id,
            "introduce",
            {
                "locator": advertised_locator,
                "display_name": self.store.profile.get("display_name", ""),
                "bio": self.store.profile.get("bio", ""),
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
             env_type: str = "message", extra: Optional[dict] = None) -> dict:
        if not isinstance(content, str) or not content:
            return {"success": False, "error": "Message content is required"}
        if len(content) > MAX_CONTENT_LENGTH:
            return {"success": False, "error": f"Message exceeds {MAX_CONTENT_LENGTH} characters"}
        body = {"content": content}
        if extra:
            body["metadata"] = dict(extra)
        return self._send_envelope(peer_id, env_type, body, expires_at)

    def _send_envelope(
        self,
        peer_id: str,
        env_type: str,
        body: dict,
        expires_at: Optional[str] = None,
    ) -> dict:
        """Seal, project, and publish a relationship-scoped envelope."""
        rel = self.store.get_relationship(peer_id)
        if rel is None or rel.state != REL_ACTIVE:
            return {"success": False, "error": "No active relationship"}
        try:
            env = seal_envelope(
                self.store.private_key_hex,
                self.agent_id,
                peer_id,
                env_type,
                body,
                expires_at=expires_at,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        projection = None
        if env_type in ANTIMATTER_ENVELOPE_TYPES:
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
        self._write_outbox(env)
        self._publish(f"{env_type} {env.id[:12]}")
        result = {"success": True, "envelope_id": env.id, "to": peer_id}
        if projection:
            result["settlement"] = projection["settlement"]
            result["trust_delta"] = projection.get("trust_delta", 0.0)
        return self._with_publish(result)

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
        return self._send_envelope(peer_id, ANTIMATTER_OFFER, body, valid_until)

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
        body = antimatter_event_body(
            "accept",
            settlement_id,
            offer_id=record["offer"]["id"],
            note=note,
            metadata=metadata or {},
        )
        return self._send_envelope(peer_id, ANTIMATTER_ACCEPT, body)

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
        body = antimatter_event_body(
            "receipt",
            settlement_id,
            acceptance_id=acceptance["id"],
            invoice_id=invoice["id"] if invoice else None,
            tx_id=tx_id,
            proof=proof or {},
            note=note,
        )
        return self._send_envelope(peer_id, ANTIMATTER_RECEIPT, body)

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
        return self._send_envelope(peer_id, ANTIMATTER_CONFIRM, body)

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

    def list_settlements(
        self,
        peer_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        return self.antimatter.list(peer_id=peer_id, status=status)

    def get_settlement(self, settlement_id: str) -> Optional[dict]:
        return self.antimatter.get(settlement_id)

    @_locked
    def accept(
        self,
        peer_id: Optional[str] = None,
        advertised_locator: Optional[str] = None,
        contact_card: Optional[dict] = None,
    ) -> dict:
        if contact_card is not None:
            try:
                card = verify_contact_card(contact_card)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            if peer_id and peer_id != card["agent_id"]:
                return {"success": False, "error": "agent_id does not match the contact card"}
            peer_id = card["agent_id"]
            try:
                agent = self.peek_remote(card["locator"], peer_id)
            except Exception as exc:
                return {"success": False, "error": f"Could not fetch contact mailbox: {exc}"}
            if agent.get("agent_id") != peer_id:
                return {"success": False, "error": "Contact card does not match the mailbox identity"}
            self.store.upsert_relationship(
                peer_id, peer_locator=card["locator"], state=REL_PENDING,
            )
            repo = self._fetch_remote(card["locator"], peer_id)
            for data in self._read_outbox_files(repo):
                self._ingest(data, card["locator"])

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
        env = seal_envelope(
            self.store.private_key_hex,
            self.agent_id,
            peer_id,
            "accept",
            {
                "locator": advertised_locator,
                "display_name": self.store.profile.get("display_name", ""),
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
        env = seal_envelope(
            self.store.private_key_hex,
            self.agent_id,
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
        env = seal_envelope(
            self.store.private_key_hex,
            self.agent_id,
            peer_id,
            "receipt",
            {"envelope_id": envelope_id},
            expires_at=_expires_in(30 * 24 * 60 * 60),
        )
        self._write_outbox(env)

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
        if env.type == "receipt":
            if self._move_to_readbox(body.get("envelope_id", "")):
                return "receipt"
            return None

        antimatter_result = None
        if env.type in ANTIMATTER_ENVELOPE_TYPES:
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

        if env.type == "accept":
            peer_locator = body.get("locator") or body.get("remote") or peer_remote
            self.store.upsert_relationship(
                env.from_id, peer_locator=peer_locator, state=REL_ACTIVE,
            )
        elif env.type == "ignore":
            self.store.upsert_relationship(env.from_id, state=REL_CLOSED)
        elif env.type == "introduce":
            peer_locator = body.get("locator") or body.get("remote") or peer_remote
            existing = self.store.get_relationship(env.from_id)
            if existing is None or existing.state == REL_PENDING:
                self.store.upsert_relationship(
                    env.from_id, peer_locator=peer_locator, state=REL_PENDING,
                )
        elif env.type == "message":
            rel = self.store.get_relationship(env.from_id)
            if rel is None or rel.state != REL_ACTIVE:
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
        if env.type in ANTIMATTER_ENVELOPE_TYPES:
            content = summarize_antimatter_event(env.type, body)
        item = {
            "id": env.id,
            "type": env.type,
            "from": env.from_id,
            "to": env.to_id,
            "timestamp": env.timestamp,
            "expires_at": env.expires_at,
            "content": content,
            "body": body,
            "consumed": env.type not in ACTIONABLE_ENVELOPE_TYPES,
        }
        if antimatter_result and not antimatter_result.get("success"):
            item["protocol_error"] = antimatter_result.get("error", "Invalid AntiMatter event")
        self.store.append_inbox(item)
        if env.type not in ("receipt", "hint"):
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
            env = seal_envelope(
                self.store.private_key_hex,
                self.agent_id,
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
        return {
            "success": True,
            "ingested": ingested,
            "errors": errors,
            "hints": hinted,
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
        return profile
