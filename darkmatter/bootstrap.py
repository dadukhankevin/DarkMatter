"""
Bootstrap routes — zero-friction node deployment.

Depends on: config
"""

import os
import re

from starlette.requests import Request
from starlette.responses import Response

from darkmatter.config import CLIENT_PROFILES


async def handle_bootstrap(request: Request) -> Response:
    """Return a shell script that bootstraps a new DarkMatter node.

    Accepts ?client=<name> query param to tailor config output.
    Default: claude-code. Valid: claude-code, cursor, gemini, codex, kimi, opencode.
    """
    from darkmatter.state import get_state
    state = get_state()
    host = request.headers.get("host", f"localhost:{state.port if state else 8100}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    # Sanitize host/scheme to prevent shell injection in generated script
    if not re.match(r'^[\w.\-]+(:\d+)?$', host):
        host = f"localhost:{state.port if state else 8100}"
    if scheme not in ("http", "https"):
        scheme = "http"
    source_url = f"{scheme}://{host}/bootstrap/server.py"
    # Allowlist client against known profiles to prevent shell injection
    client = request.query_params.get("client", "claude-code")
    if client not in CLIENT_PROFILES:
        client = "claude-code"

    script = f"""#!/bin/bash
set -e

CLIENT="{client}"

echo "=== DarkMatter Bootstrap ==="
echo "Client: $CLIENT"
echo ""

DM_DIR="$HOME/.darkmatter"
VENV_DIR="$DM_DIR/venv"

# Find python3
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PYTHON_CMD="$cmd"
        break
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    echo "ERROR: Python not found. Install Python 3.10+ first."
    exit 1
fi
echo "Using $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"

# Create directory
mkdir -p "$DM_DIR"

# Download server
echo "Downloading server.py..."
curl -sS "{source_url}" -o "$DM_DIR/server.py"

# Create venv and install dependencies
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet "mcp[cli]" httpx uvicorn starlette cryptography anyio

# Find free port in 8100-8200
PORT=8100
while [ $PORT -le 8200 ]; do
    if ! lsof -i :$PORT >/dev/null 2>&1; then
        break
    fi
    PORT=$((PORT + 1))
done
if [ $PORT -gt 8200 ]; then
    echo "ERROR: No free ports in 8100-8200 range"
    exit 1
fi
echo "Using port $PORT"

VENV_PYTHON="$VENV_DIR/bin/python"
SERVER_PY="$DM_DIR/server.py"

echo ""
echo "=== DarkMatter installed ==="
echo ""
echo "NEXT STEPS (for the installing agent):"
echo ""

# Emit client-specific MCP config instructions
case "$CLIENT" in
    claude-code)
        echo "1. Write .mcp.json in your project directory:"
        echo ""
        echo '{{"mcpServers":{{"darkmatter":{{"command":"'"$VENV_PYTHON"'","args":["'"$SERVER_PY"'"],"env":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"YOUR_NAME","DARKMATTER_CLIENT":"claude-code"}}}}}}}}'
        ;;
    cursor)
        echo "1. Write .cursor/mcp.json in your project directory:"
        echo "   (create the .cursor/ directory if needed)"
        echo ""
        echo '{{"mcpServers":{{"darkmatter":{{"command":"'"$VENV_PYTHON"'","args":["'"$SERVER_PY"'"],"env":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"YOUR_NAME","DARKMATTER_CLIENT":"cursor"}}}}}}}}'
        ;;
    gemini)
        echo "1. Write .gemini/settings.json in your project directory:"
        echo "   (create the .gemini/ directory if needed)"
        echo ""
        echo '{{"mcpServers":{{"darkmatter":{{"command":"'"$VENV_PYTHON"'","args":["'"$SERVER_PY"'"],"env":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"YOUR_NAME","DARKMATTER_CLIENT":"gemini"}}}}}}}}'
        ;;
    codex)
        echo "1. Write .codex/config.toml in your project directory:"
        echo "   (create the .codex/ directory if needed)"
        echo ""
        echo "[mcp_servers.darkmatter]"
        echo 'command = "'"$VENV_PYTHON"'"'
        echo 'args = ["'"$SERVER_PY"'"]'
        echo ""
        echo '[mcp_servers.darkmatter.env]'
        echo 'DARKMATTER_PORT = "'$PORT'"'
        echo 'DARKMATTER_DISPLAY_NAME = "YOUR_NAME"'
        echo 'DARKMATTER_CLIENT = "codex"'
        ;;
    kimi)
        echo "1. Write .mcp.json in your project directory"
        echo "   (or add to ~/.kimi/mcp.json for global):"
        echo ""
        echo '{{"mcpServers":{{"darkmatter":{{"command":"'"$VENV_PYTHON"'","args":["'"$SERVER_PY"'"],"env":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"YOUR_NAME","DARKMATTER_CLIENT":"kimi"}}}}}}}}'
        ;;
    opencode)
        echo "1. Write opencode.json in your project directory:"
        echo ""
        echo '{{"mcp":{{"darkmatter":{{"type":"local","command":["'"$VENV_PYTHON"'","'"$SERVER_PY"'"],"environment":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"YOUR_NAME","DARKMATTER_CLIENT":"opencode"}}}}}}}}'
        ;;
    openclaw)
        echo "OpenClaw does not have native MCP client support."
        echo "DarkMatter connects via the darkmatter skill (HTTP API)."
        echo ""
        echo "1. Start the DarkMatter server:"
        echo "   DARKMATTER_PORT=$PORT DARKMATTER_DISPLAY_NAME=YOUR_NAME DARKMATTER_CLIENT=openclaw \\\\"
        echo "     $VENV_PYTHON $SERVER_PY &"
        echo ""
        echo "2. Install the DarkMatter skill:"
        echo "   Copy the skills/darkmatter/ folder into your project's skills/ directory,"
        echo "   or install from ClawHub: clawhub install darkmatter"
        echo ""
        echo "3. Restart OpenClaw so it picks up the new skill."
        echo "   The skill uses curl to talk to DarkMatter's HTTP API at http://localhost:$PORT"
        ;;
    *)
        echo "1. Unknown client '$CLIENT'. Using generic .mcp.json format:"
        echo ""
        echo '{{"mcpServers":{{"darkmatter":{{"command":"'"$VENV_PYTHON"'","args":["'"$SERVER_PY"'"],"env":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"YOUR_NAME","DARKMATTER_CLIENT":"'"$CLIENT"'"}}}}}}}}'
        ;;
esac

echo ""
echo "2. Ask the user to restart their MCP client so it picks up the config."
echo ""
echo "3. After restart, call darkmatter_update_bio to describe your"
echo "   capabilities so other agents can find and route to you."
echo ""
echo "4. Call darkmatter_discover_local to find nearby agents,"
echo "   then darkmatter_connection(action='request') to connect."
echo ""
echo "Identity is automatic — a passport key is created on first run."
echo ""
echo "Supported clients: claude-code, cursor, gemini, codex, kimi, opencode, openclaw"
echo "Re-run with ?client=<name> for a different config format:"
echo "  curl {scheme}://{host}/bootstrap?client=cursor | bash"
"""
    return Response(script, media_type="text/plain")


async def handle_bootstrap_source(request: Request) -> Response:
    """Serve raw server.py source code. No auth required."""
    server_path = os.path.abspath(__file__)
    # Walk up to find the original server.py (or serve this package)
    repo_dir = os.path.dirname(os.path.dirname(server_path))
    legacy_server = os.path.join(repo_dir, "server.py")
    if os.path.exists(legacy_server):
        with open(legacy_server, "r") as f:
            source = f.read()
    else:
        source = "# DarkMatter has been refactored into the darkmatter/ package.\\n"
    return Response(source, media_type="text/plain")
