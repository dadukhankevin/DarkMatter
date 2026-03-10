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

    from darkmatter.app import main as app_main
    app_main()
