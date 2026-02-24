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

# Prompt for display name
echo ""
read -p "Agent display name [darkmatter-agent]: " DISPLAY_NAME
DISPLAY_NAME="${DISPLAY_NAME:-darkmatter-agent}"

# Build the darkmatter MCP entry as JSON
DM_ENTRY=$(cat <<JSONEOF
{
  "command": "$VENV_PYTHON",
  "args": ["$DM_DIR/server.py"],
  "env": {
    "DARKMATTER_PORT": "$PORT",
    "DARKMATTER_DISPLAY_NAME": "$DISPLAY_NAME"
  }
}
JSONEOF
)

# Find or create .mcp.json
MCP_JSON=""
for candidate in "$PWD/.mcp.json" "$HOME/.claude/.mcp.json"; do
    if [ -f "$candidate" ]; then
        MCP_JSON="$candidate"
        break
    fi
done
if [ -z "$MCP_JSON" ]; then
    MCP_JSON="$PWD/.mcp.json"
fi

# Merge darkmatter entry into .mcp.json
if [ -f "$MCP_JSON" ]; then
    # Check if python has json module (it always does)
    MERGED=$("$VENV_PYTHON" -c "
import json, sys
with open('$MCP_JSON') as f:
    config = json.load(f)
config.setdefault('mcpServers', {})
config['mcpServers']['darkmatter'] = json.loads('''$DM_ENTRY''')
print(json.dumps(config, indent=2))
")
    if [ $? -eq 0 ]; then
        echo "$MERGED" > "$MCP_JSON"
        echo "Updated existing $MCP_JSON"
    else
        echo "ERROR: Failed to merge into $MCP_JSON"
        exit 1
    fi
else
    # Create new .mcp.json
    "$VENV_PYTHON" -c "
import json
config = {'mcpServers': {'darkmatter': json.loads('''$DM_ENTRY''')}}
with open('$MCP_JSON', 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
"
    echo "Created $MCP_JSON"
fi

echo ""
echo "=== Setup complete ==="
echo "DarkMatter added to $MCP_JSON"
echo "Display name: $DISPLAY_NAME"
echo "Port: $PORT"
echo ""
echo "Restart your MCP client to connect. Auth is automatic."
echo ""
echo "Alternative: standalone HTTP mode (manual start):"
echo "  DARKMATTER_PORT=$PORT nohup $VENV_PYTHON $DM_DIR/server.py > /tmp/darkmatter-$PORT.log 2>&1 &"
echo "  Then set in .mcp.json: {\"type\":\"http\",\"url\":\"http://localhost:$PORT/mcp\"}"
