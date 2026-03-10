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

    if cmd == "init-entrypoint":
        from darkmatter.entrypoint_init import init_entrypoint
        init_entrypoint(sys.argv[2:])
        return

    if cmd == "open-entrypoint":
        from darkmatter.entrypoint_init import open_entrypoint
        open_entrypoint()
        return

    if cmd == "keepalive":
        subcmd = sys.argv[2] if len(sys.argv) > 2 else None
        client = "claude-code"
        # Parse --client flag
        for i, arg in enumerate(sys.argv[3:], 3):
            if arg == "--client" and i + 1 < len(sys.argv):
                client = sys.argv[i + 1]

        if subcmd == "install":
            from darkmatter.installer import install_keepalive
            ok, msg = install_keepalive(client=client)
            print(msg)
            raise SystemExit(0 if ok else 1)
        elif subcmd == "uninstall":
            from darkmatter.installer import uninstall_keepalive
            ok, msg = uninstall_keepalive(client=client)
            print(msg)
            raise SystemExit(0 if ok else 1)
        else:
            print("Usage: darkmatter keepalive [install|uninstall] [--client claude-code]")
            raise SystemExit(1)

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
