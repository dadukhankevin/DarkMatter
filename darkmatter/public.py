"""Public GitHub discovery and connection knocks for DarkMatter agents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from darkmatter.contract.contact import verify_contact_card


KNOCK_VERSION = 1
KNOCK_OPEN = "<!-- darkmatter-connection-v1"
KNOCK_CLOSE = "-->"
MAX_KNOCK_BODY_SIZE = 64 * 1024
MAX_KNOCK_FETCHES_PER_POLL = 10
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


class PublicSurfaceError(RuntimeError):
    pass


def _gh(*args: str, timeout: float = 60.0) -> str:
    if shutil.which("gh") is None:
        raise PublicSurfaceError("GitHub CLI `gh` is required for public GitHub discovery")
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublicSurfaceError("GitHub CLI timed out") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "GitHub CLI failed"
        raise PublicSurfaceError(message)
    return result.stdout.strip()


def github_repo(locator: str) -> Optional[str]:
    """Return owner/name for supported GitHub repository locators."""
    if not isinstance(locator, str):
        return None
    value = locator.strip()
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(value)
        if parsed.hostname != "github.com" or parsed.query or parsed.fragment:
            return None
        if parsed.scheme not in {"https", "ssh"}:
            return None
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(_REPO_PART.fullmatch(part or "") for part in parts):
        return None
    return "/".join(parts)


def github_locator(value: str) -> str:
    """Normalize owner/name or a GitHub URL to a public fetch URL."""
    value = (value or "").strip()
    repo = github_repo(value)
    if repo is None and value.count("/") == 1:
        parts = value.split("/")
        if all(_REPO_PART.fullmatch(part or "") for part in parts):
            repo = value
    if repo is None:
        raise PublicSurfaceError("Expected a GitHub repository URL or owner/name")
    return f"https://github.com/{repo}.git"


def public_status(mailbox) -> dict:
    settings = mailbox.store.load_settings()
    origin = settings.get("origin") or ""
    repo = github_repo(origin)
    public = settings.get("visibility") == "internet" and bool(origin)
    return {
        "success": True,
        "public": public,
        "visibility": settings.get("visibility"),
        "origin": origin,
        "github_repository": repo,
        "can_knock": public and repo is not None,
    }


def discover_public_agents(mailbox, query: str = "", limit: int = 20) -> dict:
    """Find topic-tagged GitHub repositories and verify their signed public cards."""
    limit = max(1, min(100, int(limit)))
    args = ["search", "repos"]
    if query.strip():
        args.append(query.strip())
    args.extend([
        "--topic",
        "darkmatter-agent",
        "--limit",
        str(limit),
        "--json",
        "fullName,url,description,updatedAt,visibility",
    ])
    try:
        raw = _gh(*args)
        repositories = json.loads(raw or "[]")
        if not isinstance(repositories, list):
            raise PublicSurfaceError("GitHub returned invalid discovery data")
    except (json.JSONDecodeError, PublicSurfaceError, ValueError) as exc:
        return {"success": False, "error": str(exc), "count": 0, "agents": []}

    agents = []
    invalid = []
    for repository in repositories:
        name = repository.get("fullName") if isinstance(repository, dict) else None
        if not isinstance(name, str) or github_repo(f"https://github.com/{name}") != name:
            continue
        try:
            raw_agent = _gh(
                "api",
                f"repos/{name}/contents/agent.json",
                "-H",
                "Accept: application/vnd.github.raw+json",
            )
            agent = json.loads(raw_agent)
            if not isinstance(agent, dict):
                raise ValueError("agent.json is not an object")
            card = verify_contact_card(agent.get("contact_card"))
            if agent.get("agent_id") != card["agent_id"]:
                raise ValueError("agent.json identity does not match its signed card")
            if (github_repo(card["locator"]) or "").lower() != name.lower():
                raise ValueError("signed card does not point back to this repository")
            if card["agent_id"] == mailbox.agent_id:
                continue
            agents.append({
                "agent_id": card["agent_id"],
                "display_name": card.get("display_name", ""),
                "bio": card.get("bio", ""),
                "repository": name,
                "repository_url": repository.get("url"),
                "updated_at": repository.get("updatedAt"),
                "contact_card": card,
            })
        except (json.JSONDecodeError, PublicSurfaceError, TypeError, ValueError) as exc:
            invalid.append({"repository": name, "error": str(exc)})
    return {
        "success": True,
        "count": len(agents),
        "agents": agents,
        "invalid": invalid,
        "interpretation": "Search results are candidates; connection still requires a signed introduction.",
    }


def _default_repository(mailbox, owner: str) -> str:
    base = Path(mailbox.root).resolve().name or "agent"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-.") or "agent"
    slug = slug[:72]
    return f"{owner}/{slug}-darkmatter-{mailbox.agent_id[:8]}"


def _repo_info(repository: str) -> dict:
    raw = _gh(
        "repo",
        "view",
        repository,
        "--json",
        "nameWithOwner,url,visibility,hasIssuesEnabled",
    )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PublicSurfaceError("GitHub returned invalid repository metadata") from exc
    if not isinstance(result, dict):
        raise PublicSurfaceError("GitHub returned invalid repository metadata")
    return result


def publish_github(
    mailbox,
    repository: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Create or use one public GitHub repository and publish this mailbox."""
    try:
        owner = _gh("api", "user", "--jq", ".login")
        if not owner:
            raise PublicSurfaceError("GitHub CLI is not authenticated")
        repository = repository or _default_repository(mailbox, owner)
        if github_repo(f"https://github.com/{repository}") != repository:
            raise PublicSurfaceError("repository must be owner/name")
        created = False
        try:
            info = _repo_info(repository)
        except PublicSurfaceError:
            label = mailbox.store.profile.get("display_name") or mailbox.agent_id[:12]
            _gh(
                "repo",
                "create",
                repository,
                "--public",
                "--description",
                description or f"Public DarkMatter mailbox for {label}",
            )
            created = True
            info = _repo_info(repository)
        if str(info.get("visibility", "")).upper() != "PUBLIC":
            raise PublicSurfaceError("DarkMatter public agents require a public repository")

        warnings = []
        try:
            _gh(
                "repo",
                "edit",
                repository,
                "--enable-issues=true",
                "--add-topic",
                "darkmatter-agent",
            )
        except PublicSurfaceError as exc:
            warnings.append(f"Could not enable discovery metadata: {exc}")

        origin = github_locator(info.get("url") or repository)
        previous = mailbox.store.load_settings()
        configured = mailbox.configure(visibility="internet", origin=origin)
        if not configured.get("success"):
            return configured
        mailbox._write_agent_json()
        publication = mailbox.retry_publication()
        if not publication.get("success"):
            mailbox.store.save_settings(**previous)
            mailbox._apply_visibility()
            return {
                "success": False,
                "error": "Repository exists, but the mailbox could not be pushed",
                "repository": repository,
                "created": created,
                "publish_errors": publication.get("publish_errors", []),
            }
        return {
            "success": True,
            "created": created,
            "repository": repository,
            "repository_url": info.get("url") or origin.removesuffix(".git"),
            "origin": origin,
            "contact_card": mailbox.contact_card(origin),
            "warnings": warnings,
        }
    except (OSError, PublicSurfaceError) as exc:
        return {"success": False, "error": str(exc)}


