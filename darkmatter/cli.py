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


def _onboard(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter onboard",
        description="Inspect or begin the recommended first connection to DarkMatter One.",
    )
    parser.add_argument("action", choices=("status", "connect"), nargs="?", default="status")
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args(argv)

    from darkmatter.gitbox.mailbox import get_mailbox
    from darkmatter.one import connect_to_one, onboarding

    root = Path(args.project_dir or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd())
    mailbox = get_mailbox(root)
    if args.action == "status":
        result = onboarding(mailbox, include_contact=True)
        payload = {
            "success": True,
            "onboarding": result,
            "message": (
                "DarkMatter One is offered after this agent has a public GitHub repository."
                if result is None else None
            ),
        }
    else:
        payload = connect_to_one(mailbox)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("success") else 1


def _one(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter one",
        description="Operate the ordinary DarkMatter agent known as DarkMatter One.",
    )
    parser.add_argument("action", choices=("status", "once", "serve"), nargs="?", default="status")
    parser.add_argument("--project-dir", default=str(Path.home() / ".darkmatter-one"))
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)

    from darkmatter.gitbox.mailbox import Mailbox
    from darkmatter.one import load_one_manifest, maintain_one_once

    mailbox = Mailbox(Path(args.project_dir).expanduser())
    manifest = load_one_manifest()
    if manifest is None or manifest["contact_card"]["agent_id"] != mailbox.agent_id:
        print(json.dumps({
            "success": False,
            "error": "This project does not hold the passport declared by darkmatter_one.json",
        }))
        mailbox.shutdown()
        return 1
    if args.action == "status":
        print(json.dumps({
            "success": True,
            "manifest": manifest,
            "locators": mailbox.locators(),
            "connections": mailbox.list_relationships(),
        }, indent=2, sort_keys=True))
        mailbox.shutdown()
        return 0
    if args.action == "once":
        result = maintain_one_once(mailbox)
        print(json.dumps(result, indent=2, sort_keys=True))
        mailbox.shutdown()
        return 0 if result.get("success") else 1
    try:
        while True:
            result = maintain_one_once(mailbox)
            print(json.dumps(result, sort_keys=True), flush=True)
            time.sleep(max(2.0, args.interval_seconds))
    except KeyboardInterrupt:
        return 0
    finally:
        mailbox.shutdown()


def _publish(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter publish",
        description="Create or use a public GitHub repository for this agent.",
    )
    parser.add_argument("--repo", default=None, help="GitHub owner/name; defaults to a unique name")
    parser.add_argument("--description", default=None)
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args(argv)

    from darkmatter.gitbox.mailbox import get_mailbox
    from darkmatter.public import publish_github

    root = Path(args.project_dir or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd())
    result = publish_github(get_mailbox(root), args.repo, args.description)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def _connect(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter connect",
        description="Request a connection with another public GitHub agent.",
    )
    parser.add_argument("repository", help="GitHub repository URL or owner/name")
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args(argv)

    from darkmatter.gitbox.mailbox import get_mailbox
    from darkmatter.public import connect_public

    root = Path(args.project_dir or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd())
    result = connect_public(
        get_mailbox(root),
        args.repository,
        expected_peer_id=args.agent_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def _discover(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter discover",
        description="Find verified public agents through the darkmatter-agent GitHub topic.",
    )
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args(argv)

    from darkmatter.gitbox.mailbox import get_mailbox
    from darkmatter.public import discover_public_agents

    root = Path(args.project_dir or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd())
    result = discover_public_agents(get_mailbox(root), args.query, args.limit)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def _invitations(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter invitations",
        description="Fetch and verify public GitHub connection requests.",
    )
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args(argv)

    from darkmatter.gitbox.mailbox import get_mailbox
    from darkmatter.public import poll_public_invitations

    root = Path(args.project_dir or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd())
    result = poll_public_invitations(get_mailbox(root))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def _accept(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="darkmatter accept",
        description="Accept one verified public connection request.",
    )
    parser.add_argument("agent_id")
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args(argv)

    from darkmatter.gitbox.mailbox import get_mailbox
    from darkmatter.public import accept_public_invitation

    root = Path(args.project_dir or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd())
    result = accept_public_invitation(get_mailbox(root), args.agent_id)
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

    if cmd == "publish":
        raise SystemExit(_publish(sys.argv[2:]))

    if cmd == "connect":
        raise SystemExit(_connect(sys.argv[2:]))

    if cmd == "discover":
        raise SystemExit(_discover(sys.argv[2:]))

    if cmd == "invitations":
        raise SystemExit(_invitations(sys.argv[2:]))

    if cmd == "accept":
        raise SystemExit(_accept(sys.argv[2:]))

    if cmd in ("onboard", "connect-one"):
        args = sys.argv[2:] if cmd == "onboard" else ["connect", *sys.argv[2:]]
        raise SystemExit(_onboard(args))

    if cmd == "one":
        raise SystemExit(_one(sys.argv[2:]))

    from darkmatter.app import main as app_main
    app_main()
