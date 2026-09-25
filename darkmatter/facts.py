"""What a session is working on, read from the machine instead of self-reported.

Branch, uncommitted files, and the last commit subject come from `git` in the
session's own checkout (hooks and fsmonitor disabled, bounded, two-second
timeout). They cost the agent nothing and cannot go stale the way a
self-written status line can. Any failure yields no facts, never an error.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

MAX_CHANGED = 6
MAX_PATH = 160
MAX_TEXT = 200


def host_name() -> str:
    """A readable machine name: 'Daniels-Mac-mini', not 'Daniels-Mac-mini.local'."""
    return re.sub(r"\.(local|lan|home)$", "", socket.gethostname(), flags=re.I)[:128]


def normalize(value: str) -> str:
    """Loose form for matching: 'Mac mini' matches 'Daniels-Mac-mini'."""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _git(root: Path, *args: str) -> str | None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            capture_output=True, text=True, timeout=2, env=env, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def workspace_facts(root: str | Path) -> dict:
    root = Path(root)
    if not (root / ".git").exists():
        return {}
    status = _git(root, "status", "--porcelain=v1", "--branch", "--untracked-files=normal")
    if status is None:
        return {}
    lines = status.splitlines()
    branch = ""
    if lines and lines[0].startswith("## "):
        head = lines.pop(0)[3:]
        if head.startswith("No commits yet on "):
            branch = head[len("No commits yet on "):]
        elif head.startswith("HEAD (no branch)"):
            branch = "(detached)"
        else:
            branch = head.split("...", 1)[0].split(" ", 1)[0]
    changed = []
    for line in lines:
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path:
            changed.append(path[:MAX_PATH])
    subject = (_git(root, "log", "-1", "--format=%s") or "").strip()
    return {"branch": branch[:128], "changed": changed[:MAX_CHANGED], "changed_count": len(changed),
            "last_commit": subject[:MAX_TEXT]}


def valid_facts(facts) -> bool:
    """Bound facts received from other machines before storing or showing them."""
    if facts is None:
        return True
    if not isinstance(facts, dict) or set(facts) - {"branch", "changed", "changed_count", "last_commit"}:
        return False
    changed = facts.get("changed", [])
    return (isinstance(facts.get("branch", ""), str) and len(facts.get("branch", "")) <= 128
            and isinstance(changed, list) and len(changed) <= MAX_CHANGED
            and all(isinstance(p, str) and len(p) <= MAX_PATH for p in changed)
            and type(facts.get("changed_count", 0)) is int and 0 <= facts.get("changed_count", 0) < 10 ** 6
            and isinstance(facts.get("last_commit", ""), str) and len(facts.get("last_commit", "")) <= MAX_TEXT)


def label(card: dict) -> str:
    """'claude-code · DarkMatter@fix/wake-leak · MacBook-Pro' — readable, not an identity."""
    project = card.get("project") or "?"
    branch = (card.get("facts") or {}).get("branch")
    where = f"{project}@{branch}" if branch else project
    return " · ".join(part for part in (card.get("client") or "agent", where, card.get("host") or "") if part)


def matches(card: dict, match: dict) -> bool:
    """Every given field must loosely match: host, project, client, branch, or session id prefix."""
    fields = {"host": card.get("host"), "project": card.get("project"), "client": card.get("client"),
              "branch": (card.get("facts") or {}).get("branch"), "session": card.get("id")}
    for key, wanted in match.items():
        have = fields.get(key)
        if key == "session":
            names = [n for n in (have, card.get("session")) if isinstance(n, str)]
            if not (isinstance(wanted, str) and wanted and any(n.startswith(wanted) for n in names)):
                return False
        elif not have or not normalize(wanted) or normalize(wanted) not in normalize(have):
            return False
    return True


__all__ = ["host_name", "label", "matches", "normalize", "valid_facts", "workspace_facts"]
