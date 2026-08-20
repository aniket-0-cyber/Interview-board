#!/usr/bin/env bash
#
# Puts the board on a public https address, for when the candidate is joining
# from their own machine. Run it with:   bash run.sh
#
# If Claude Desktop already has the board running (see install.sh) this attaches
# the tunnel to that one rather than starting a rival — same board, same window,
# now reachable from outside.
#
#   bash run.sh --no-app      keep it in the browser instead of its own window
#   bash run.sh --no-tunnel   local only, no public URL

set -uo pipefail
cd "$(dirname "$0")"
PORT="${BOARD_PORT:-8765}"

WANT_TUNNEL=1
WANT_APP=1
for arg in "$@"; do
  [ "$arg" = "--no-tunnel" ] && WANT_TUNNEL=0
  [ "$arg" = "--no-app" ] && WANT_APP=0
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 isn't installed. Run:  xcode-select --install"
  exit 1
fi

if [ ! -d venv ]; then
  echo "First-time setup, this takes a minute..."
  python3 -m venv venv || exit 1
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q mcp uvicorn starlette websockets || exit 1
fi

if [ "$WANT_APP" = "1" ] && ! ./venv/bin/python -c "import webview" 2>/dev/null; then
  echo "Adding the app window (one-off)..."
  ./venv/bin/pip install -q pywebview || WANT_APP=0
fi

# Two tokens. The shared one opens the board; the interviewer's one also opens
# the private view and the MCP endpoint, so the link you hand a candidate can't
# reach your notes or drive Claude's tools.
if [ ! -f .token ]; then
  ./venv/bin/python -c "import secrets;print(secrets.token_urlsafe(24))" > .token
fi
if [ ! -f .token.host ]; then
  ./venv/bin/python -c "import secrets;print(secrets.token_urlsafe(24))" > .token.host
fi
export BOARD_TOKEN="$(cat .token)"
export BOARD_HOST_TOKEN="$(cat .token.host)"

# --- is a board already running? -----------------------------------------
# Claude Desktop starts one when it launches. Binding the same port would just
# fail, and starting a second board on another port would leave Claude writing
# to one while you watch the other.
find_board() {
  for candidate in $(seq "$PORT" $((PORT + 19))); do
    if curl -s --max-time 1 "http://127.0.0.1:$candidate/health?t=$BOARD_HOST_TOKEN" \
         | grep -q '"ok":true'; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}
OWNER="$(find_board)"
LIVE_PORT="${OWNER:-$PORT}"

# --- optional: bring up the public tunnel ourselves -----------------------
TUNNEL_URL=""
if [ "$WANT_TUNNEL" = "1" ] && command -v cloudflared >/dev/null 2>&1; then
  LOG="$(mktemp)"
  cloudflared tunnel --url "http://127.0.0.1:$LIVE_PORT" > "$LOG" 2>&1 &
  CF_PID=$!
  trap 'kill $CF_PID 2>/dev/null' EXIT
  printf "Opening tunnel"
  for _ in $(seq 1 30); do
    TUNNEL_URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1)"
    [ -n "$TUNNEL_URL" ] && break
    printf "."
    sleep 1
  done
  echo
fi

echo
echo "  ── Interview board ─────────────────────────────────────────────"
echo
if [ -n "$OWNER" ]; then
  echo "  Attached to the board already running on port $OWNER."
  echo "  Ctrl+C stops the tunnel and leaves that board alone."
else
  echo "  Started a board on port $PORT."
fi
echo
if [ "$WANT_APP" = "1" ]; then
  echo "  1. Your board opens in its own window. Park it beside Claude."
else
  echo "  1. Open your board (this view has your private notes on it):"
  echo "     http://127.0.0.1:$LIVE_PORT/host?t=$BOARD_HOST_TOKEN"
fi
echo
if [ -n "$TUNNEL_URL" ]; then
  echo "  2. In Claude: Customize > Connectors > + > Add custom connector"
  echo "     Paste this whole line as the URL. Leave everything else blank:"
  echo
  echo "     $TUNNEL_URL/mcp?t=$BOARD_HOST_TOKEN"
  echo
  echo "  3. Share this with the candidate — canvas only, no notes, no tools:"
  echo
  echo "     $TUNNEL_URL/?t=$BOARD_TOKEN"
elif [ "$WANT_TUNNEL" = "0" ]; then
  echo "  2. No tunnel this run, so Claude can only reach the board if you"
  echo "     registered it locally:  bash install.sh"
  echo
elif ! command -v cloudflared >/dev/null 2>&1; then
  echo "  2. To connect Claude from elsewhere you need a public URL:"
  echo "        brew install cloudflared"
  echo "     then quit this (Ctrl+C) and run  bash run.sh  again."
  echo
else
  echo "  2. The tunnel didn't come up in time. Quit (Ctrl+C) and try again."
  echo
  echo "  3. Share this with the candidate on your network:"
  echo "     http://127.0.0.1:$LIVE_PORT/?t=$BOARD_TOKEN"
fi
echo
echo "  Boards save to ./boards   ·   Ctrl+C to stop"
echo "  ────────────────────────────────────────────────────────────────"
echo

open_window() {
  [ "$WANT_APP" = "1" ] || return 0
  nohup ./venv/bin/python board_mcp.py --window \
    "http://127.0.0.1:$LIVE_PORT/host?t=$BOARD_HOST_TOKEN" >/dev/null 2>&1 &
  disown 2>/dev/null
}

if [ -n "$OWNER" ]; then
  # Somebody else owns the board; we're only here to hold the tunnel open.
  open_window
  if [ -n "${CF_PID:-}" ]; then
    wait "$CF_PID"
  else
    echo "  Nothing to do — the board is already running and you asked for no tunnel."
  fi
elif [ "$WANT_APP" = "1" ]; then
  BOARD_BANNER=0 ./venv/bin/python board_mcp.py --app
else
  BOARD_BANNER=0 ./venv/bin/python board_mcp.py
fi
