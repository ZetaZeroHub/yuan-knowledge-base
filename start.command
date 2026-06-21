#!/usr/bin/env bash
# start.command — Launch Yuan Knowledge Base workspace console (macOS double-click)
# Starts a local static file server and opens paper-ui in the default browser.

set -euo pipefail
cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"

# EVOLVEKB_PORT / EVOLVEKB_AGENT: legacy compatibility env vars, kept as-is
PORT="${EVOLVEKB_PORT:-8741}"

check_python() {
    if command -v python3 &>/dev/null; then
        echo "python3"
    elif command -v python &>/dev/null; then
        echo "python"
    else
        echo ""
    fi
}

PYTHON="$(check_python)"
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found. Please install Python 3.8+ to use this workspace."
    exit 1
fi

# Kill any existing server on this port
lsof -ti:"$PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true

AGENT="${EVOLVEKB_AGENT:-codex}"
CONSOLE_URL="http://localhost:$PORT/paper-ui/index.html"

echo "Starting Yuan Knowledge Base server on http://localhost:$PORT ..."
echo ""
echo "Yuan Knowledge Base is starting..."
echo "  Console:  $CONSOLE_URL"
echo "  Server:   http://localhost:$PORT"
echo "  Agent:    $AGENT (set EVOLVEKB_AGENT=claude to switch)"  # EVOLVEKB_AGENT: legacy compatibility
echo "  Stop:     Ctrl+C or close this window"
echo ""

# Open browser after server has time to bind the port
(
    sleep 2
    if [[ "$OSTYPE" == "darwin"* ]]; then
        open "$CONSOLE_URL"
    elif command -v xdg-open &>/dev/null; then
        xdg-open "$CONSOLE_URL"
    else
        echo "Open manually: $CONSOLE_URL"
    fi
) &

# Run server in FOREGROUND — keeps the terminal alive.
exec $PYTHON -u "$SCRIPT_DIR/server.py" --port "$PORT" --bind 127.0.0.1 --agent "$AGENT"
