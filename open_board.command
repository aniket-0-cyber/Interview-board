#!/usr/bin/env bash
#
# Opens the board window right now, without waiting for Claude to draw on it.
# Double-click this file in Finder, or run:  bash open_board.command
#
#   bash open_board.command --print   just prints the URL

set -uo pipefail
cd "$(dirname "$0")"
PORT="${BOARD_PORT:-8765}"

if [ ! -x ./venv/bin/python ]; then
  echo "Not set up yet. Run:  bash install.sh"
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi

[ -f .token ] || ./venv/bin/python -c "import secrets;print(secrets.token_urlsafe(24))" > .token
TOKEN="$(cat .token)"

PRINT=0
for arg in "$@"; do
  [ "$arg" = "--print" ] && PRINT=1
done

# Whichever process got there first owns the board — Claude Desktop's, a Claude
# Code session's, or a run.sh. Find it rather than starting a rival.
find_board() {
  for candidate in $(seq "$PORT" $((PORT + 19))); do
    if curl -s --max-time 1 "http://127.0.0.1:$candidate/health?t=$TOKEN" \
         | grep -q '"ok":true'; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

FOUND="$(find_board)"

if [ -z "$FOUND" ]; then
  echo "No board running — starting one..."
  BOARD_BANNER=0 nohup ./venv/bin/python board_mcp.py >/dev/null 2>&1 &
  disown 2>/dev/null
  for _ in $(seq 1 30); do
    FOUND="$(find_board)" && [ -n "$FOUND" ] && break
    sleep 0.4
  done
fi

if [ -z "$FOUND" ]; then
  echo "Couldn't start the board. Try:  bash run.sh --no-tunnel"
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi

URL="http://127.0.0.1:$FOUND/?t=$TOKEN"

if [ "$PRINT" = "1" ]; then
  echo "$URL"
  exit 0
fi

echo "Board is on port $FOUND — opening the window."
nohup ./venv/bin/python board_mcp.py --window "$URL" >/dev/null 2>&1 &
disown 2>/dev/null
sleep 1
