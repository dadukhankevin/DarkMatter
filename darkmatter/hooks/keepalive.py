"""
Keep-alive hook for DarkMatter mesh agents.

When an agent finishes responding, the CLI's stop/idle hook fires and
invokes this module. We check whether the agent explicitly opted out
(via <dm:stop/>) and if not, nudge it back into listening mode.

If the hook fires twice within COOLDOWN_SECONDS, the agent is allowed
to stop (it has nothing to do and the nudge isn't helping).

Currently supports: Claude Code (Stop hook), OpenCode (session.status / session.idle plugin).
Future: Gemini CLI (AfterAgent), Codex CLI (agent-turn-complete).

Usage (Claude Code):
    Hook command: python3 -m darkmatter.hooks.keepalive
    Reads JSON from stdin, writes JSON decision to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import time

from darkmatter.hooks import STOP_TOKEN

# If the hook fires twice within this window, let the agent stop.
COOLDOWN_SECONDS = 40

# Timestamp file — records when the hook last fired and blocked.
_STAMP_DIR = os.path.join(os.path.expanduser("~"), ".darkmatter", "keepalive")
os.makedirs(_STAMP_DIR, exist_ok=True)


def _stamp_path(session_id: str) -> str:
    """Per-session stamp file to track last hook fire."""
    safe_id = session_id.replace("/", "_") if session_id else "default"
    return os.path.join(_STAMP_DIR, f"{safe_id}.stamp")


def _check_cooldown(session_id: str) -> bool:
    """Return True if the hook fired recently (within COOLDOWN_SECONDS).

    Also updates the stamp to the current time.
    """
    path = _stamp_path(session_id)
    now = time.time()
    fired_recently = False

    try:
        if os.path.exists(path):
            last_fire = float(open(path).read().strip())
            if now - last_fire < COOLDOWN_SECONDS:
                fired_recently = True
    except (ValueError, OSError):
        pass

    # Update stamp
    try:
        with open(path, "w") as f:
            f.write(str(now))
    except OSError:
        pass

    return fired_recently


def _clear_stamp(session_id: str) -> None:
    """Remove the stamp file (called when we allow stop)."""
    try:
        os.remove(_stamp_path(session_id))
    except FileNotFoundError:
        pass


def check_should_keep_alive(
    last_message: str,
    stop_hook_active: bool,
    session_id: str = "",
) -> tuple[bool, str]:
    """Core keep-alive logic, CLI-agnostic.

    Returns (should_block, reason).
    If should_block is True, the agent should continue rather than stopping.
    """
    # Agent explicitly opted out — let it stop
    if STOP_TOKEN in last_message:
        _clear_stamp(session_id)
        return False, ""

    # If hook fired twice within cooldown window, let it stop
    if _check_cooldown(session_id):
        _clear_stamp(session_id)
        return False, ""

    # Nudge the agent back into listening mode
    reason = (
        "[reminder: consider creating insights for code you just explored]\n\n"
        "Call darkmatter_wait_for_message() to listen for incoming messages. "
        "Do not pass a short timeout — the default (1 hour) is intentional. "
        "When you truly need to exit, include <dm:stop/> in your final message."
    )
    return True, reason


def handle_claude_code_stop(hook_input: dict) -> dict | None:
    """Handle Claude Code's Stop hook event.

    Returns a JSON-serializable dict to write to stdout,
    or None to allow the stop (exit 0 with no output).
    """
    last_message = hook_input.get("last_assistant_message", "")
    stop_hook_active = hook_input.get("stop_hook_active", False)
    session_id = hook_input.get("session_id", "")

    should_block, reason = check_should_keep_alive(last_message, stop_hook_active, session_id)

    if not should_block:
        return None  # Allow stop

    return {"decision": "block", "reason": reason}


def main() -> None:
    """Entry point for Claude Code Stop hook (stdin JSON → stdout JSON)."""
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        # Can't parse input — fail open, let the agent stop
        sys.exit(0)

    result = handle_claude_code_stop(hook_input)

    if result is None:
        sys.exit(0)

    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
