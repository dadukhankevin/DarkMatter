"""Thin git subprocess helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from darkmatter.contract.contact import validate_locator
from darkmatter.store.local import atomic_write_text


class GitError(RuntimeError):
    pass


def git(cwd: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
             "-c", "protocol.ext.allow=never", *args],
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
    url = validate_locator(url)
    if is_git_url(url):
        return url
    return str(Path(url).expanduser().resolve())


def push_url(work: Path, url: str) -> None:
    """Push HEAD:main to any git URL (path, https, ssh)."""
    git(work, "push", resolve_remote(url), "HEAD:main")


def rev_parse(path: Path, ref: str = "HEAD") -> str:
    return git(path, "rev-parse", ref).stdout.strip()


def clone_or_update(remote: str, dest: Path) -> Path:
    remote = resolve_remote(remote)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or (dest / ".git").is_symlink():
        raise GitError("Peer cache must not be a symlink")
    if (dest / ".git").exists():
        git(dest, "remote", "set-url", "origin", remote)
        git(dest, "fetch", "origin")
        git(dest, "update-ref", "HEAD", "origin/main")
    else:
        if dest.exists():
            shutil.rmtree(dest)
        git(dest.parent, "clone", "--no-checkout", "--branch", "main", "--", remote, dest.name)
    _materialize_mail(dest)
    return dest


def _materialize_mail(dest: Path) -> None:
    """Read only bounded protocol blobs, never check out peer-controlled files.

    This bypasses .gitattributes smudge filters, symlinks, submodules and hooks.
    Git history is retained for hints; arbitrary repository code is never loaded.
    Network pack sizes still require operator/host resource controls.
    """
    tree = git(dest, "ls-tree", "-r", "-l", "-z", "HEAD", "--",
               "agent.json", "commitment.json", "outbox", "readbox", "antimatter").stdout
    blobs = []
    total = 0
    for record in tree.split("\0"):
        if not record:
            continue
        header, name = record.split("\t", 1)
        mode, kind, oid, size = header.split()
        parts = Path(name).parts
        allowed = name in ("agent.json", "commitment.json") or (
            len(parts) == 2 and parts[0] in ("outbox", "readbox", "antimatter") and parts[1].endswith(".json"))
        if not allowed or mode not in ("100644", "100755") or kind != "blob":
            continue
        limit = 8 * 1024 * 1024 if parts[0] == "antimatter" else 1024 * 1024
        size = int(size)
        if size > limit:
            raise GitError("Peer protocol file exceeds its size limit")
        total += size
        if len(blobs) >= 4096 or total > 32 * 1024 * 1024:
            raise GitError("Peer mailbox exceeds the materialization budget")
        blobs.append((name, oid, size))
    contents = []
    if blobs:
        try:
            result = subprocess.run(
                ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "cat-file", "--batch"],
                cwd=str(dest), input="".join(oid + "\n" for _, oid, _ in blobs).encode(),
                capture_output=True, timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError("Peer blob read timed out") from exc
        if result.returncode:
            raise GitError("Could not read peer protocol blobs")
        offset = 0
        for name, oid, size in blobs:
            end = result.stdout.find(b"\n", offset)
            if result.stdout[offset:end] != f"{oid} blob {size}".encode():
                raise GitError("Unexpected Git blob response")
            offset = end + 1
            try:
                contents.append((name, result.stdout[offset:offset + size].decode("utf-8")))
            except UnicodeDecodeError as exc:
                raise GitError("Peer protocol JSON must be UTF-8") from exc
            offset += size + 1
    for name in ("agent.json", "commitment.json", "outbox", "readbox", "antimatter"):
        path = dest / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    for name, content in contents:
        atomic_write_text(dest / name, content)
