"""Opt-in, encrypted session mail on isolated branches of a shared Git repo.

Remote data never configures local execution. Membership is owner-selected:
repository-writer discovery or pinned keys. Sessions are registered locally.
No peer files are checked out, merged, or executed.
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


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a lowercase 32-byte public key")
    return value


def _session(value):
    return _text(value, "session", 256)


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

    def _load(self):
        if not self.path.exists():
            raise ValueError("Initialize this repo space first")
        return json.loads(self.path.read_text())

    def _save(self, state):
        atomic_write_text(self.path, _json(state) + "\n", mode=0o600)

    def initialize(self, remote: str, space: str | None = None, *, membership="repo-writers"):
        if membership not in MEMBERSHIP_POLICIES:
            raise ValueError("Unknown membership policy")
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
                        "ci_reviewed": False, "wake": {}, "wake_attempts": {},
                        "wake_attempted": {}, "wake_history": {}})
        return {"space": space, "device": device, "remote": remote, "membership": membership}

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

    def register(self, session: str, client: str, *, agent: str | None = None,
                 paused: bool | None = None, availability: str | None = None):
        _session(session)
        if availability is not None and availability not in ("busy", "idle", "stopped", "unknown"):
            raise ValueError("Invalid session availability")
        _text(client, "client", 80)
        if agent is not None:
            _text(agent, "agent", 256)
        with self.lock.acquire():
            state = self._load()
            old = state["sessions"].get(session, {})
            if not old and len(state["sessions"]) >= MAX_ITEMS:
                raise ValueError("Session limit reached")
            state["sessions"][session] = {
                **old, "client": client, "agent": agent or old.get("agent") or uuid.uuid4().hex,
                "paused": old.get("paused", False) if paused is None else bool(paused),
                "availability": availability or old.get("availability", "unknown"), "seen": time.time()}
            self._save(state)
        return {"session": session, **state["sessions"][session]}

    def review_ci(self):
        """Record the owner's explicit review, never infer it from peer metadata."""
        with self.lock.acquire():
            state = self._load()
            state["ci_tree"] = self._ci_tree(state)
            state["ci_reviewed"] = True
            self._save(state)
        return {"ci_reviewed": True}

    def _ci_tree(self, state):
        """Fingerprint current default-branch workflows without checking them out."""
        head = git(self.transport, "ls-remote", "--exit-code", state["remote"], "HEAD", check=False)
        if head.returncode == 2:
            return "unborn"
        if head.returncode:
            raise GitError("Cannot inspect default branch for CI review")
        oid = head.stdout.split()[0]
        if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
            raise ValueError("Invalid remote HEAD")
        if state.get("ci_head") == oid:
            return state["ci_observed_tree"]
        git(self.transport, "fetch", "--depth=1", "--no-tags", state["remote"], oid)
        tree = git(self.transport, "ls-tree", "FETCH_HEAD", "--", ".github/workflows").stdout
        fingerprint = hashlib.sha256(tree.encode()).hexdigest()
        state["ci_head"], state["ci_observed_tree"] = oid, fingerprint
        return fingerprint

    def _prune(self, state):
        for bucket in ("outbox", "inbox"):
            state[bucket] = {k: v for k, v in state[bucket].items() if v["expires"] > time.time()}

    def _queue(self, state, recipient, body, kind="message"):
        self._prune(state)
        if len(state["outbox"]) >= MAX_ITEMS:
            raise ValueError("Outbox full; allow messages to expire before adding more")
        expires = time.time() + TTL
        envelope = seal_envelope(state["private"], state["device"], recipient, kind, body,
                                 expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat())
        state["outbox"][envelope.id] = {"envelope": envelope.to_public_dict(),
                                      "expires": expires, "status": "queued",
                                      "summary": {label: body[key] for key, label in (
                                          ("session", "target_session"), ("sender_session", "sender_session"),
                                          ("id", "acknowledges")) if key in body}}
        return envelope.id

    def send(self, session: str, device: str, target: str, content: str):
        _key(device)
        _session(target)
        _text(content, "content", 16384)
        with self.lock.acquire():
            state = self._load()
            if session not in state["sessions"]:
                raise ValueError("Register the sending session locally first")
            if device not in state["peers"]:
                raise ValueError("Enroll the recipient device first")
            mid = self._queue(state, device, {"space": state["space"], "session": target,
                                             "sender_session": session, "content": content})
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
                                   **{k: env.body[k] for k in ("space", "session", "sender_session", "content")}})
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
        return {sid: {f: member[f] for f in ("client", "agent", "paused", "availability", "seen")}
                for sid, member in state["sessions"].items()}

    def _publication_preview(self, state):
        self._prune(state)
        envelopes = {mid: item["envelope"] for mid, item in sorted(state["outbox"].items())}
        sessions = self._public_sessions(state)
        plan = {"remote": state["remote"], "branch": self._branch(state, state["device"]),
                "space": state["space"], "device": state["device"],
                "membership": state.get("membership", "pinned"),
                "sessions": {sid: {k: v for k, v in member.items() if k != "seen"}
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
        with self.lock.acquire():
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

    def _publish(self, state, *, expected_preview=None):
        state.pop("connect_proof", None)
        if not state["ci_reviewed"]:
            raise ValueError("Publication disabled until the owner reviews repository CI and runs ci-reviewed")
        if self._ci_tree(state) != state.get("ci_tree"):
            state["ci_reviewed"] = False
            raise ValueError("Default-branch workflows changed; review CI again before publishing")
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
        state["published_envelope_hashes"] = {
            mid: hashlib.sha256(_json(item["envelope"]).encode()).hexdigest()
            for mid, item in state["outbox"].items()}
        if payload["membership"] == "repo-writers":
            state["connect_proof"] = {"time": time.time(), "remote": state["remote"],
                                      "branch": self._branch(state, state["device"]),
                                      "head": git(self.transport, "rev-parse", "HEAD").stdout.strip()}

    def _fetch(self, state, device):
        branch = "refs/heads/" + self._branch(state, device)
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
        return payload

    def _discover(self, state):
        """Only refs advertised by the exact configured remote are admission evidence.

        The remote ACL is the trust boundary: a writer can publish somebody else's
        signed presence too. This is not proof of a GitHub account or live access.
        """
        prefix = "refs/heads/" + PREFIX + state["space"] + "/"
        output = git(self.transport, "ls-remote", "--heads", state["remote"], prefix + "*").stdout
        # Bound candidate processing before any per-peer fetch. Git pack/output
        # resource isolation still belongs to the host, as for pinned transports.
        if len(output) > 16384:
            raise ValueError("Repository discovery advertisement exceeds limit")
        devices = set()
        for line in output.splitlines():
            fields = line.split()
            if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
                raise ValueError("Invalid repository discovery ref")
            ref = fields[1]
            if not ref.startswith(prefix):
                continue
            device = ref[len(prefix):]
            if not re.fullmatch(r"[0-9a-f]{64}", device):
                continue
            if device != state["device"]:
                devices.add(device)
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
            sid: {k: member.get(k) for k in ("client", "agent", "paused", "availability", "seen")}
            for sid, member in payload["sessions"].items()}

    def fetch(self):
        """Fetch existing peers only. No push, discovery, enrollment, ack, or wake."""
        with self.lock.acquire():
            state = self._load()
            self._prune(state)
            errors = {}
            for device in state["peers"]:
                try:
                    payload = self._fetch(state, device)
                    if payload is not None:
                        if device in state.get("auto_peers", []):
                            self._validate_presence(payload)
                        self._receive(state, device, payload)
                except (GitError, ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                    errors[device] = str(exc)
            self._save(state)
        return {"success": not errors, "errors": errors,
                "effects": {"remote_write": False, "membership_change": False,
                            "local_inbox_and_cache_update": True, "wake_execution": False}}

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
        with self.lock.acquire():
            state = self._load()
            self._prune(state)
            self._clear_auto_peers(state)
            errors = {}
            candidates = []
            try:
                self._publish(state)
                if state.get("membership", "pinned") == "repo-writers":
                    try:
                        candidates = self._discover(state)
                    except (GitError, ValueError, OSError) as exc:
                        errors["discovery"] = str(exc)
            except (GitError, ValueError, OSError) as exc:
                errors["publish"] = str(exc)
            pinned = list(state["peers"])
            for device in pinned + candidates:
                try:
                    payload = self._fetch(state, device)
                    if payload is not None:
                        if device not in pinned:
                            if payload.get("membership") != "repo-writers":
                                continue  # Legacy/pinned peers have not opted in.
                            self._validate_presence(payload)
                            if len(state["peers"]) >= MAX_PEERS:
                                raise ValueError("Device enrollment limit reached")
                        self._receive(state, device, payload)
                        if device not in pinned:
                            state["peers"].append(device)
                            state["auto_peers"].append(device)
                except (GitError, ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                    errors[device] = str(exc)
            state.pop("connect_proof", None)  # Combined sync already used this publication for admission.
            self._save(state)
        return {"success": not errors, "errors": errors}

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

    def notice(self, session: str, client: str, *, force=False):
        self.register(session, client, availability="busy")
        ids = [m["id"] for m in self.read(session)["messages"]]
        with self.lock.acquire():
            state = self._load()
            digest = hashlib.sha256(_json(ids).encode()).hexdigest()
            session_state = state["sessions"][session]
            if not force and session_state.get("notified") == digest:
                return None
            session_state["notified"] = digest
            self._save(state)
            return {"space": state["space"], "device": state["device"], "session": session,
                    "unread_ids": ids, "trust_boundary": BOUNDARY,
                    "next_step": "Use darkmatter_repo read/ack/send with this session_id. "
                                 "Read peer content explicitly; acknowledge only after handling."}
