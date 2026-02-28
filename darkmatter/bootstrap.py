"""
Bootstrap routes — zero-friction node deployment.

Depends on: config
"""

import os

from starlette.requests import Request
from starlette.responses import Response


async def handle_bootstrap(request: Request) -> Response:
    """Return a shell script that bootstraps a new DarkMatter node."""
    from darkmatter.state import get_state
    state = get_state()
    host = request.headers.get("host", f"localhost:{state.port if state else 8100}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    source_url = f"{scheme}://{host}/bootstrap/server.py"

    script = f"""#!/bin/bash
set -e

echo "=== DarkMatter Bootstrap ==="
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

# Find free port in 8100-8110
PORT=8100
while [ $PORT -le 8110 ]; do
    if ! lsof -i :$PORT >/dev/null 2>&1; then
        break
    fi
    PORT=$((PORT + 1))
done
if [ $PORT -gt 8110 ]; then
    echo "ERROR: No free ports in 8100-8110 range"
    exit 1
fi
echo "Using port $PORT"

VENV_PYTHON="$VENV_DIR/bin/python"

echo ""
echo "=== DarkMatter installed ==="
echo ""
echo "NEXT STEPS (for the installing agent):"
echo ""
echo "1. Write .mcp.json in your project directory with this content"
echo "   (replace YOUR_NAME with a short name for this agent):"
echo ""
echo '{{"mcpServers":{{"darkmatter":{{"command":"'"$VENV_PYTHON"'","args":["'"$DM_DIR"'/server.py"],"env":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"YOUR_NAME"}}}}}}}}'
echo ""
echo "2. Ask the user to restart Claude Code (or their MCP client)"
echo "   so it picks up the new .mcp.json config."
echo ""
echo "3. After restart, call darkmatter_update_bio to describe your"
echo "   capabilities so other agents can find and route to you."
echo ""
echo "4. Call darkmatter_discover_local to find nearby agents,"
echo "   then darkmatter_connection(action='request') to connect."
echo ""
echo "Identity is automatic — a passport key is created on first run."
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
