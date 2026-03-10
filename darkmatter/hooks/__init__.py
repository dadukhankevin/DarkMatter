"""
DarkMatter hooks for CLI agent lifecycle events.

Each CLI tool (Claude Code, Gemini CLI, Codex CLI, OpenCode) has its own
hook format. The core logic lives in keepalive.py; CLI-specific wrappers
parse their input format and call the shared logic.
"""

# Stop token — agents include this in their final message to exit the keep-alive loop.
STOP_TOKEN = "<dm:stop/>"
