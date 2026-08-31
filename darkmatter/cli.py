"""
Lightweight command dispatch for the darkmatter console entrypoint.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _wait_hook(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter wait-hook",
        description="Wait for DarkMatter mail and signal a host hook when it arrives.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args(argv)

    hook_input: dict = {}
    if not sys.stdin.isatty():
        try:
            parsed = json.loads(sys.stdin.read() or "{}")
            if isinstance(parsed, dict):
                hook_input = parsed
        except json.JSONDecodeError:
            pass

    root = Path(
        os.environ.get("DARKMATTER_PROJECT_DIR")
        or hook_input.get("cwd")
        or os.getcwd()
    )
    session_id = str(hook_input.get("session_id") or "default")

    from darkmatter.gitbox.mailbox import get_mailbox
    from darkmatter.wakeup import format_wake_message, wait_for_messages_sync, wake_lease

    with wake_lease(root, session_id) as acquired:
        if not acquired:
            return 0
        messages = wait_for_messages_sync(
            get_mailbox(root),
            timeout_seconds=args.timeout_seconds,
        )
    if not messages:
        return 0
    print(format_wake_message(messages), file=sys.stderr)
    return 2


def _maintain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter maintain",
        description="Keep a mailbox synchronized, live, and recover interrupted routes.",
    )
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    parser.add_argument("--interval-seconds", type=float, default=30)
    parser.add_argument("--presence-interval-seconds", type=float, default=86400)
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args(argv)
    if args.interval_seconds < 2:
        parser.error("--interval-seconds must be at least 2")
    if args.presence_interval_seconds < 60:
        parser.error("--presence-interval-seconds must be at least 60")

    from darkmatter.gitbox.mailbox import get_mailbox

    root = Path(args.project_dir or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd())
    mailbox = get_mailbox(root)
    try:
        while True:
            result = mailbox.maintain_once(args.presence_interval_seconds)
            print(json.dumps(result, sort_keys=True), flush=True)
            if args.once:
                return 0 if result.get("success") else 1
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        mailbox.shutdown()


def _audit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter audit",
        description="Verify raw AntiMatter evidence without computing a score.",
    )
    parser.add_argument("--peer-id", default=None)
    parser.add_argument("--include-proofs", action="store_true")
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args(argv)

    from darkmatter.gitbox.mailbox import get_mailbox

    root = Path(args.project_dir or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd())
    result = get_mailbox(root).audit(args.peer_id, args.include_proofs)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if cmd == "install-mcp":
        from darkmatter.installer import main as installer_main
        raise SystemExit(installer_main(sys.argv[2:]))

    if cmd == "wait-hook":
        raise SystemExit(_wait_hook(sys.argv[2:]))

    if cmd == "maintain":
        raise SystemExit(_maintain(sys.argv[2:]))

    if cmd == "audit":
        raise SystemExit(_audit(sys.argv[2:]))

    from darkmatter.app import main as app_main
    app_main()
