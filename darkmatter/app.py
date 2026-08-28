"""v3 entrypoint — git mailbox + MCP stdio."""

import os
import sys

import anyio
from mcp.server.stdio import stdio_server

from darkmatter.gitbox.mailbox import get_mailbox
from darkmatter.logging import get_logger
from darkmatter.mcp import mcp
import darkmatter.mcp.tools  # noqa: F401

_log = get_logger("app")


async def run_stdio() -> None:
    get_mailbox()
    async with stdio_server() as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


def print_status() -> None:
    mb = get_mailbox()
    loc = mb.locators()
    _log.info("DarkMatter 3 — git mailbox")
    _log.info("Agent: %s", mb.store.profile.get("display_name"))
    _log.info("ID: %s...%s", mb.agent_id[:16], mb.agent_id[-8:])
    _log.info("Visibility: %s", loc["visibility"])
    _log.info("Locator: %s", loc["primary"])
    if loc["lan"]:
        _log.info("LAN: %s", loc["lan"])
    if loc["internet"]:
        _log.info("Internet: %s", loc["internet"])


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd == "install-mcp":
        from darkmatter.installer import main as installer_main
        raise SystemExit(installer_main(sys.argv[2:]))

    transport = os.environ.get("DARKMATTER_TRANSPORT", "auto")
    use_stdio = transport == "stdio" or (transport == "auto" and not sys.stdin.isatty())
    if use_stdio:
        anyio.run(run_stdio)
    else:
        print_status()


if __name__ == "__main__":
    main()
