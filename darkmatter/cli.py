"""
Lightweight command dispatch for the darkmatter console entrypoint.
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "install-mcp":
        from darkmatter.installer import main as installer_main
        raise SystemExit(installer_main(sys.argv[2:]))

    from darkmatter.app import main as app_main
    app_main()
