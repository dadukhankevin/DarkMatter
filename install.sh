#!/bin/bash
set -e

echo "=== DarkMatter Bootstrap ==="
echo ""

GITHUB_RAW="https://raw.githubusercontent.com/dadukhankevin/DarkMatter/main/server.py"
DM_DIR="$HOME/.darkmatter"
VENV_DIR="$DM_DIR/venv"
PYTHON_CMD=""

# Find python3
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
curl -fsSL "$GITHUB_RAW" -o "$DM_DIR/server.py"

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
echo "=== Setup complete ==="
echo ""
echo "Add to your .mcp.json (stdio mode — auto-starts with your MCP client):"
echo '{"mcpServers":{"darkmatter":{"command":"'"$VENV_PYTHON"'","args":["'"$DM_DIR"'/server.py"],"env":{"DARKMATTER_PORT":"'"$PORT"'","DARKMATTER_DISPLAY_NAME":"your-agent-name"}}}}'
echo ""
echo "Then restart your MCP client. Auth is automatic — no setup needed."
echo ""
echo "Or for standalone HTTP mode (manual start):"
echo "  DARKMATTER_PORT=$PORT nohup $VENV_PYTHON $DM_DIR/server.py > /tmp/darkmatter-$PORT.log 2>&1 &"
echo "  .mcp.json: {\"mcpServers\":{\"darkmatter\":{\"type\":\"http\",\"url\":\"http://localhost:$PORT/mcp\"}}}"
