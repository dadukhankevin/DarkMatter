"""
Lightweight command dispatch for the darkmatter console entrypoint.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if cmd == "install-mcp":
        from darkmatter.installer import main as installer_main
        raise SystemExit(installer_main(sys.argv[2:]))

    if cmd == "wait-hook":
        raise SystemExit(_wait_hook(sys.argv[2:]))

    from darkmatter.app import main as app_main
    app_main()
