#!/bin/bash
set -eu

DM_DIR="$HOME/.darkmatter"
VENV_DIR="$DM_DIR/venv"
PYTHON_CMD=""

echo "=== DarkMatter Bootstrap ==="
echo ""

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

# Verify Python version is 3.10+
PY_VERSION=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
PY_MAJOR=$("$PYTHON_CMD" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
PY_MINOR=$("$PYTHON_CMD" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
if [ "$PY_MAJOR" -lt 3 ] 2>/dev/null || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; } 2>/dev/null; then
    echo "ERROR: Python 3.10+ required, found $PY_VERSION. Please upgrade."
    exit 1
fi
echo "Using $PYTHON_CMD ($PY_VERSION)"

# Create directory
mkdir -p "$DM_DIR"

# Create venv and install
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

echo "Installing DarkMatter..."
"$VENV_DIR/bin/pip" install --quiet --upgrade dmagent

VERSION=$("$VENV_DIR/bin/python" -c "import darkmatter; print(darkmatter.__version__)")
echo "Installed dmagent $VERSION"

# Find free port in 8100-8200
PORT=8100
while [ $PORT -le 8200 ]; do
    if command -v lsof >/dev/null 2>&1; then
        if ! lsof -i :$PORT >/dev/null 2>&1; then
            break
        fi
    else
        if ! "$PYTHON_CMD" -c "import socket; s=socket.socket(); s.settimeout(0.1); exit(0 if s.connect_ex(('127.0.0.1',$PORT)) else 1)" 2>/dev/null; then
            break
        fi
    fi
    PORT=$((PORT + 1))
done
if [ $PORT -gt 8200 ]; then
    echo "ERROR: No free ports in 8100-8200 range"
    exit 1
fi
echo "Using port $PORT"

VENV_PYTHON="$VENV_DIR/bin/python"

# Prompt for display name (read from /dev/tty so curl|bash works)
echo ""
if [ -t 0 ]; then
    read -p "Agent display name [darkmatter-agent]: " DISPLAY_NAME
else
    read -p "Agent display name [darkmatter-agent]: " DISPLAY_NAME </dev/tty 2>/dev/null || true
fi
DISPLAY_NAME="${DISPLAY_NAME:-darkmatter-agent}"

# Build the darkmatter MCP entry as JSON
DM_ENTRY=$(cat <<JSONEOF
{
  "command": "$VENV_PYTHON",
  "args": ["-m", "darkmatter"],
  "env": {
    "DARKMATTER_PORT": "$PORT",
    "DARKMATTER_DISPLAY_NAME": "$DISPLAY_NAME"
  }
}
JSONEOF
)

# Install into ALL known MCP config locations
INSTALLED=0

install_mcp_entry() {
    local target="$1"
    local target_dir
    target_dir=$(dirname "$target")
    mkdir -p "$target_dir"

    if [ -f "$target" ]; then
        MERGED=$(echo "$DM_ENTRY" | "$VENV_PYTHON" -c "
import json, sys
entry = json.load(sys.stdin)
with open('$target') as f:
    config = json.load(f)
config.setdefault('mcpServers', {})
config['mcpServers']['darkmatter'] = entry
print(json.dumps(config, indent=2))
")
        if [ $? -eq 0 ]; then
            printf '%s\n' "$MERGED" > "$target"
            echo "  Updated $target"
            INSTALLED=$((INSTALLED + 1))
        else
            echo "  WARNING: Failed to merge into $target"
        fi
    else
        echo "$DM_ENTRY" | "$VENV_PYTHON" -c "
import json, sys
entry = json.load(sys.stdin)
config = {'mcpServers': {'darkmatter': entry}}
with open('$target', 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
"
        echo "  Created $target"
        INSTALLED=$((INSTALLED + 1))
    fi
}

echo ""
echo "Installing MCP config (global — applies to all projects)..."
install_mcp_entry "$HOME/.claude.json"

echo ""
echo "=== Setup complete ==="
echo "DarkMatter installed to $INSTALLED location(s)"
echo "Display name: $DISPLAY_NAME"
echo "Port: $PORT"
echo "Version: $VERSION"
echo ""
echo "Restart your MCP client to connect. Auth is automatic."
echo ""
echo "To update later:  $VENV_DIR/bin/pip install --upgrade dmagent"
echo ""
echo "Alternative: standalone HTTP mode (manual start):"
echo "  DARKMATTER_PORT=$PORT nohup $VENV_PYTHON -m darkmatter > /tmp/darkmatter-$PORT.log 2>&1 &"
echo "  Then set in .mcp.json: {\"type\":\"http\",\"url\":\"http://localhost:$PORT/mcp\"}"
