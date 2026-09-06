"""Portable CLI and lifecycle adapter for local session collaboration."""

import argparse
import json
import os
import shlex
import sqlite3
import sys

from darkmatter.collaboration import Collaboration


def execute(board, action, *, scope="workspace", objective=None, recipient=None,
            content=None, message_id=None, ids=None, resource=None, seconds=900):
    if action == "join":
        return {"success": True, "self": board.join(objective)}
    if action == "status":
        return board.status(scope)
    if action == "read":
        return board.read()
    if action == "send":
        return board.send(recipient, content, message_id)
    if action == "delivery":
        return board.delivery(message_id)
    if action == "ack":
        return board.ack(ids or [])
    if action == "claim":
        return board.claim(resource, seconds)
    if action == "release":
        return board.release(resource)
    if action == "leave":
        return board.leave()
    raise ValueError("Unknown collaboration action")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="darkmatter collaborate")
    parser.add_argument("action", choices=("join", "status", "read", "send", "delivery", "ack", "claim", "release", "leave", "hook"))
    parser.add_argument("--session", dest="session_id")
    parser.add_argument("--client")
    parser.add_argument("--project-dir", default=os.environ.get("DARKMATTER_PROJECT_DIR"))
    parser.add_argument("--scope", choices=("workspace", "repo", "device"), default="workspace")
    parser.add_argument("--objective")
    parser.add_argument("--recipient")
    parser.add_argument("--content")
    parser.add_argument("--message-id")
    parser.add_argument("--id", action="append", dest="ids")
    parser.add_argument("--resource")
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        if args.action == "hook":
            # Never inspect transcript paths, prompts, tool arguments, or outputs.
            raw = sys.stdin.read(65537)
            if len(raw) > 65536:
                return 0
            event = json.loads(raw or "{}")
            if not isinstance(event, dict):
                return 0
            cursor = args.client == "cursor"
            session_id = event.get("conversation_id") if cursor else event.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                return 0
            name = event.get("hook_event_name", "")
            cursor_names = {"sessionStart": "SessionStart", "postToolUse": "PostToolUse", "sessionEnd": "SessionEnd"}
            if cursor:
                name = cursor_names.get(name, "")
            if name not in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionEnd"):
                return 0
            if cursor:
                roots = event.get("workspace_roots")
                if not isinstance(roots, list) or not roots or not isinstance(roots[0], str) or not roots[0]:
                    return 0
                root = roots[0]
            else:
                root = event.get("cwd") or os.getcwd()
            board = Collaboration(args.project_dir or root, session_id, args.client)
            if name == "SessionEnd":
                board.leave()
                return 0
            note = board.notification(force=name in ("SessionStart", "UserPromptSubmit"))
            if note:
                note["cli_fallback"] = shlex.join([sys.executable, "-I", "-m", "darkmatter", "collaborate",
                                                  "status", "--scope", "repo", "--client", board.client, "--session", board.session_id])
                text = "DarkMatter local collaboration update (identifiers only):\n" + json.dumps(note, ensure_ascii=True)
                output = {"additional_context": text} if cursor else {"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}
                print(json.dumps(output))
            return 0
        board = Collaboration(args.project_dir or os.getcwd(), args.session_id, args.client)
        result = execute(board, args.action, scope=args.scope, objective=args.objective,
                         recipient=args.recipient, content=args.content, message_id=args.message_id,
                         ids=args.ids, resource=args.resource, seconds=args.seconds)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result.get("success") else 1
    except (ValueError, OSError, sqlite3.Error) as exc:
        if args.action == "hook":
            print(f"DarkMatter collaboration unavailable: {type(exc).__name__}", file=sys.stderr)
            return 0
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
