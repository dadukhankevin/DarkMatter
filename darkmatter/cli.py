"""
Lightweight command dispatch for the darkmatter console entrypoint.
"""

from __future__ import annotations

import sys


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if cmd == "install-mcp":
        from darkmatter.installer import main as installer_main
        raise SystemExit(installer_main(sys.argv[2:]))

    # Auto-install MCP configs on first run
    _auto_install_if_needed()

    from darkmatter.app import main as app_main
    app_main()


def _auto_install_if_needed() -> None:
    """Run install-mcp --all on first ever start (sentinel check)."""
    from pathlib import Path

    sentinel = Path.home() / ".darkmatter" / ".mcp_installed"
    if sentinel.exists():
        return

    print("First run detected — installing MCP configs for all supported clients...")
    from darkmatter.installer import main as installer_main
    installer_main(["--all"])

    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("installed\n")
