#!/usr/bin/env bash
#
# Clean slate. Stops every board process, re-registers the server with Claude
# Desktop, and tells you what's left to do by hand.
#
#   bash reset.sh                 stop everything, re-register
#   bash reset.sh --new-tokens    also mint fresh tokens (invalidates old links)

set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd -P)"

echo
echo "  ── Resetting the interview board ───────────────────────────────"
echo

# 1 · stop anything running out of this folder, and only this folder
echo "  Stopping board processes..."
STOPPED=0
for pid in $(pgrep -f "board_mcp.py" 2>/dev/null); do
  kill "$pid" 2>/dev/null && STOPPED=$((STOPPED + 1))
done
for pid in $(pgrep -f "cloudflared tunnel --url http://127.0.0.1" 2>/dev/null); do
  kill "$pid" 2>/dev/null && STOPPED=$((STOPPED + 1))
done
sleep 2
for pid in $(pgrep -f "board_mcp.py" 2>/dev/null); do
  kill -9 "$pid" 2>/dev/null
done
echo "    stopped $STOPPED"

STILL="$(pgrep -f "board_mcp.py" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$STILL" != "0" ]; then
  echo "    $STILL wouldn't die — Claude Desktop restarts them; quit it (Cmd+Q) first"
fi

# 2 · optionally start over with fresh secrets
if [ "${1:-}" = "--new-tokens" ]; then
  echo "  Minting new tokens..."
  rm -f .token .token.host
  ./venv/bin/python -c "import secrets;print(secrets.token_urlsafe(24))" > .token
  ./venv/bin/python -c "import secrets;print(secrets.token_urlsafe(24))" > .token.host
  echo "    done — any old link or connector URL is now dead"
fi

# 3 · re-register cleanly: drop the entry, then add it back
echo "  Re-registering with Claude Desktop..."
bash install.sh --remove >/dev/null 2>&1
bash install.sh >/dev/null 2>&1 || { echo "    install.sh failed — run it on its own to see why"; exit 1; }
echo "    registered"

echo
echo "  ── Now do these two things ─────────────────────────────────────"
echo
echo "  1. In Claude, delete any OLD custom connector for this board."
echo "     Settings > Connectors — remove anything pointing at a"
echo "     trycloudflare.com address. Those tunnels are dead and every"
echo "     call to them fails."
echo
echo "  2. Quit Claude Desktop completely (Cmd+Q) and open it again."
echo "     Settings > Local MCP servers should list: interview-board"
echo
echo "  Then just talk to it. To see the board yourself at any time:"
echo "     bash open_board.command"
echo "  ────────────────────────────────────────────────────────────────"
echo
