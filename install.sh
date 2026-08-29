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
"$VENV_DIR/bin/pip" install --quiet --upgrade 'dmagent[solana]'

VERSION=$("$VENV_DIR/bin/python" -c "import darkmatter; print(darkmatter.__version__)")
echo "Installed dmagent $VERSION"

VENV_PYTHON="$VENV_DIR/bin/python"

# Prompt for display name (read from /dev/tty so curl|bash works)
echo ""
if [ -t 0 ]; then
    read -p "Agent display name [darkmatter-agent]: " DISPLAY_NAME
else
    read -p "Agent display name [darkmatter-agent]: " DISPLAY_NAME </dev/tty 2>/dev/null || true
fi
DISPLAY_NAME="${DISPLAY_NAME:-darkmatter-agent}"

echo ""
echo "Installing MCP config for supported clients..."
"$VENV_PYTHON" -m darkmatter install-mcp \
  --display-name "$DISPLAY_NAME" \
  --python "$VENV_PYTHON"

echo ""
echo "=== Setup complete ==="
echo "Display name: $DISPLAY_NAME"
echo "Version: $VERSION"
echo ""
echo "Restart your MCP client to connect. Auth is automatic."
echo ""
echo "To update later:  $VENV_DIR/bin/pip install --upgrade 'dmagent[solana]'"