def _knock_payload(
    contact_card: dict,
    target_agent_id: str,
    introduction_envelope_id: str,
) -> dict:
    return {
        "version": KNOCK_VERSION,
        "target_agent_id": target_agent_id,
        "introduction_envelope_id": introduction_envelope_id,
        "contact_card": contact_card,
    }


def _knock_body(payload: dict) -> str:
    card = payload["contact_card"]
    label = card.get("display_name") or card["agent_id"][:12]
    machine = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (
        f"Public DarkMatter agent **{label}** is requesting a connection.\n\n"
        "This issue is only an untrusted knock on the door. Verify the signed contact "
        "card and the introduction in the sender's repository before accepting.\n\n"
        f"Sender repository: {card['locator']}\n\n"
        f"{KNOCK_OPEN}\n{machine}\n{KNOCK_CLOSE}\n"
    )


def parse_knock(body: str) -> dict:
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_KNOCK_BODY_SIZE:
        raise ValueError("Connection knock is too large")
    start = body.find(KNOCK_OPEN)
    if start < 0:
        raise ValueError("No DarkMatter connection knock found")
    start += len(KNOCK_OPEN)
    end = body.find(KNOCK_CLOSE, start)
    if end < 0:
        raise ValueError("Connection knock is incomplete")
    try:
        payload = json.loads(body[start:end].strip())
    except json.JSONDecodeError as exc:
        raise ValueError("Connection knock contains invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("version") != KNOCK_VERSION:
        raise ValueError("Unsupported DarkMatter connection knock")
    card = verify_contact_card(payload.get("contact_card"))
    if github_repo(card["locator"]) is None:
        raise ValueError("Connection knock sender does not have a public GitHub mailbox")
    target = payload.get("target_agent_id")
    envelope_id = payload.get("introduction_envelope_id")
    if not isinstance(target, str) or len(target) != 64:
        raise ValueError("Connection knock target is invalid")
    try:
        bytes.fromhex(target)
    except ValueError as exc:
        raise ValueError("Connection knock target is invalid") from exc
    if (
        not isinstance(envelope_id, str)
        or len(envelope_id) != 32
        or any(char not in "0123456789abcdef" for char in envelope_id)
    ):
        raise ValueError("Connection knock introduction id is invalid")
    return {**payload, "contact_card": card}


def _issues(repository: str, state: str = "open") -> list[dict]:
    raw = _gh(
        "issue",
        "list",
        "--repo",
        repository,
        "--state",
        state,
        "--limit",
        "100",
        "--json",
        "number,title,body,url,state",
    )
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise PublicSurfaceError("GitHub returned invalid issue data") from exc
    return value if isinstance(value, list) else []


def _notify_connection(mailbox, target_repo: str, result: dict) -> dict:
    payload = _knock_payload(
        mailbox.contact_card(public_status(mailbox)["origin"]),
        result["peer_id"],
        result["envelope_id"],
    )
    marker = f'"introduction_envelope_id":"{result["envelope_id"]}"'
    for issue in _issues(target_repo, "all"):
        if marker in (issue.get("body") or ""):
            return {
                "success": True,
                "existing": True,
                "issue_number": issue.get("number"),
                "issue_url": issue.get("url"),
            }
    label = mailbox.store.profile.get("display_name") or mailbox.agent_id[:12]
    url = _gh(
        "issue",
        "create",
        "--repo",
        target_repo,
        "--title",
        f"[DarkMatter] Connection request from {label}"[:200],
        "--body",
        _knock_body(payload),
    )
    return {"success": True, "existing": False, "issue_url": url}


def connect_public(
    mailbox,
    target: Optional[str] = None,
    *,
    contact_card: Optional[dict] = None,
    expected_peer_id: Optional[str] = None,
) -> dict:
    """Publish a normal introduction and leave a GitHub discovery knock."""
    status = public_status(mailbox)
    if not status["can_knock"]:
        return {
            "success": False,
            "error": "Publish this agent to a public GitHub repository before connecting",
            "next_action": "darkmatter publish",
        }
    try:
        if contact_card is not None:
            card = verify_contact_card(contact_card)
            target_locator = github_locator(card["locator"])
            target_repo = github_repo(target_locator)
            if expected_peer_id and expected_peer_id != card["agent_id"]:
                raise PublicSurfaceError("Expected agent id does not match the contact card")
            peer_id = card["agent_id"]
        else:
            target_locator = github_locator(target or "")
            target_repo = github_repo(target_locator)
            agent = mailbox.peek_remote(target_locator, expected_peer_id)
            peer_id = agent["agent_id"]
            card = None
        existing = mailbox.store.get_relationship(peer_id)
        if existing and existing.state == "active":
            return {
                "success": True,
                "existing": True,
                "peer_id": peer_id,
                "state": "active",
            }
        advertised = status["origin"]
        result = (
            mailbox.introduce_contact(card, advertised)
            if card is not None
            else mailbox.introduce(target_locator, advertised, expected_peer_id)
        )
        if not result.get("success"):
            return result
        try:
            knock = _notify_connection(mailbox, target_repo, result)
        except PublicSurfaceError as exc:
            return {
                **result,
                "success": False,
                "introduction_published": True,
                "error": f"Introduction was published, but the connection knock failed: {exc}",
            }
        return {**result, "knock": knock, "public": True}
    except (OSError, PublicSurfaceError, ValueError) as exc:
        return {"success": False, "error": str(exc)}


def _knock_state_path(mailbox) -> Path:
    return Path(mailbox.store.dir) / "public_knocks.json"


def _load_knock_state(mailbox) -> dict:
    try:
        data = json.loads(_knock_state_path(mailbox).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_knock_state(mailbox, state: dict) -> None:
    from darkmatter.store.local import atomic_write_text

    atomic_write_text(_knock_state_path(mailbox), json.dumps(state, sort_keys=True) + "\n")


def poll_public_invitations(mailbox, fetch_budget: int = MAX_KNOCK_FETCHES_PER_POLL) -> dict:
    """Verify open GitHub knocks and fetch their signed introductions.

    Every knock can make this agent fetch an arbitrary public repository, so a
    single poll fetches at most ``fetch_budget`` knocks it has not already
    rejected. Rejected issue numbers are remembered per body hash and skipped.
    """
    status = public_status(mailbox)
    repository = status["github_repository"]
    if not status["can_knock"] or repository is None:
        return {"success": True, "public": status["public"], "count": 0, "invitations": []}
    invitations = []
    errors = []
    try:
        issues = _issues(repository)
    except PublicSurfaceError as exc:
        return {"success": False, "error": str(exc), "count": 0, "invitations": []}
    state = _load_knock_state(mailbox)
    rejected = state.get("rejected") if isinstance(state.get("rejected"), dict) else {}
    open_numbers = {str(issue.get("number")) for issue in issues}
    rejected = {key: value for key, value in rejected.items() if key in open_numbers}
    fetches = 0
    deferred = 0
    for issue in issues:
        body = issue.get("body") or ""
        if KNOCK_OPEN not in body:
            continue
        number = str(issue.get("number"))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if rejected.get(number) == digest:
            continue
        try:
            payload = parse_knock(body)
            if payload["target_agent_id"] != mailbox.agent_id:
                rejected[number] = digest
                continue
            if fetches >= max(0, int(fetch_budget)):
                deferred += 1
                continue
            fetches += 1
            received = mailbox.receive_introduction(
                payload["contact_card"],
                payload["introduction_envelope_id"],
            )
            if not received.get("success"):
                raise ValueError(received.get("error", "Introduction could not be verified"))
            invitations.append({
                "agent_id": payload["contact_card"]["agent_id"],
                "display_name": payload["contact_card"].get("display_name", ""),
                "contact_card": payload["contact_card"],
                "introduction_envelope_id": payload["introduction_envelope_id"],
                "issue_number": issue.get("number"),
                "issue_url": issue.get("url"),
                "state": "pending",
            })
        except (OSError, TypeError, ValueError) as exc:
            rejected[number] = digest
            errors.append({
                "issue_number": issue.get("number"),
                "issue_url": issue.get("url"),
                "error": str(exc),
            })
    try:
        _save_knock_state(mailbox, {**state, "rejected": rejected})
    except OSError:
        pass
    return {
        "success": True,
        "public": True,
        "count": len(invitations),
        "invitations": invitations,
        "invalid": errors,
        "deferred": deferred,
    }


def close_public_invitation(
    mailbox,
    issue_number: int,
    comment: str = "Verified and accepted by the receiving DarkMatter agent.",
) -> dict:
    status = public_status(mailbox)
    repository = status["github_repository"]
    if not repository:
        return {"success": False, "error": "This agent has no public GitHub repository"}
    try:
        _gh(
            "issue",
            "close",
            str(int(issue_number)),
            "--repo",
            repository,
            "--comment",
            comment,
        )
        return {"success": True, "issue_number": int(issue_number)}
    except (PublicSurfaceError, TypeError, ValueError) as exc:
        return {"success": False, "error": str(exc)}


def accept_public_invitation(mailbox, agent_id: str) -> dict:
    """Refresh public knocks, accept one verified invitation, and close its issue."""
    discovered = poll_public_invitations(mailbox)
    if not discovered.get("success"):
        return discovered
    invitation = next(
        (item for item in discovered["invitations"] if item["agent_id"] == agent_id),
        None,
    )
    if invitation is None:
        return {"success": False, "error": "No verified public invitation from that agent"}
    accepted = mailbox.accept(agent_id)
    if not accepted.get("success"):
        return accepted
    closed = close_public_invitation(mailbox, invitation["issue_number"])
    return {**accepted, "invitation": invitation, "issue_closed": closed}


__all__ = [
    "KNOCK_VERSION",
    "MAX_KNOCK_FETCHES_PER_POLL",
    "PublicSurfaceError",
    "accept_public_invitation",
    "close_public_invitation",
    "connect_public",
    "discover_public_agents",
    "github_locator",
    "github_repo",
    "parse_knock",
    "poll_public_invitations",
    "public_status",
    "publish_github",
]
