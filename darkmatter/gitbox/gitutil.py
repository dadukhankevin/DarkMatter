"""Thin git subprocess helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(cwd: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError("git command timed out after 60 seconds") from exc
    if check and result.returncode != 0:
        raise GitError(result.stderr.strip() or result.stdout.strip() or "git failed")
    return result


def init_repo(path: Path, bare: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    cmd = ["init", "-b", "main"]
    if bare:
        cmd.append("--bare")
    git(path, *cmd)
    if not bare:
        git(path, "config", "user.email", "darkmatter@local")
        git(path, "config", "user.name", "DarkMatter")


def commit_all(path: Path, message: str) -> bool:
    git(path, "add", "-A")
    staged = git(path, "status", "--porcelain")
    if not staged.stdout.strip():
        return False
    git(path, "commit", "-m", message)
    return True


def ensure_origin(work: Path, bare: Path) -> None:
    remotes = git(work, "remote", check=False)
    if "origin" not in remotes.stdout.split():
        git(work, "remote", "add", "origin", str(bare.resolve()))
    git(work, "push", "-u", "origin", "HEAD")


def is_git_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "git@", "ssh://"))


def resolve_remote(url: str) -> str:
    """Path remotes become absolute; URL remotes stay as given."""
    if is_git_url(url):
        return url
    return str(Path(url).expanduser().resolve())


def push_url(work: Path, url: str) -> None:
    """Push HEAD:main to any git URL (path, https, ssh)."""
    git(work, "push", resolve_remote(url), "HEAD:main")


def rev_parse(path: Path, ref: str = "HEAD") -> str:
    return git(path, "rev-parse", ref).stdout.strip()


def clone_or_update(remote: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest / ".git").exists():
        git(dest, "remote", "set-url", "origin", remote)
        git(dest, "fetch", "origin")
        git(dest, "reset", "--hard", "origin/main", check=False)
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    git(dest.parent, "clone", "--branch", "main", remote, dest.name)
    return dest
