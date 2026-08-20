# Interview board — setup for macOS

A live interview canvas Claude can draw on while you're in voice mode. You talk,
and documents, boxes and diagrams appear in a window you keep beside the Claude
app. You type and drag on it, and Claude can read what you did.

It also keeps a layer the candidate never sees — your notes, a rubric, a model
answer you reveal when you're ready — so one board serves both of you. Boards
are saved on your Mac so you can reopen them later.

This guide assumes you have never used Terminal. It takes about ten minutes,
most of which is waiting.

---

## Two ways to run it

**Local — recommended.** Claude Desktop starts the board itself. No tunnel, no
public URL, nothing to re-paste, ever. One command to set up:

```
bash install.sh
```

Then quit and reopen Claude Desktop, and just talk. Everything below about
tunnels and connectors stops applying — jump to [Local setup](#local-setup).

The catch: it only works in **Claude Desktop on this Mac**, because Claude starts
the server as a program on your machine. Nothing on the web or on your phone can
reach it.

**Tunnel.** Puts the board on a public https address so Claude reaches it from
anywhere. You need this if the candidate is joining from their own machine, or if
you use voice mode on your phone. It costs you a fresh URL to paste into Claude's
connector settings every time you restart it. That's steps 1–8 below.

Both can coexist. Install the local one for everyday use and run `bash run.sh`
on the days someone needs to join remotely.

---

## Local setup

```
bash install.sh
```

That installs what it needs, mints your two tokens, and adds one entry to
Claude Desktop's config file (your previous config is backed up next to it
first). Then:

1. **Quit Claude Desktop completely** — Cmd + Q, not just closing the window.
2. Open it again. Settings → Local MCP servers should list **interview-board**.
3. Start talking. The board window opens by itself the first time Claude draws
   on it, so opening Claude doesn't put a window on screen you didn't ask for.

There is no URL to copy and no token to paste. The board page is served on
`127.0.0.1` for the window's benefit only, and Claude talks to the server over
the pipe it opened when it started the program.

To undo it: `bash install.sh --remove`, then restart Claude Desktop.

### Opening the board yourself

The window appears on its own the first time Claude draws on the board. To pull
it up before that — to set up before a candidate joins, or just to look at
yesterday's board — **double-click `open_board.command`** in Finder, or:

```
bash open_board.command
```

It finds whichever board is already running and opens a window on it. If nothing
is running it starts one first. `--share` opens the candidate's view instead, and
`--print` just prints the URL if you'd rather paste it into a browser.

### One board, however many Claudes

Claude Desktop starts a copy of the server for each surface that uses it — the
chat and a Claude Code session get one each — and `bash run.sh` may be running
too. Whichever one starts first owns the board; the rest detect it and hand their
tool calls over to it.

So there is only ever one board and one window, no matter how many places you
talk to Claude from. If you ever suspect you're looking at a stale one,
`bash open_board.command --print` tells you which port actually holds it.


**Sharing with a candidate on another machine** still needs a public address —
run `bash run.sh` for that session and hand them the share link it prints.

## The tunnel route

Only needed for remote candidates or phone voice mode.

## Step 1 · Put the folder somewhere permanent

Unzip `interview-board.zip` (double-click it in Finder). Drag the resulting
`interview-board` folder somewhere you won't clean out — your Documents folder
is fine. **Not Downloads**, because your saved boards live inside this folder.

## Step 2 · Open Terminal

Press **Cmd + Space**, type `terminal`, press **Enter**.

A window opens with a line of text and a blinking cursor. This is where every
command below gets typed. Type it, then press Enter.

## Step 3 · Point Terminal at the folder

Type this, **including the trailing space**, but don't press Enter yet:

```
cd 
```

Now drag the `interview-board` folder from Finder and drop it onto the Terminal
window. It pastes the full path for you. *Now* press Enter.

That's the trick that makes Terminal bearable — you never have to type a file
path by hand.

To check it worked, type `ls` and press Enter. You should see `board_mcp.py`,
`board.html`, `run.sh` and a couple of others listed.

## Step 4 · Install the tunnel tool

Claude can't reach your Mac directly, so you need a program that gives your
board a temporary public web address. Type:

```
brew install cloudflared
```

If you get `command not found: brew`, install Homebrew first by pasting this,
then run the line above again:

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Homebrew's installer will ask for your Mac password. It won't show anything as
you type it — that's normal, just type it and press Enter.

## Step 5 · Start it

```
bash run.sh
```

The first time, this takes a minute or two while it installs what it needs. When
it's ready you'll see a box like this:

```
  ── Interview board ────────────────────────────────────

  1. Your board opens in its own window. Park it beside Claude.

  2. In Claude: Customize > Connectors > + > Add custom connector
     Paste this whole line as the URL. Leave everything else blank:

     https://something-random.trycloudflare.com/mcp?t=HOST-TOKEN

  3. Share this with the candidate — canvas only, no notes, no tools:

     https://something-random.trycloudflare.com/?t=SHARED-TOKEN

  Boards save to ./boards   ·   Ctrl+C to stop
  ───────────────────────────────────────────────────────
```

