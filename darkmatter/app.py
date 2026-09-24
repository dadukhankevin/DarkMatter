"""v3 entrypoint — git mailbox + MCP stdio."""

import os
import sys
import threading
import time

import anyio
from mcp.server.stdio import stdio_server

from darkmatter.gitbox.mailbox import get_mailbox
from darkmatter.logging import get_logger
from darkmatter.mcp import mcp
import darkmatter.mcp.tools  # noqa: F401

_log = get_logger("app")


def start_space_worker(root=None):
    """Keep this checkout's repo space moving while any MCP session is open.

    Several MCP processes share one throttle, so the remote is polled about once
    per interval per device. Each poll is one ls-remote; changed branches are
    fetched and a push happens only when mail, receipts, or presence changed.
    DARKMATTER_SPACE_SYNC_SECONDS=0 disables it.
    """
    try:
        interval = float(os.environ.get("DARKMATTER_SPACE_SYNC_SECONDS", "15"))
    except ValueError:
        interval = 15.0
    if not interval > 0:
        return None
    interval = min(max(interval, 5.0), 3600.0)
    root = root or os.environ.get("DARKMATTER_PROJECT_DIR") or os.getcwd()

    def loop():
        from darkmatter.collaboration_cli import repo_space
        while True:
            try:
                space = repo_space(root)
                if space is not None:
                    result = space.sync_if_due(interval)
                    if result and result["errors"]:
                        _log.debug("repo space sync errors: %s", result["errors"])
            except Exception as exc:  # Background transport must never take down the server.
                _log.debug("repo space sync failed: %s", exc)
            time.sleep(interval)

    worker = threading.Thread(target=loop, name="darkmatter-space-sync", daemon=True)
    worker.start()
    return worker


def start_presence_heartbeat(interval: float = 60.0):
    from darkmatter.mcp.tools import heartbeat_served_sessions

    def loop():
        while True:
            time.sleep(interval)
            heartbeat_served_sessions()

    worker = threading.Thread(target=loop, name="darkmatter-presence", daemon=True)
    worker.start()
    return worker


async def run_stdio() -> None:
    get_mailbox()
    start_space_worker()
    start_presence_heartbeat()
    if os.environ.get("DARKMATTER_NETWORK_MODE") != "off":
        from darkmatter.network import start_background
        start_background()
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
    from darkmatter.one import onboarding
    first_contact = onboarding(mb)
    if first_contact and first_contact.get("recommended"):
        _log.info("First contact: connect to DarkMatter One with `darkmatter onboard connect`")


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
