"""Opt-in, encrypted session mail on isolated branches of a shared Git repo.

Remote data never configures local execution. Membership is owner-selected:
repository-writer discovery or pinned keys. Sessions are registered locally.
No peer files are checked out, merged, or executed.

Network work (push, ls-remote, fetch) runs under a separate transport lock so
hooks that only touch local state are never blocked behind a slow remote.
Lock order is always transport lock, then state lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from darkmatter.collaboration import BOUNDARY, _private_directory, _text
from darkmatter.contract.envelope import is_expired, open_envelope, seal_envelope
from darkmatter.facts import host_name, valid_facts
from darkmatter.filelock import ProjectLock
from darkmatter.gitbox.gitutil import GitError, git, init_repo, resolve_remote
from darkmatter.identity import generate_keypair
from darkmatter.security import sign_payload, verify_signed_payload
from darkmatter.store.local import atomic_write_text

DOMAIN = "darkmatter.repo-space.v1"
PREFIX = "darkmatter/mail/v1/"
MAX_ITEMS = 128
MAX_PEERS = 32
MAX_BLOB = 8 * 1024 * 1024
TTL = 7 * 86400
MEMBERSHIP_POLICIES = ("repo-writers", "pinned")
MAX_BLOCKED = 4096
CONNECT_PROOF_TTL = 300
# Unchanged presence is republished at most this often; mail and session changes
# publish immediately. Presence stays valid for TTL, so this is ample margin.
PRESENCE_REFRESH = 6 * 3600
# Changed-file lists alone never force a push; they ride along with the next
# publication, or refresh presence after this long while they keep changing.
FACTS_REFRESH = 1800
MAX_OBJECTIVE = 512
MAX_WORKFLOWS = 100
MAX_WORKFLOW_BYTES = 256 * 1024
# GitHub honors [skip ci] for push events but not for branch create/delete.
CI_BRANCH_EVENTS = ("create", "delete")
# Bookkeeping written only by publication; merged back after lock-free network work.
_PUBLISH_KEYS = ("published_envelope_hashes", "connect_proof", "published_preview", "published_facts",
                 "published_time", "published_head", "ci_reviewed", "ci_tree",
                 "ci_head", "ci_observed_tree", "ci_observed_risky")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a lowercase 32-byte public key")
    return value


def _session(value):
    return _text(value, "session", 256)


def _workflow_triggers(text):
    """Best-effort top-level `on:` event names; None when no trigger block is found."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"""^(?:on|"on"|'on'|true)\s*:\s*(.*?)\s*(?:#.*)?$""", line)
        if not match:
            continue
        if match.group(1):
            return set(re.findall(r"[A-Za-z_][\w-]*", match.group(1)))
        events, indent = set(), None
        for sub in lines[index + 1:]:
            if not sub.strip() or sub.lstrip().startswith("#"):
                continue
            width = len(sub) - len(sub.lstrip())
            if width == 0:
                break
            indent = width if indent is None else indent
            name = re.match(r"(?:-\s*)?([A-Za-z_][\w-]*)", sub.strip())
            if width == indent and name:
                events.add(name.group(1))
        return events
    return None


def workflow_needs_review(text):
    """Conservative: unparseable triggers or branch create/delete events need a human."""
    triggers = _workflow_triggers(text)
    return triggers is None or bool(triggers & set(CI_BRANCH_EVENTS))


def project_name(remote: str) -> str:
    """'git@github.com:owner/DarkMatter.git' -> 'DarkMatter'."""
    tail = re.split(r"[/:]", remote.rstrip("/"))[-1]
    return re.sub(r"\.git$", "", tail)[:128] or "repo"


def default_space_directory(root=None):
    from darkmatter.collaboration import repository_root
    configured = os.environ.get("DARKMATTER_SPACE_DIR")
    if configured:
        return Path(configured).expanduser()
    root = repository_root(root or os.environ.get("DARKMATTER_PROJECT_DIR") or Path.cwd())
    digest = hashlib.sha256(str(root).encode()).hexdigest()
    return Path.home() / ".darkmatter" / "spaces" / digest


class RepoSpace:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().absolute()
        _private_directory(self.directory)
        for name in ("state.json", "space.lock", "transport"):
            if (self.directory / name).is_symlink():
                raise ValueError("Repo-space paths must not be symlinks")
        self.path = self.directory / "state.json"
        self.transport = self.directory / "transport"
        self.lock = ProjectLock(self.directory / "space.lock")
        self.net_lock = ProjectLock(self.directory / "transport.lock")

    def _load(self):
        if not self.path.exists():
            raise ValueError("Initialize this repo space first")
        return json.loads(self.path.read_text())

    def _save(self, state):
        atomic_write_text(self.path, _json(state) + "\n", mode=0o600)

    def initialize(self, remote: str, space: str | None = None, *, membership="repo-writers",
                   ci_review="manual"):
        """Create a device identity. ci_review="auto" lets publication scan workflows itself."""
        if membership not in MEMBERSHIP_POLICIES:
            raise ValueError("Unknown membership policy")
        if ci_review not in ("auto", "manual"):
            raise ValueError("ci_review must be auto or manual")
        remote = resolve_remote(remote)
        space = space or ("shared" if membership == "repo-writers" else uuid.uuid4().hex)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", space):
            raise ValueError("Space id must be 1-64 plain identifier characters")
        with self.lock.acquire():
            if self.path.exists():
                raise ValueError("Already initialized; existing identity preserved")
            if self.transport.exists():
                raise ValueError("Transport already exists without state; inspect it before initializing")
            private, device = generate_keypair()
            init_repo(self.transport)
            git(self.transport, "config", "commit.gpgsign", "false")
            git(self.transport, "config", "core.attributesFile", os.devnull)
            self._save({"version": 1, "space": space, "remote": remote,
                        "private": private, "device": device, "peers": [],
                        "membership": membership, "auto_peers": [], "blocked_devices": [],
                        "sessions": {}, "outbox": {}, "inbox": {}, "peer_sessions": {},
                        "ci_reviewed": False, "ci_review": ci_review, "wake": {}, "wake_attempts": {},
                        "wake_attempted": {}, "wake_history": {}})
        return {"space": space, "device": device, "remote": remote, "membership": membership,
                "ci_review": ci_review}

    @staticmethod
    def _clear_auto_peers(state):
        for device in state.get("auto_peers", []):
            state["peers"] = [p for p in state["peers"] if p != device]
            state["peer_sessions"].pop(device, None)
        state["auto_peers"] = []

    def set_membership(self, policy):
        """Local owner choice; never enabled by an advertisement or MCP argument."""
        if policy not in MEMBERSHIP_POLICIES:
            raise ValueError("Unknown membership policy")
        with self.lock.acquire():
            state = self._load()
            self._clear_auto_peers(state)
            state["membership"] = policy
            state.pop("connect_proof", None)
            self._save(state)
        return {"membership": policy}

    def enroll(self, device: str, *, remove=False):
        _key(device)
        with self.lock.acquire():
            state = self._load()
            blocked = state.setdefault("blocked_devices", [])
            if remove:
                if device not in blocked:
                    if len(blocked) >= MAX_BLOCKED:
                        raise ValueError("Revoked device limit reached")
                    blocked.append(device)
                state["peers"] = [p for p in state["peers"] if p != device]
                state["peer_sessions"].pop(device, None)
            elif device not in state["peers"]:
                if len(state["peers"]) >= MAX_PEERS:
                    raise ValueError("Device enrollment limit reached")
                state["peers"].append(device)
            if not remove and device in blocked:
                blocked.remove(device)
            # Explicit enrollment pins the key; revocation must survive rediscovery.
            state["auto_peers"] = [p for p in state.get("auto_peers", []) if p != device]
            self._save(state)
        return {"device": device, "enrolled": not remove}

    def register(self, session: str, client: str | None, *, agent: str | None = None,
                 paused: bool | None = None, availability: str | None = None,
                 objective: str | None = None, facts: dict | None = None):
        _session(session)
        if objective is not None:
            _text(objective, "objective", MAX_OBJECTIVE, empty=True)
        if facts is not None and not valid_facts(facts):
            raise ValueError("Invalid session facts")
        if availability is not None and availability not in ("busy", "idle", "stopped", "unknown"):
            raise ValueError("Invalid session availability")
        if client is not None:
            _text(client, "client", 80)
        if agent is not None:
            _text(agent, "agent", 256)
        with self.lock.acquire():
            state = self._load()
            old = state["sessions"].get(session, {})
            if not old and len(state["sessions"]) >= MAX_ITEMS:
                raise ValueError("Session limit reached")
            state["sessions"][session] = {
                **old, "client": client or old.get("client") or "cli", "agent": agent or old.get("agent") or uuid.uuid4().hex,
                "paused": old.get("paused", False) if paused is None else bool(paused),
                "availability": availability or old.get("availability", "unknown"), "seen": time.time(),
                "objective": old.get("objective", "") if objective is None else objective,
                "objective_at": old.get("objective_at", 0) if objective is None else time.time(),
                "facts": old.get("facts", {}) if facts is None else facts}
            self._save(state)
        return {"session": session, **state["sessions"][session]}

    def review_ci(self):
        """Record the owner's explicit review, never infer it from peer metadata."""
        with self.net_lock.acquire(), self.lock.acquire():
            state = self._load()
            state["ci_tree"], risky = self._ci_scan(state)
            state["ci_reviewed"] = True
            self._save(state)
        return {"ci_reviewed": True, "workflows_triggered_by_new_branches": risky}

    def scan_ci(self):
        """Inspect default-branch workflows; auto-approve publication only if none react to mail branches."""
        with self.net_lock.acquire(), self.lock.acquire():
            state = self._load()
            tree, risky = self._ci_scan(state)
            if not risky:
                state["ci_tree"], state["ci_reviewed"] = tree, True
            self._save(state)
        return {"ci_reviewed": state["ci_reviewed"], "workflows_needing_review": risky}

    def _ci_tree(self, state):
        return self._ci_scan(state)[0]

    def _ci_scan(self, state):
        """Fingerprint default-branch workflows and flag any that fire on branch creation.

        Never checks out remote files; workflow YAML is read as bounded blobs.
        """
        head = git(self.transport, "ls-remote", "--exit-code", state["remote"], "HEAD", check=False)
        if head.returncode == 2:
            return "unborn", []
        if head.returncode:
            raise GitError("Cannot inspect default branch for CI review")
        oid = head.stdout.split()[0]
        if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
            raise ValueError("Invalid remote HEAD")
        if state.get("ci_head") == oid and "ci_observed_risky" in state:
            return state["ci_observed_tree"], state["ci_observed_risky"]
        git(self.transport, "fetch", "--depth=1", "--no-tags", state["remote"], oid)
        tree = git(self.transport, "ls-tree", "FETCH_HEAD", "--", ".github/workflows").stdout
        fingerprint = hashlib.sha256(tree.encode()).hexdigest()
        risky = []
        listing = git(self.transport, "ls-tree", "-r", "-l", "FETCH_HEAD", "--", ".github/workflows").stdout
        entries = [line for line in listing.splitlines() if line.strip()]
        if len(entries) > MAX_WORKFLOWS:
            risky.append(".github/workflows (too many files to scan)")
            entries = []
        for line in entries:
            header, path = line.split("\t", 1)
            _mode, kind, blob, size = header.split()
            if kind != "blob" or not path.endswith((".yml", ".yaml")):
                continue
            if size == "-" or int(size) > MAX_WORKFLOW_BYTES:
                risky.append(path + " (too large to scan)")
                continue
            text = git(self.transport, "cat-file", "blob", blob).stdout
            if workflow_needs_review(text):
                risky.append(path)
        state["ci_head"], state["ci_observed_tree"], state["ci_observed_risky"] = oid, fingerprint, risky
        return fingerprint, risky

    def _prune(self, state):
        for bucket in ("outbox", "inbox"):
            state[bucket] = {k: v for k, v in state[bucket].items() if v["expires"] > time.time()}

    def _queue(self, state, recipient, body, kind="message", envelope_id=None):
        self._prune(state)
        if len(state["outbox"]) >= MAX_ITEMS:
            raise ValueError("Outbox full; allow messages to expire before adding more")
        expires = time.time() + TTL
        envelope = seal_envelope(state["private"], state["device"], recipient, kind, body,
                                 expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                                 **({"envelope_id": envelope_id} if envelope_id else {}))
        state["outbox"][envelope.id] = {"envelope": envelope.to_public_dict(),
                                      "expires": expires, "status": "queued",
                                      "summary": {label: body[key] for key, label in (
                                          ("session", "target_session"), ("sender_session", "sender_session"),
                                          ("id", "acknowledges")) if key in body}}
        return envelope.id

    def send(self, session: str, device: str, target: str, content: str, message_id: str | None = None,
             addressed: dict | None = None):
        _key(device)
        _session(target)
        _text(content, "content", 16384)
        if message_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", message_id):
            raise ValueError("message_id must be a plain identifier")
        with self.lock.acquire():
            state = self._load()
            if session not in state["sessions"]:
                raise ValueError("Register the sending session locally first")
            if device not in state["peers"]:
                raise ValueError("Unknown remote device; check status for connected peers")
            digest = hashlib.sha256(_json([device, target, session, content]).encode()).hexdigest()
            existing = state["outbox"].get(message_id) if message_id else None
            if existing:
                # Retrying the same id is safe only for the identical message.
                if existing.get("digest") != digest:
                    raise ValueError("message_id already belongs to another message")
                return {"id": message_id, "status": existing["status"], "duplicate": True}
            body = {"space": state["space"], "session": target, "sender_session": session, "content": content}
            if addressed is not None:
                body["addressed"] = addressed
            mid = self._queue(state, device, body, envelope_id=message_id)
            state["outbox"][mid]["digest"] = digest
            self._save(state)
        return {"id": mid, "status": "queued"}

    def read(self, session: str):
        with self.lock.acquire():
            state = self._load()
            self._prune(state)
            result = []
            for mid, item in state["inbox"].items():
                if item["acknowledged"] or item["sender"] not in state["peers"]:
                    continue
                env = open_envelope(item["envelope"], state["private"])
                if env.body["session"] == session:
                    result.append({"id": mid, "device": env.from_id,
                                   **{k: env.body[k] for k in ("space", "session", "sender_session", "content")},
                                   "addressed": env.body.get("addressed") or {"mode": "direct"}})
        return {"messages": result, "trust_boundary": BOUNDARY}

    def ack(self, session: str, message_id: str):
        with self.lock.acquire():
            state = self._load()
            self._prune(state)
            item = state["inbox"].get(message_id)
            if not item:
                raise ValueError("Unknown or expired message")
            env = open_envelope(item["envelope"], state["private"])
            if env.body["session"] != session or item["sender"] not in state["peers"]:
                raise ValueError("Message is not addressed to this enrolled session")
            if not item["acknowledged"]:
                self._queue(state, env.from_id, {"space": state["space"], "id": env.id}, "receipt")
                item["acknowledged"] = True
                self._save(state)
        return {"id": message_id, "acknowledged": True}

    def remote_sessions(self):
        """Sessions advertised by admitted devices, addressed as device/session."""
        with self.lock.acquire():
            state = self._load()
        from darkmatter.collaboration import _card
        found = []
        for device, sessions in state["peer_sessions"].items():
            if device not in state["peers"]:
                continue
            for sid, member in sessions.items():
                found.append(_card({"id": device + "/" + sid, "device": device, "session": sid,
                                    "client": member.get("client"), "availability": member.get("availability"),
                                    "paused": member.get("paused"), "objective": member.get("objective") or "",
                                    "objective_at": member.get("objective_at") or 0,
                                    "facts": member.get("facts") or {}, "host": member.get("host") or "",
                                    "project": project_name(state["remote"]),
                                    "seen": member.get("seen"), "where": "remote",
                                    **({} if "host" in member and member["host"] is not None else
                                       {"outdated": "That machine runs DarkMatter older than 3.15: no cards, "
                                                    "and no network discovery before 3.14. Upgrade it."})}))
        return sorted(found, key=lambda item: -(item["seen"] or 0))[:100]

    def has_incoming(self, message_id: str):
        with self.lock.acquire():
            return message_id in self._load()["inbox"]

    def delivery(self, message_id: str):
        with self.lock.acquire():
            item = self._load()["outbox"].get(message_id)
        return None if item is None else item["status"]

    def status(self):
        with self.lock.acquire():
            state = self._load()
            self._prune(state)
            return {"space": state["space"], "device": state["device"],
                    "membership": state.get("membership", "pinned"),
                    "auto_peers": state.get("auto_peers", []),
                    "blocked_devices": state.get("blocked_devices", []),
                    "sessions": state["sessions"], "peers": state["peers"],
                    "peer_sessions": state["peer_sessions"], "ci_reviewed": state["ci_reviewed"],
                    "delivery": {k: v["status"] for k, v in state["outbox"].items()},
                    "wake_attempts": state["wake_attempts"]}

    def _branch(self, state, device):
        return PREFIX + state["space"] + "/" + device

    @staticmethod
    def _public_sessions(state):
        # Sessions unseen for the retention window are no longer advertised.
        horizon = time.time() - TTL
        return {sid: {**{f: member[f] for f in ("client", "agent", "paused", "availability", "seen")},
                      "objective": member.get("objective", ""), "objective_at": member.get("objective_at", 0),
                      "facts": member.get("facts", {}), "host": host_name()}
                for sid, member in state["sessions"].items() if member.get("seen", 0) > horizon}

    def _publication_preview(self, state):
        self._prune(state)
        envelopes = {mid: item["envelope"] for mid, item in sorted(state["outbox"].items())}
        sessions = self._public_sessions(state)
        plan = {"remote": state["remote"], "branch": self._branch(state, state["device"]),
                "space": state["space"], "device": state["device"],
                "membership": state.get("membership", "pinned"),
                # Heartbeat timestamps and changing file lists are not reasons to push.
                "sessions": {sid: {k: v for k, v in member.items() if k not in ("seen", "facts")}
                             for sid, member in sessions.items()}, "envelopes": envelopes}
        fingerprint = hashlib.sha256(_json(plan).encode()).hexdigest()
        prior = state.get("published_envelope_hashes")
        outgoing = [{"id": mid, "kind": env["type"], "recipient_device": env["to"],
                     "expires_at": env.get("expires_at"),
                     "envelope_sha256": hashlib.sha256(_json(env).encode()).hexdigest(),
                     "previously_published": None if prior is None else
                     prior.get(mid) == hashlib.sha256(_json(env).encode()).hexdigest(),
                     **state["outbox"][mid].get("summary", {})}
                    for mid, env in envelopes.items()]
        return {"success": True, "preview_id": fingerprint, "remote": state["remote"],
                "branch": plan["branch"], "space": state["space"], "device": state["device"],
                "presence": {"membership": plan["membership"], "sessions": sessions,
                             "session_last_seen_may_refresh": True,
                             "refresh_timestamp_and_nonce": plan["membership"] == "repo-writers"},
                "outgoing": outgoing,
                "removed_envelope_ids": sorted(set(prior or {}) - set(envelopes)),
                "publication_effects": {"remote_write": True, "membership_change": False,
                                        "wake_execution": False},
                "ci_review_recorded": state["ci_reviewed"],
                "note": "All retained envelopes are republished, including receipts and acknowledged mail. "
                        "No message bodies or private keys are included in this preview. "
                        "Session last-seen timestamps may refresh without changing the preview ID. "
                        "Publication still checks current CI policy and Git permissions."}

    def preview(self):
        """Local-only inspection; no Git/network calls and no durable state changes."""
        with self.lock.acquire():
            return self._publication_preview(self._load())

    def publish(self, expected_preview: str):
        """Publish exactly the reviewed correspondence/presence, without enrollment."""
        with self.net_lock.acquire(), self.lock.acquire():
            state = self._load()
            plan = self._publication_preview(state)
            if not expected_preview or expected_preview != plan["preview_id"]:
                return {"success": False, "error": "Publication changed or preview missing; run preview again"}
            try:
                self._publish(state, expected_preview=expected_preview)
            except (GitError, ValueError, OSError) as exc:
                self._save(state)
                return {"success": False, "error": str(exc)}
            self._save(state)
        return {"success": True, "published": plan, "effects": plan["publication_effects"]}

    def _needs_publish(self, state):
        """Publish only when correspondence/presence changed or presence is due for refresh."""
        age = time.time() - state.get("published_time", 0)
        return (state.get("published_preview") != self._publication_preview(state)["preview_id"]
                or age > PRESENCE_REFRESH
                or (age > FACTS_REFRESH and state.get("published_facts") != self._facts_digest(state)))

    def _facts_digest(self, state):
        return hashlib.sha256(_json({sid: m.get("facts", {}) for sid, m in state["sessions"].items()}).encode()).hexdigest()

    def _publish(self, state, *, expected_preview=None):
        state.pop("connect_proof", None)
        auto = state.get("ci_review") == "auto"
        if not state["ci_reviewed"] and not auto:
            raise ValueError("Publication disabled until the owner reviews repository CI and runs ci-reviewed")
        tree = self._ci_tree(state)
        risky = [] if tree == "unborn" else state.get("ci_observed_risky", [])
        if tree != state.get("ci_tree"):
            if auto and not risky:
                state["ci_tree"], state["ci_reviewed"] = tree, True
            else:
                state["ci_reviewed"] = False
                detail = f" ({', '.join(risky)} may run on new mail branches; exclude darkmatter/mail/**)" if risky else ""
                if state.get("ci_tree") is None:
                    raise ValueError("Publication disabled until CI review" + detail
                                     + "; then run `darkmatter space ci-reviewed`")
                raise ValueError("Default-branch workflows changed; review CI again before publishing" + detail)
        self._prune(state)
        if expected_preview and self._publication_preview(state)["preview_id"] != expected_preview:
            raise ValueError("Publication changed during preflight; run preview again")
        payload = {"version": 1, "space": state["space"], "device": state["device"],
                   "membership": state.get("membership", "pinned"),
                   "sessions": self._public_sessions(state),
                   "envelopes": [v["envelope"] for v in state["outbox"].values()]}
        if payload["membership"] == "repo-writers":
            # Require a real push for automatic admission, even with no new mail.
            payload.update(published_at=time.time(), publication_id=uuid.uuid4().hex)
        signed = {"payload": payload, "signature": sign_payload(state["private"], DOMAIN, _json(payload))}
        text = _json(signed)
        if len(text.encode()) > MAX_BLOB:
            raise ValueError("Mailbox snapshot exceeds size limit")
        target = self.transport / "mail.json"
        # Only our own serialized data enters this private worktree.
        atomic_write_text(target, text)
        git(self.transport, "add", "--", "mail.json")
        if git(self.transport, "diff", "--cached", "--quiet", check=False).returncode:
            git(self.transport, "commit", "-m", "DarkMatter mailbox [skip ci] [skip actions]")
        git(self.transport, "push", state["remote"], "HEAD:refs/heads/" + self._branch(state, state["device"]))
        head = git(self.transport, "rev-parse", "HEAD").stdout.strip()
        state["published_envelope_hashes"] = {
            mid: hashlib.sha256(_json(item["envelope"]).encode()).hexdigest()
            for mid, item in state["outbox"].items()}
        state["published_preview"] = self._publication_preview(state)["preview_id"]
        state["published_time"], state["published_head"] = time.time(), head
        state["published_facts"] = self._facts_digest(state)
        if payload["membership"] == "repo-writers":
            state["connect_proof"] = {"time": time.time(), "remote": state["remote"],
                                      "branch": self._branch(state, state["device"]), "head": head}

    def _fetch(self, state, device, head=None):
        branch = "refs/heads/" + self._branch(state, device)
        if head is None:
            exists = git(self.transport, "ls-remote", "--exit-code", state["remote"], branch, check=False)
            if exists.returncode == 2:
                return None
            if exists.returncode:
                raise GitError("Could not query enrolled peer branch")
        git(self.transport, "fetch", "--depth=1", "--no-tags", state["remote"], branch)
        # Inspect only the one protocol blob. Never checkout remote files or filters.
        tree = git(self.transport, "ls-tree", "-l", "FETCH_HEAD", "--", "mail.json").stdout.strip()
        if not tree:
            raise ValueError("Missing peer mailbox")
        header, name = tree.split("\t", 1)
        mode, kind, oid, size = header.split()
        if name != "mail.json" or mode != "100644" or kind != "blob" or int(size) > MAX_BLOB:
            raise ValueError("Invalid or oversized peer mailbox blob")
        signed = json.loads(git(self.transport, "cat-file", "blob", oid).stdout)
        payload = signed["payload"]
        if (payload.get("version") != 1 or payload.get("device") != device
                or payload.get("space") != state["space"]
                or not verify_signed_payload(device, signed.get("signature", ""), DOMAIN, _json(payload))):
            raise ValueError("Invalid repo-space snapshot signature or membership")
        if not isinstance(payload.get("sessions"), dict) or len(payload["sessions"]) > MAX_ITEMS:
            raise ValueError("Invalid peer session list")
        if not isinstance(payload.get("envelopes"), list) or len(payload["envelopes"]) > MAX_ITEMS:
            raise ValueError("Invalid peer envelope list")
        for sid, member in payload["sessions"].items():
            _session(sid)
            if not isinstance(member, dict):
                raise ValueError("Invalid peer session descriptor")
            _text(member.get("client"), "client", 80)
            _text(member.get("agent"), "agent", 256)
            if member.get("availability") not in ("busy", "idle", "stopped", "unknown"):
                raise ValueError("Invalid peer availability")
            if type(member.get("paused")) is not bool:
                raise ValueError("Invalid peer pause state")
            if member.get("objective") is not None:
                _text(member["objective"], "objective", MAX_OBJECTIVE, empty=True)
            if not valid_facts(member.get("facts")) or type(member.get("objective_at", 0)) not in (int, float):
                raise ValueError("Invalid peer session facts")
            if member.get("host") is not None:
                _text(member["host"], "host", 128, empty=True)
        return payload

    def _remote_heads(self, state):
        """One ls-remote for every mail branch in this space: {device: commit}."""
        prefix = "refs/heads/" + PREFIX + state["space"] + "/"
        output = git(self.transport, "ls-remote", "--heads", state["remote"], prefix + "*").stdout
        # Bound candidate processing before any per-peer fetch. Git pack/output
        # resource isolation still belongs to the host, as for pinned transports.
        if len(output) > 16384:
            raise ValueError("Repository discovery advertisement exceeds limit")
        heads = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
                raise ValueError("Invalid repository discovery ref")
            ref = fields[1]
            if ref.startswith(prefix) and re.fullmatch(r"[0-9a-f]{64}", ref[len(prefix):]):
                heads[ref[len(prefix):]] = fields[0]
        return heads

    def _discover(self, state, heads=None):
        """Only refs advertised by the exact configured remote are admission evidence.

        The remote ACL is the trust boundary: a writer can publish somebody else's
        signed presence too. This is not proof of a GitHub account or live access.
        """
        heads = self._remote_heads(state) if heads is None else heads
        devices = set(heads) - {state["device"]}
        if len(devices) > MAX_PEERS:
            raise ValueError("Repository discovery device limit exceeded")
        return sorted(devices - set(state["peers"]) - set(state.get("blocked_devices", [])))

    @staticmethod
    def _validate_presence(payload):
        if payload.get("membership") != "repo-writers":
            raise ValueError("Peer has not enabled repository-writer membership")
        published = payload.get("published_at")
        if (type(published) not in (float, int)
                or not time.time() - TTL <= published <= time.time() + 300):
            raise ValueError("Expired or invalid repository presence")

    def _receive(self, state, device, payload, *, receive_mail=True):
        # Validate complete snapshot before mutating durable state.
        incoming, receipts = [], []
        seen_ids = set()
        for raw in payload["envelopes"]:
            if not isinstance(raw, dict):
                raise ValueError("Malformed envelope entry")
            if raw.get("to") != state["device"]:
                continue
            env = open_envelope(raw, state["private"])
            if env.id in seen_ids:
                raise ValueError("Duplicate message id in snapshot")
            seen_ids.add(env.id)
            existing = state["inbox"].get(env.id)
            if existing and existing["envelope"] != env.to_public_dict():
                raise ValueError("Message id reused with different envelope")
            if env.from_id != device or env.body.get("space") != state["space"]:
                raise ValueError("Envelope sender or space mismatch")
            if not env.expires_at or is_expired(env):
                continue
            expiry = datetime.fromisoformat(env.expires_at.replace("Z", "+00:00"))
            expires = (expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)).timestamp()
            if expires > time.time() + TTL + 300:
                raise ValueError("Envelope expiry exceeds retention limit")
            if env.type == "receipt":
                original = state["outbox"].get(env.body.get("id"))
                if original and original["envelope"]["to"] == device:
                    receipts.append(env.body["id"])
            elif env.type == "message":
                _session(env.body.get("session"))
                _session(env.body.get("sender_session"))
                _text(env.body.get("content"), "content", 16384)
                if "addressed" in env.body and (not isinstance(env.body["addressed"], dict)
                                                or len(_json(env.body["addressed"])) > 1024):
                    raise ValueError("Invalid addressing metadata")
                if env.body["session"] in state["sessions"] and env.id not in state["inbox"]:
                    incoming.append((env, expires))
            else:
                raise ValueError("Unsupported repo-space envelope type")
        if receive_mail and len(state["inbox"]) + len(incoming) > MAX_ITEMS:
            raise ValueError("Inbox full; delivery deferred")
        if receive_mail:
            for env, expires in incoming:
                state["inbox"][env.id] = {"envelope": env.to_public_dict(), "sender": device,
                                         "expires": expires, "acknowledged": False}
            for mid in receipts:
                state["outbox"][mid]["status"] = "acknowledged"
        state["peer_sessions"][device] = {
            sid: {k: member.get(k) for k in ("client", "agent", "paused", "availability", "seen", "objective",
                                             "objective_at", "facts", "host")}
            for sid, member in payload["sessions"].items()}

    def fetch(self):
        """Fetch existing peers only. No push, discovery, enrollment, ack, or wake."""
        result = self._exchange(publish=False, discover=False)
        result["effects"] = {"remote_write": False, "membership_change": False,
                             "local_inbox_and_cache_update": True, "wake_execution": False}
        return result

    def connect(self):
        """Apply automatic membership without publishing or receiving correspondence.

        Consume a successful publication once, within five minutes, verifying its
        ref still exists. New admission always requires another authorized push.
        """
        with self.lock.acquire():
            state = self._load()
            proof = state.pop("connect_proof", None)
            before = set(state["peers"])
            self._clear_auto_peers(state)
            errors = {}
            try:
                if state.get("membership", "pinned") != "repo-writers":
                    raise ValueError("Enable repo-writers membership locally first")
                if (not proof or proof["remote"] != state["remote"]
                        or proof["branch"] != self._branch(state, state["device"])
                        or not 0 <= time.time() - proof["time"] <= CONNECT_PROOF_TTL):
                    raise ValueError("Connect requires a new successful publish within five minutes")
                tip = git(self.transport, "ls-remote", "--exit-code", state["remote"],
                          "refs/heads/" + proof["branch"]).stdout.split()
                if not tip or tip[0] != proof["head"]:
                    raise ValueError("Published presence changed remotely; publish again before connecting")
                for device in self._discover(state):
                    try:
                        payload = self._fetch(state, device)
                        if payload is None or payload.get("membership") != "repo-writers":
                            continue
                        self._validate_presence(payload)
                        if len(state["peers"]) >= MAX_PEERS:
                            raise ValueError("Device enrollment limit reached")
                        self._receive(state, device, payload, receive_mail=False)
                        state["peers"].append(device)
                        state["auto_peers"].append(device)
                    except (GitError, ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                        errors[device] = str(exc)
            except (GitError, ValueError, OSError) as exc:
                errors["connect"] = str(exc)
            self._save(state)
        return {"success": not errors, "errors": errors,
                "added": sorted(set(state["peers"]) - before),
                "removed": sorted(before - set(state["peers"])),
                "effects": {"remote_write": False, "membership_change": True,
                            "wake_execution": False}}

    def sync(self):
        """Publish if needed, discover writers, and receive changed peer mailboxes."""
        return self._exchange(publish=True, discover=True)

    def sync_if_due(self, interval: float):
        """Shared throttle so several local processes do not all poll the same remote."""
        with self.lock.acquire():
            state = self._load()
            if time.time() - state.get("last_sync", 0) < interval:
                return None
            state["last_sync"] = time.time()
            self._save(state)
        return self.sync()

    def _exchange(self, *, publish, discover):
        """Network phase on a snapshot without the state lock; apply results under it.

        A single ls-remote lists every mail branch. Branches whose commit and our
        local session set are unchanged since the last successful receive are not
        fetched again. Automatic admission requires that our own last publication
        is still the tip of our branch; failed publication clears automatic peers.
        """
        with self.net_lock.acquire():
            with self.lock.acquire():
                snap = self._load()
                self._prune(snap)
            errors, attempted, failed = {}, False, False
            auto = discover and snap.get("membership", "pinned") == "repo-writers"

            def publish_now():
                nonlocal attempted, failed
                attempted = True
                try:
                    self._publish(snap)
                except (GitError, ValueError, OSError) as exc:
                    errors["publish"], failed = str(exc), True

            if publish and self._needs_publish(snap):
                publish_now()
            auto_peers = set(snap.get("auto_peers", []))
            pinned = [p for p in snap["peers"] if p not in auto_peers]
            heads = None
            if auto and not failed:
                try:
                    heads = self._remote_heads(snap)
                    if publish and heads.get(snap["device"]) != snap.get("published_head"):
                        publish_now()  # Our presence vanished or diverged: prove write access again.
                        heads[snap["device"]] = snap.get("published_head")
                    if not failed:
                        self._discover(snap, heads)  # Enforce the device budget before any fetch.
                except (GitError, ValueError, OSError) as exc:
                    errors["discovery"], heads = str(exc), None
            admit = auto and not failed and heads is not None
            targets = list(pinned)
            if admit:
                blocked = set(snap.get("blocked_devices", []))
                targets += sorted(d for d in heads if d != snap["device"] and d not in pinned and d not in blocked)
            elif not discover:
                targets += sorted(auto_peers)
            if targets and heads is None and not (auto and failed):
                try:
                    heads = self._remote_heads(snap)
                except (GitError, ValueError, OSError) as exc:
                    errors["fetch"] = str(exc)
            cache = snap.get("peer_heads", {})
            sessions_key = hashlib.sha256(_json(sorted(snap["sessions"])).encode()).hexdigest()
            fetched = {}
            for device in targets:
                head = (heads or {}).get(device)
                if head is None:
                    continue
                entry = cache.get(device)
                if entry and entry.get("head") == head and entry.get("sessions") == sessions_key:
                    fetched[device] = (head, None)
                    continue
                try:
                    fetched[device] = (head, self._fetch(snap, device, head))
                except (GitError, ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                    errors[device] = str(exc)
            with self.lock.acquire():
                state = self._load()
                self._prune(state)
                if attempted:
                    for key in _PUBLISH_KEYS:
                        if key in snap:
                            state[key] = snap[key]
                        else:
                            state.pop(key, None)
                self._apply_exchange(state, fetched, errors, discover=discover,
                                     admit=admit and state.get("membership", "pinned") == "repo-writers")
                if discover:
                    state.pop("connect_proof", None)  # Combined sync already used this publication.
                state["last_sync"] = time.time()
                self._save(state)
        return {"success": not errors, "errors": errors}

    def _apply_exchange(self, state, fetched, errors, *, discover, admit):
        auto_before = list(state.get("auto_peers", []))
        pinned = [p for p in state["peers"] if p not in auto_before]
        blocked = set(state.get("blocked_devices", []))
        cache = state.setdefault("peer_heads", {})
        sessions_key = hashlib.sha256(_json(sorted(state["sessions"])).encode()).hexdigest()
        admitted = []
        for device, (head, payload) in fetched.items():
            automatic = device not in pinned
            if automatic and (device in blocked or not (admit or (not discover and device in auto_before))):
                continue
            try:
                if payload is None:
                    if automatic:
                        self._validate_presence({"membership": "repo-writers",
                                                 "published_at": cache[device].get("published_at")})
                else:
                    if automatic:
                        if payload.get("membership") != "repo-writers":
                            continue  # Legacy/pinned peers have not opted in.
                        self._validate_presence(payload)
                    self._receive(state, device, payload)
                    cache[device] = {"head": head, "sessions": sessions_key,
                                     "published_at": payload.get("published_at")}
                if automatic:
                    admitted.append(device)
            except (GitError, ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                errors[device] = str(exc)
                cache.pop(device, None)
        if not discover:
            return
        room = max(0, MAX_PEERS - len(pinned))
        if len(admitted) > room:
            errors["discovery"] = "Device enrollment limit reached"
            admitted = admitted[:room]
        for device in set(auto_before) - set(admitted):
            state["peer_sessions"].pop(device, None)
            cache.pop(device, None)
        state["peers"] = pinned + admitted
        state["auto_peers"] = admitted

    def configure_wake(self, session: str, argv: list[str], cwd: str, *, enabled=False):
        """Owner-configured executable; no shell, interpolation, or peer arguments."""
        if not isinstance(argv, list) or not argv or len(argv) > 32:
            raise ValueError("Wake command must be an argv list of 1-32 strings")
        for arg in argv:
            _text(arg, "wake argument", 4096)
        if not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
            raise ValueError("Wake executable must be an existing absolute file path")
        cwd = str(Path(cwd).expanduser().resolve(strict=True))
        if not Path(cwd).is_dir():
            raise ValueError("Wake working directory must be a directory")
        with self.lock.acquire():
            state = self._load()
            if session not in state["sessions"]:
                raise ValueError("Register session first")
            state["wake"][session] = {"argv": argv, "cwd": cwd, "enabled": bool(enabled)}
            self._save(state)
        return {"session": session, "enabled": bool(enabled)}

    def wake_once(self):
        """At-most-one attempt per inbox batch; failed/uncertain attempts need retry.

        Persist before launching so crashes never cause an automatic spend loop.
        Only identifiers reach the adapter; peer prose requires explicit read.
        """
        jobs = []
        with self.lock.acquire():
            state = self._load()
            self._prune(state)
            state["wake_attempted"] = {k: v for k, v in state["wake_attempted"].items() if v > time.time()}
            for session, config in state["wake"].items():
                if (not config["enabled"] or state["sessions"][session]["paused"]
                        or state["sessions"][session]["availability"] not in ("idle", "stopped")):
                    continue
                ids = []
                for mid, item in state["inbox"].items():
                    if item["acknowledged"] or item["sender"] not in state["peers"]:
                        continue
                    env = open_envelope(item["envelope"], state["private"])
                    if env.body["session"] == session:
                        ids.append(mid)
                if not ids or all(mid in state["wake_attempted"] for mid in ids):
                    continue
                history = [t for t in state["wake_history"].get(session, []) if time.time() - t < 3600]
                if len(history) >= 4:
                    continue
                digest = hashlib.sha256(_json(sorted(ids)).encode()).hexdigest()
                previous = state["wake_attempts"].get(session, {})
                if (previous.get("batch") == digest or previous.get("status") == "attempting"
                        or time.time() - previous.get("time", 0) < 300):
                    continue
                state["wake_attempts"][session] = {"batch": digest, "time": time.time(), "status": "attempting"}
                state["wake_history"][session] = history + [time.time()]
                for mid in ids:
                    state["wake_attempted"][mid] = state["inbox"][mid]["expires"]
                jobs.append((session, config, digest, ids))
            self._save(state)
        for session, config, digest, ids in jobs:
            # Recheck after any earlier slow adapter, so a newly paused/revoked
            # session does not run based on stale configuration.
            with self.lock.acquire():
                latest = self._load()
                member = latest["sessions"][session]
                if (member["paused"] or member["availability"] not in ("idle", "stopped")
                        or latest["wake"].get(session) != config
                        or any(latest["inbox"].get(mid, {}).get("sender") not in latest["peers"] for mid in ids)):
                    latest["wake_attempts"][session]["status"] = "cancelled"
                    self._save(latest)
                    continue
            event = {"type": "darkmatter.mail_available", "space": state["space"],
                     "session": session, "ids": ids, "trust_boundary": BOUNDARY}
            try:
                result = subprocess.run(config["argv"], input=_json(event), text=True,
                                        cwd=config["cwd"], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=60, check=False)
                status = "adapter_accepted" if result.returncode == 0 else "failed"
            except (OSError, subprocess.TimeoutExpired):
                status = "failed"
            with self.lock.acquire():
                latest = self._load()
                if latest["wake_attempts"].get(session, {}).get("batch") == digest:
                    latest["wake_attempts"][session]["status"] = status
                    self._save(latest)
        return {"attempted": len(jobs)}

    def retry_wake(self, session: str):
        """Owner-only retry after inspecting failed or uncertain adapter execution."""
        with self.lock.acquire():
            state = self._load()
            if session not in state["sessions"]:
                raise ValueError("Unknown local session")
            state["wake_attempts"].pop(session, None)
            for mid, item in state["inbox"].items():
                if open_envelope(item["envelope"], state["private"]).body["session"] == session:
                    state["wake_attempted"].pop(mid, None)
            self._save(state)
        return {"session": session, "retry_requested": True}

    def notice(self, session: str, client: str, *, force=False, remind_unread=False, facts=None):
        """Identifiers-only hook notice; None when unread ids and remote peers are unchanged."""
        self.register(session, client, availability="busy", facts=facts)
        ids = [m["id"] for m in self.read(session)["messages"]]
        remote = sorted(item["id"] for item in self.remote_sessions() if item["availability"] != "stopped")
        with self.lock.acquire():
            state = self._load()
            digest = hashlib.sha256(_json([ids, remote]).encode()).hexdigest()
            session_state = state["sessions"][session]
            if not force and session_state.get("notified") == digest and not (remind_unread and ids):
                return None
            session_state["notified"] = digest
            self._save(state)
            return {"space": state["space"], "device": state["device"], "session": session,
                    "unread_ids": ids, "remote_peers": len(remote), "trust_boundary": BOUNDARY}
