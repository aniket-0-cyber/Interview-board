#!/usr/bin/env bash
#
# Registers the board as a local MCP server in Claude Desktop, so Claude starts
# it for you and there is no tunnel, no public URL, and nothing to re-paste.
#
#   bash install.sh
#
# Undo it from Claude's own settings, or with:  bash install.sh --remove

set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd -P)"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
NAME="interview-board"

if [ ! -d "$(dirname "$CONFIG")" ]; then
  echo "Claude Desktop isn't installed, or has never been opened."
  echo "Install it from claude.ai/download, open it once, then run this again."
  exit 1
fi

if [ ! -d venv ]; then
  echo "Setting up, this takes a minute..."
  python3 -m venv venv || exit 1
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q mcp uvicorn starlette websockets pywebview || exit 1
elif ! ./venv/bin/python -c "import webview" 2>/dev/null; then
  ./venv/bin/pip install -q pywebview
fi

# Minted here rather than at launch so the board's own URL never changes.
[ -f .token ] || ./venv/bin/python -c "import secrets;print(secrets.token_urlsafe(24))" > .token
[ -f .token.host ] || ./venv/bin/python -c "import secrets;print(secrets.token_urlsafe(24))" > .token.host

REMOVE=0
[ "${1:-}" = "--remove" ] && REMOVE=1

BACKUP="$CONFIG.backup-$(date +%Y%m%d-%H%M%S)"
[ -f "$CONFIG" ] && cp "$CONFIG" "$BACKUP"

HERE="$HERE" NAME="$NAME" CONFIG="$CONFIG" REMOVE="$REMOVE" ./venv/bin/python <<'PYTHON'
import json, os
from pathlib import Path

config = Path(os.environ["CONFIG"])
name, here = os.environ["NAME"], os.environ["HERE"]

try:
    data = json.loads(config.read_text())
except (OSError, json.JSONDecodeError):
    data = {}
if not isinstance(data, dict):
    data = {}

servers = data.setdefault("mcpServers", {})
if os.environ["REMOVE"] == "1":
    removed = servers.pop(name, None)
    print(f"  removed {name}" if removed else f"  {name} was not registered")
else:
    servers[name] = {
        "command": f"{here}/venv/bin/python",
        "args": [f"{here}/board_mcp.py", "--stdio"],
    }
    print(f"  registered {name}")

config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(json.dumps(data, indent=2) + "\n")
PYTHON

echo
if [ "$REMOVE" = "1" ]; then
  echo "  Quit Claude Desktop and reopen it to finish removing."
else
  echo "  ── Installed ───────────────────────────────────────────────────"
  echo
  echo "  1. Quit Claude Desktop completely (Cmd+Q) and open it again."
  echo "  2. Settings > Local MCP servers should now list: $NAME"
  echo "  3. Just talk. The board window opens the first time Claude"
  echo "     draws on it — no URL to paste, ever again."
  echo
  echo "  Sharing with someone on another machine still needs the tunnel:"
  echo "     bash run.sh"
  echo "  ────────────────────────────────────────────────────────────────"
fi
[ -f "$BACKUP" ] && echo && echo "  Previous config saved to:" && echo "  $BACKUP"
echo
