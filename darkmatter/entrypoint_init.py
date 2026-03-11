"""
Init and open the DarkMatter entrypoint — a Claude Code session configured
as a mesh peer for human ↔ mesh interaction.

Commands:
    darkmatter init-entrypoint [--port PORT] [--display-name NAME]
    darkmatter open-entrypoint
"""

import argparse
import json
import os
import shutil
import sys


ENTRYPOINT_DIR = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint")

CLAUDE_MD_TEMPLATE = """\
# DarkMatter Mesh Entrypoint

You are a **mesh entrypoint** — a DarkMatter peer whose only job is interfacing
between a human and the agent mesh.

## Your Role
- You are a peer, not an orchestrator
- Route the human's messages to the best available peer
- Relay responses back to the human naturally
- If you pick the wrong peer, they'll forward it — the mesh self-corrects

## Startup
1. `darkmatter_update_bio(display_name="{display_name}", bio="Human interface to the mesh")`
2. `darkmatter_connection(action="connect", target_url="http://127.0.0.1:{dev_port}")` if not connected
3. `darkmatter_wait_for_message()` to start listening

## Message Routing
- Look at connected peers' bios and capabilities
- Send to whoever best matches the human's request
- Wait for their reply, relay it back
- Don't try to do development work yourself — route to the right agent

## Context Management
- Context is a sliding window — old entries drop off automatically
- Use `darkmatter_create_insight` to persist important knowledge across sessions
"""


def _detect_darkmatter_command() -> tuple[str, list[str]]:
    """Detect the best way to invoke darkmatter for the MCP config."""
    # Prefer the darkmatter binary if it exists in the venv
    venv_bin = os.path.join(os.path.expanduser("~"), ".darkmatter", "venv", "bin", "darkmatter")
    if os.path.isfile(venv_bin):
        return venv_bin, []

    # Fall back to python -m darkmatter
    return sys.executable, ["-m", "darkmatter"]


def init_entrypoint(argv: list[str] = None) -> None:
    """Initialize the entrypoint directory with .mcp.json and CLAUDE.md."""
    parser = argparse.ArgumentParser(description="Initialize DarkMatter entrypoint")
    parser.add_argument("--port", type=int, default=8200, help="Port for entrypoint node (default: 8200)")
    parser.add_argument("--display-name", default="Human-Entrypoint", help="Display name (default: Human-Entrypoint)")
    parser.add_argument("--dev-port", type=int, default=8100, help="Dev node port to connect to (default: 8100)")
    args = parser.parse_args(argv or [])

    ep_dir = ENTRYPOINT_DIR
    dm_dir = os.path.join(ep_dir, ".darkmatter")
    os.makedirs(dm_dir, exist_ok=True)

    # Write .mcp.json
    command, cmd_args = _detect_darkmatter_command()
    mcp_config = {
        "mcpServers": {
            "darkmatter": {
                "command": command,
                "args": cmd_args,
                "env": {
                    "DARKMATTER_PORT": str(args.port),
                    "DARKMATTER_DISPLAY_NAME": args.display_name,
                    "DARKMATTER_ROUTER_MODE": "queue_only",
                },
            }
        }
    }
    mcp_path = os.path.join(ep_dir, ".mcp.json")
    with open(mcp_path, "w") as f:
        json.dump(mcp_config, f, indent=2)
        f.write("\n")

    # Write CLAUDE.md
    claude_md = CLAUDE_MD_TEMPLATE.format(
        display_name=args.display_name,
        dev_port=args.dev_port,
    )
    claude_md_path = os.path.join(ep_dir, "CLAUDE.md")
    with open(claude_md_path, "w") as f:
        f.write(claude_md)

    # Auto-install keep-alive hook for Claude Code
    from darkmatter.installer import install_keepalive
    ok, msg = install_keepalive(client="claude-code", python_cmd=command)

    print(f"Entrypoint initialized at {ep_dir}")
    print(f"  .mcp.json: port {args.port}, display_name={args.display_name}")
    print(f"  CLAUDE.md: connects to dev node on port {args.dev_port}")
    if ok:
        print(f"  Keep-alive hook: installed")
    print(f"\nRun 'darkmatter open-entrypoint' to launch Claude Code there.")


def open_entrypoint() -> None:
    """Open Claude Code in the entrypoint directory."""
    ep_dir = ENTRYPOINT_DIR

    if not os.path.isfile(os.path.join(ep_dir, ".mcp.json")):
        print("Entrypoint not initialized. Running init first...")
        init_entrypoint([])

    # Check claude is available
    claude_path = shutil.which("claude")
    if not claude_path:
        print("Error: 'claude' command not found. Install Claude Code first.", file=sys.stderr)
        print("  curl -fsSL https://claude.ai/install.sh | bash", file=sys.stderr)
        sys.exit(1)

    print(f"Opening Claude Code in {ep_dir}...")
    os.chdir(ep_dir)
    os.execvp("claude", ["claude"])