**Leave this Terminal window open.** Closing it stops the board. To stop it
deliberately, click the window and press **Ctrl + C**.

Note the two different links. They are not interchangeable — see
[Two tokens](#two-tokens) below.

## Step 6 · Your board window

The board opens by itself in a plain window with no browser chrome, so you can
park it beside the Claude app and stay in voice mode without tab-switching.

Top right it should say **live** with a green dot, and **interviewer view** in a
gold pill — that pill is how you know you're looking at the view with your
private notes on it, not the one you shared.

To keep it above other windows, start with `BOARD_ON_TOP=1 bash run.sh`. To go
back to an ordinary browser tab instead, `bash run.sh --no-app`.

## Step 7 · Tell Claude about it

In Claude: **Customize → Connectors → + → Add custom connector**.

- **Name**: Interview board
- **URL**: the whole `https://...trycloudflare.com/mcp?t=...` line from **box 2**,
  including the `?t=` part on the end — that's your password
- **Advanced settings**: leave blank. Client ID and secret are for OAuth
  servers; this one doesn't use OAuth.

Save it, then make sure it's toggled on in your conversation.

> The token can also go in an `Authorization: Bearer ...` header if your version
> of Claude offers a request-headers field. Both work identically. The URL
> method is used here because it needs nothing but a paste.

> **The tunnel address changes every time you run `run.sh`.** A free Cloudflare
> quick tunnel gets a new random hostname on each start, so you have to update
> the connector URL each session. If that gets old, a named Cloudflare tunnel
> gives you a fixed address.

## Step 8 · Use it

Switch to voice mode and say something like:

> Run a mock interview using my board connector. Start a new board named after
> the problem, and never dictate code out loud — write it to the board. Keep a
> model answer on the private layer so I can reveal it at the end, and take
> private notes as we go. When I say I've written something, read the board and
> critique it rather than solving it for me.

Ask it to write something and watch the window.

---

## Every session after the first

1. Open Terminal, press **Cmd + ↑** to bring back your last `cd` command, Enter.
2. `bash run.sh`
3. The window opens itself; **re-paste the new connector URL into Claude.**

Step 3 is annoying but unavoidable: the free tunnel gives you a different
address every time. Your tokens never change, so only the domain part differs.
If you use this often, a Cloudflare named tunnel or a cheap VPS makes the
address permanent and step 3 goes away.

---

## When something goes wrong

**Claude Desktop doesn't list the server** — you have to quit it fully with
Cmd + Q and reopen; closing the window isn't enough. If it's still missing, check
Settings → Local MCP servers → Edit Config and look for an `interview-board`
entry under `mcpServers`.

**The board window never appears** — it opens on Claude's first *use* of the
board, not at launch. Say "put a note on the board" and it should show up, or
double-click `open_board.command` to pull it up yourself.

**Claude says it wrote something but your window didn't change** — you may be
looking at a board from before the last Claude Desktop restart. Quit Claude
Desktop (Cmd + Q), reopen, and run `bash open_board.command` for a window on the
current one.

**`command not found: brew`** — Homebrew isn't installed. See step 4.

**Board says "reconnecting"** — either the URL is missing its `?t=...`, or the
Terminal window got closed. Check the Terminal.

**Claude says the connector failed** — usually the tunnel address changed when
you restarted; copy the new line from the Terminal box into the connector's URL.
If it fails immediately with an authorisation error, the `?t=...` part got
dropped from the end of the URL.

**Claude says it wrote something but the board is blank** — the window isn't
connected. Close and reopen it. Claude is told when nobody's watching, so you
can also just ask "is anyone viewing the board?"

**You can't see something Claude says it wrote** — it may be on the private
layer, which only your window renders. Ask "is that private?" or look for the
gold dashed border.

**The candidate says they can't see something** — check whether it's private.
Ask Claude to make it shared, or click the ◐ button on the node.

**Nothing works and you want to check the board itself is fine** — in a second
Terminal tab (Cmd + T), `cd` to the folder again and run:

```
BOARD_HOST_TOKEN=$(cat .token.host) BOARD_TOKEN=$(cat .token) ./venv/bin/python smoke_test.py
```

That drives every feature without Claude involved and prints `PASS` at the end.
If that passes, the problem is the connector setup, not the board.

---

## Saving and reopening boards

Everything autosaves to the `boards` folder inside `interview-board`, one file
per board, updated a couple of seconds after each change.

**By voice:**

| You say | What happens |
|---|---|
| "Start a new board called Merge Intervals" | Saves the current one, opens a fresh one |
| "What boards do I have?" | Lists them, newest first |
| "Open my two sum board" | Reopens it — partial names are fine |
| "Save this as Two Sum attempt two" | Forks it under a new name |
| "Delete the LRU cache board" | Asks you to confirm first |

If a name matches two boards, Claude reads you the options instead of guessing.

**In the window:** the header has the board name (click to rename), a dropdown
of everything saved, and a New button.

---

## Reference

### What's on the board

One canvas you pan and zoom, holding four kinds of thing:

| | |
|---|---|
| **Documents** | Code or prose, syntax-coloured, editable in place. Several can sit side by side — a brief, the attempt, a rewrite |
| **Boxes** | Labelled rectangles, ellipses or diamonds for system-design sketching |
| **Labels** | Bare text dropped anywhere |
| **Diagrams** | A Mermaid diagram rendered as one node |

Lines connect any two nodes and follow them when either is dragged.

Toolbar keys: **V** select · **B** box · **T** text · **D** doc · **L** line.
Scroll to pan, pinch or ⌘-scroll to zoom, drag a node's header to move it,
drag the corner to resize, Backspace to delete the selection. Click a line to
remove it. **Tidy** reflows everything onto a grid.

### Two tokens

The interviewer's view and the shared view are separate, enforced by the server
rather than by the page:

| | Shared link `/?t=…` | Your link `/host?t=…` |
|---|---|---|
| The canvas | yes | yes |
| Private nodes | never sent | yes |
| Private notes and rubric | never sent | yes |
| The MCP endpoint | refused | yes |

Give the candidate the shared link. The private layer is stripped from their
snapshot before it reaches them, so there is nothing on their machine to reveal
by poking at the page, and their token cannot drive Claude's tools either.

Any node can be moved across the line — the ◐ button on a document, or ask
Claude. Flipping a model answer to shared is how you reveal it at the end.

### The tools Claude gets

| Tool | Effect |
|---|---|
| `board_add_doc` | Puts a document on the canvas, optionally private |
| `board_patch_doc` | Replaces one snippet in place, so the text doesn't jump |
| `board_add_box` | One labelled box |
| `board_add_text` | One floating label |
| `board_draw_diagram` | Renders a Mermaid diagram as a node |
| `board_connect` | Draws a line between two nodes |
| `board_update` | Changes a node's text, title, language, or which layer it's on |
| `board_move` | Repositions a node |
| `board_remove` | Removes a node or a line |
| `board_arrange` | Reflows everything onto a grid |
| `board_note` | One line in your private notes |
| `board_rubric` | Sets where the candidate stands on one dimension |
| `board_read` | Everything on the board, including what you typed and dragged |
| `board_clear` | Empties the canvas, same board |
| `board_new` | Saves current, starts a fresh named board |
| `board_save` | Save now, or save-as |
| `board_list` | Every saved board |
| `board_load` | Reopen a saved board |
| `board_delete` | Delete one, confirmation required |

### How live it is

Claude → board is instant: a tool call pushes over the websocket before the call
returns, so the canvas updates mid-sentence. Nodes Claude touches pulse once so
your eye can find them.

Board → Claude is on demand: your typing and dragging sync to the server
continuously, but Claude only sees them when it calls `board_read`. In practice
that's invisible — you say "take a look" and it reads first. What you can't get
is Claude reacting to your typing unprompted; nothing can interrupt a
conversation from outside.

Each node's border is coloured by who touched it last: cool for Claude, warm for
you. Private nodes are dashed and gold.

### Files

| File | What it is |
|---|---|
| `install.sh` | Registers the board with Claude Desktop, so there's no tunnel |
| `open_board.command` | Double-click to open the board window on demand |
| `reset.sh` | Stops everything and re-registers cleanly when it gets confused |
| `run.sh` | Starts everything with a public tunnel, for remote candidates |
| `board_mcp.py` | The server — MCP endpoint, board state, websocket, disk store |
| `board.html` | The canvas |
| `smoke_test.py` | Tests everything without Claude, including the private layer |
| `boards/` | Your saved boards |
| `.token` | The shared password, safe to hand a candidate |
| `.token.host` | Yours. Opens the private layer and the tools. Don't share it |

### Settings

Set these before `bash run.sh` if you need to:

| Variable | Default | Purpose |
|---|---|---|
| `BOARD_TOKEN` | generated into `.token` | Shared secret |
| `BOARD_HOST_TOKEN` | generated into `.token.host` | Interviewer secret |
| `BOARD_ON_TOP` | unset | `1` floats the window above everything else |
| `BOARD_DIR` | `./boards` | Where boards save |
| `BOARD_PORT` | `8765` | Port |

`bash run.sh --no-tunnel` skips the tunnel and works locally only.
`bash run.sh --no-app` keeps it in a browser tab instead of its own window.
`./venv/bin/python board_mcp.py --stdio` is what Claude Desktop runs; you
shouldn't need to call it yourself.

### One security note

The tunnel address is public, so anyone with the link could reach your Mac's
board — which is why every route requires a token. Don't paste either token into
a chat, an issue, or a screenshot.

`.token.host` is the one that matters: it opens your private notes and lets
anything holding it drive Claude's tools against your board. If you think either
leaked, delete that file, restart, and re-add the connector with the new value.

Boards never leave your machine — they're JSON files in `boards/`, including the
private layer.
