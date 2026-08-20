"""
board_mcp — a live interview whiteboard Claude can drive from voice mode.

One process serves three things:
  GET  /        the board page you keep open on a second screen
  WS   /ws      live push (Claude -> page) and live sync (page -> Claude)
  POST /mcp     the MCP endpoint you register as a custom connector

Boards are named, autosaved to ./boards as JSON, and can be reloaded later.

Run:  python board_mcp.py
"""

import asyncio
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional
from urllib.parse import parse_qs

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

HOST = os.environ.get("BOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("BOARD_PORT", "8765"))
PAGE = Path(__file__).parent / "board.html"
BOARDS_DIR = Path(os.environ.get("BOARD_DIR", Path(__file__).parent / "boards"))
MAX_NOTES = 50
MAX_NODES = 120
AUTOSAVE_SECONDS = 2.0

def _token(filename: str, variable: str) -> str:
    """Tokens have to survive restarts, or the board URL changes every launch and
    nothing you bookmarked still works. Environment first, then the file beside
    this script, and only then a freshly minted one — which gets written down."""
    supplied = os.environ.get(variable, "").strip()
    if supplied:
        return supplied
    path = Path(__file__).parent / filename
    try:
        stored = path.read_text().strip()
        if stored:
            return stored
    except OSError:
        pass
    minted = secrets.token_urlsafe(24)
    try:
        path.write_text(minted + "\n")
    except OSError:
        pass
    return minted


# Anything not on your machine must present this.
TOKEN = _token(".token", "BOARD_TOKEN")

# The interviewer's own token. It opens /host and /mcp on top of the board
# itself, where the shared token opens only the board. Two tokens is what makes
# the private layer real rather than something the page politely declines to
# render: the candidate's link cannot reach the private data or the tools.
HOST_TOKEN = _token(".token.host", "BOARD_HOST_TOKEN")

# Set once the stdio server knows which port it landed on. While it is None the
# board is being served some other way and nothing here should open a window.
WINDOW_URL: Optional[str] = None
_window_proc: Optional[subprocess.Popen] = None

mcp = MCPServer("board_mcp")


# --------------------------------------------------------------------------
# Disk store
# --------------------------------------------------------------------------

def slugify(name: str) -> str:
    """'Two Sum — attempt 2' -> 'two-sum-attempt-2'. Filename-safe by construction."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "untitled"


class Store:
    """Boards on disk, one JSON file each."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, slug: str) -> Path:
        # slugify again on the way in so a crafted slug can never escape the dir
        return self.root / f"{slugify(slug)}.json"

    def save(self, data: Dict[str, Any]) -> Path:
        path = self._path(data["slug"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)  # atomic, so a crash mid-write can't corrupt a board
        return path

    def load(self, slug: str) -> Optional[Dict[str, Any]]:
        path = self._path(slug)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def delete(self, slug: str) -> bool:
        path = self._path(slug)
        if path.exists():
            path.unlink()
            return True
        return False

    def index(self) -> List[Dict[str, Any]]:
        """Every saved board, newest first, without loading full contents."""
        out: List[Dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            out.append({
                "slug": data.get("slug", path.stem),
                "name": data.get("name", path.stem),
                "nodes": len(data.get("nodes") or {}),
                "notes": len(data.get("notes") or []),
                "updated": data.get("updated", ""),
            })
        return sorted(out, key=lambda b: b["updated"], reverse=True)

    def resolve(self, query: str) -> List[str]:
        """Match a spoken name loosely: exact slug, then substring on name or slug."""
        wanted = slugify(query)
        entries = self.index()
        exact = [b["slug"] for b in entries if b["slug"] == wanted]
        if exact:
            return exact
        return [b["slug"] for b in entries
                if wanted in b["slug"] or wanted in slugify(b["name"])]


store = Store(BOARDS_DIR)


# --------------------------------------------------------------------------
# Live board state
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Footprint per node type, in board pixels. When Claude doesn't name coordinates
# the board finds a free slot itself, so a voice-driven call never has to do
# layout arithmetic mid-sentence.
NODE_SIZES: Dict[str, tuple] = {
    "doc":     (460, 320),
    "diagram": (440, 300),
    "box":     (190, 96),
    "text":    (220, 44),
}
NODE_TYPES = tuple(NODE_SIZES)
GRID_X, GRID_STEP, MARGIN, GUTTER = 500, 60, 40, 24


class Board:
    """The board on screen: positioned nodes and the edges between them, plus
    websocket fan-out and autosave.

    A node marked private belongs to the interviewer alone. It is stripped from
    the shared projection before it ever reaches the candidate's socket, and any
    edit naming it from a non-host connection is refused, so the private layer is
    a property of the server rather than something the page is trusted to hide."""

    def __init__(self) -> None:
        self._clients: Dict[WebSocket, bool] = {}      # socket -> is_host
        self._lock = asyncio.Lock()
        self._dirty = False
        self._saver: Optional[asyncio.Task] = None
        self.reset("Untitled board")

    # ---- contents ----

    def reset(self, name: str) -> None:
        self.name = name
        self.slug = slugify(name)
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Dict[str, Dict[str, Any]] = {}
        self.notes: List[Dict[str, str]] = []          # host-only
        self.rubric: List[Dict[str, str]] = []         # host-only
        self.seq = 0
        self.rev = 0
        self.last_editor = "claude"

    # ---- nodes and edges ----

    def _fresh_id(self) -> str:
        """Random rather than sequential: consecutive ids would let the candidate
        infer how many private nodes are hidden from the gaps in what they see."""
        while True:
            ident = secrets.token_hex(3)
            if ident not in self.nodes and ident not in self.edges:
                return ident

    def place(self, w: int, h: int) -> tuple:
        """First free slot on a loose grid, scanning down each column in turn."""
        taken = [(n["x"], n["y"], n["w"], n["h"]) for n in self.nodes.values()]
        for col in range(12):
            x = MARGIN + col * GRID_X
            for row in range(48):
                y = MARGIN + row * GRID_STEP
                if not any(x < tx + tw + GUTTER and tx < x + w + GUTTER
                           and y < ty + th + GUTTER and ty < y + h + GUTTER
                           for tx, ty, tw, th in taken):
                    return x, y
        return MARGIN, MARGIN

    def add_node(self, kind: str, **fields: Any) -> Dict[str, Any]:
        w, h = NODE_SIZES[kind]
        x, y = fields.pop("x", None), fields.pop("y", None)
        if x is None or y is None:
            x, y = self.place(w, h)
        self.seq += 1
        node = {
            "id": self._fresh_id(), "type": kind, "seq": self.seq,
            "x": int(x), "y": int(y), "w": int(fields.pop("w", w)), "h": int(fields.pop("h", h)),
            "title": "", "text": "", "language": "python", "shape": "rect",
            "private": False, "author": "claude",
        }
        node.update({k: v for k, v in fields.items() if v is not None})
        self.nodes[node["id"]] = node
        return node

    def remove_node(self, ident: str) -> bool:
        if ident not in self.nodes:
            return False
        del self.nodes[ident]
        for edge_id in [e for e, v in self.edges.items() if ident in (v["from"], v["to"])]:
            del self.edges[edge_id]
        return True

    def connect(self, src: str, dst: str, label: str = "", dashed: bool = False) -> Dict[str, Any]:
        edge = {"id": self._fresh_id(), "from": src, "to": dst,
                "label": label, "dashed": dashed, "author": "claude"}
        self.edges[edge["id"]] = edge
        return edge

    def arrange(self) -> None:
        """Reflow every node onto the grid in creation order, keeping edges."""
        order = sorted(self.nodes.values(), key=lambda n: n["seq"])
        for node in order:
            node["x"], node["y"] = -100_000, -100_000     # park it out of the way
        for node in order:
            node["x"], node["y"] = self.place(node["w"], node["h"])

    def readable(self, ident: str, host: bool) -> bool:
        node = self.nodes.get(ident)
        return bool(node) and (host or not node.get("private"))

    # ---- persistence and projection ----

    def adopt(self, data: Dict[str, Any]) -> None:
        """Replace live contents with a board loaded from disk."""
        self.name = data.get("name", "Untitled board")
        self.slug = data.get("slug", slugify(self.name))
        self.nodes = {i: dict(n) for i, n in (data.get("nodes") or {}).items()}
        self.edges = {i: dict(e) for i, e in (data.get("edges") or {}).items()}
        self.notes = [dict(n) for n in (data.get("notes") or [])][-MAX_NOTES:]
        self.rubric = [dict(r) for r in (data.get("rubric") or [])]
        self.seq = max([n.get("seq", 0) for n in self.nodes.values()] or [0])
        self.rev = int(data.get("rev", 0))
        self.last_editor = "claude"
        if not self.nodes:
            self._migrate_panes(data)

    def _migrate_panes(self, data: Dict[str, Any]) -> None:
        """Boards written by the three-pane version keep opening: their code and
        diagram become the first two nodes on the canvas."""
        code = data.get("code") or {}
        diagram = data.get("diagram") or {}
        if (code.get("source") or "").strip():
            self.add_node("doc", title=code.get("title") or "", text=code["source"],
                          language=code.get("language") or "python")
        if (diagram.get("mermaid") or "").strip():
            self.add_node("diagram", title=diagram.get("title") or "", text=diagram["mermaid"])
        for line in data.get("notes") or []:
            if isinstance(line, str):
                self.notes.append({"text": line, "at": now_iso(), "author": "claude"})

    def document(self) -> Dict[str, Any]:
        """The persisted form. Private nodes and notes are saved: the file on disk
        is the interviewer's copy, and never leaves the machine."""
        return {
            "slug": self.slug,
            "name": self.name,
            "nodes": {i: dict(n) for i, n in self.nodes.items()},
            "edges": {i: dict(e) for i, e in self.edges.items()},
            "notes": [dict(n) for n in self.notes],
            "rubric": [dict(r) for r in self.rubric],
            "rev": self.rev,
            "updated": now_iso(),
        }

    def snapshot(self, host: bool = True) -> Dict[str, Any]:
        """The form sent to a page. `host` decides whether the private layer is in
        it at all — the guest projection never carries the data, so there is
        nothing for a candidate to reveal by poking at the page."""
        nodes = {i: {k: v for k, v in n.items() if k != "seq"}
                 for i, n in self.nodes.items() if host or not n.get("private")}
        edges = {i: dict(e) for i, e in self.edges.items()
                 if e["from"] in nodes and e["to"] in nodes}
        doc: Dict[str, Any] = {
            "slug": self.slug, "name": self.name, "rev": self.rev,
            "nodes": nodes, "edges": edges,
            "last_editor": self.last_editor, "updated": now_iso(),
            "host": host, "saved": store.index(),
        }
        if host:
            doc["notes"] = [dict(n) for n in self.notes]
            doc["rubric"] = [dict(r) for r in self.rubric]
        return doc

    # ---- plumbing ----

    async def add_client(self, ws: WebSocket, host: bool) -> None:
        self._clients[ws] = host
        await ws.send_text(json.dumps({"type": "state", "board": self.snapshot(host)}))

    def drop_client(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    async def commit(self, editor: str) -> Dict[str, Any]:
        """Bump the revision, push the right projection to every open page, queue
        an autosave."""
        async with self._lock:
            self.rev += 1
            self.last_editor = editor
            self._dirty = True
            views = {True: self.snapshot(True), False: self.snapshot(False)}
        for ws, is_host in list(self._clients.items()):
            try:
                await ws.send_text(json.dumps(
                    {"type": "state", "board": views[is_host], "origin": editor}))
            except Exception:
                self.drop_client(ws)
        self._ensure_saver()
        return views[True]

    def _ensure_saver(self) -> None:
        if self._saver is None or self._saver.done():
            self._saver = asyncio.ensure_future(self._autosave_loop())

    async def _autosave_loop(self) -> None:
        """Coalesce rapid edits into one write every couple of seconds."""
        while True:
            await asyncio.sleep(AUTOSAVE_SECONDS)
            if not self._dirty:
                return
            self._dirty = False
            try:
                store.save(self.document())
            except OSError:
                self._dirty = True  # try again next tick

    def flush(self) -> Path:
        self._dirty = False
        return store.save(self.document())

    @property
    def viewers(self) -> int:
        return len(self._clients)

    @property
    def watchers(self) -> int:
        """Shared views only — the interviewer's own window doesn't count as an
        audience, so Claude can still be told nobody can see the board."""
        return sum(1 for is_host in self._clients.values() if not is_host)


board = Board()


def _ensure_window() -> None:
    """Open the board window the first time Claude actually touches the board.

    Claude Desktop starts its local servers when the app launches, and a window
    appearing then would be a window nobody asked for. Waiting for the first tool
    call means it shows up exactly when you start an interview.

    It is a separate process on purpose: pywebview wants the main thread, and in
    stdio mode this process's main thread belongs to the MCP loop. Tracking that
    process rather than a flag means closing the window isn't permanent — the next
    thing Claude draws brings it back."""
    global _window_proc
    if not WINDOW_URL:
        return
    if _window_proc is not None and _window_proc.poll() is None:
        return                       # one is already up
    try:
        _window_proc = subprocess.Popen([sys.executable, __file__, "--window", WINDOW_URL],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _ack(action: str, state: Dict[str, Any], **extra: Any) -> str:
    """Uniform tool result: cheap to read, says whether anyone is watching."""
    _ensure_window()
    payload = {
        "ok": True,
        "action": action,
        "board": state["name"],
        "rev": state["rev"],
        "viewers": board.viewers,
        "shared_viewers": board.watchers,
    }
    if board.viewers == 0:
        payload["note"] = "No window has the board open, so nobody can see this."
    elif board.watchers == 0:
        payload["note"] = "Only the interviewer's window is open; the shared view has no one in it."
    payload.update(extra)
    return json.dumps(payload)


def _fail(error: str, hint: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": error, "hint": hint, **extra})


# --------------------------------------------------------------------------
# Tool inputs
# --------------------------------------------------------------------------
#
# Every tool takes its fields directly rather than nesting them under a single
# model. A wrapper object reads fine in a schema but is a trap in practice: a
# tool whose fields all have defaults looks callable with no arguments at all,
# the wrapper goes missing, and the whole call dies on validation — which the
# client reports as a bare "tool call failed" with nothing to act on.

class Language(str, Enum):
    python = "python"
    javascript = "javascript"
    typescript = "typescript"
    java = "java"
    cpp = "cpp"
    go = "go"
    rust = "rust"
    sql = "sql"
    markdown = "markdown"
    text = "text"


class ResponseFormat(str, Enum):
    markdown = "markdown"
    json = "json"


class Shape(str, Enum):
    rect = "rect"
    ellipse = "ellipse"
    diamond = "diamond"


class Verdict(str, Enum):
    strong = "strong"
    mixed = "mixed"
    weak = "weak"
    unset = "unset"


# Shared field shapes, so the same idea reads the same way in every signature.
XPos = Annotated[Optional[int], Field(description="Left edge in board pixels. Omit and the board finds a free spot.")]
YPos = Annotated[Optional[int], Field(description="Top edge in board pixels. Omit and the board finds a free spot.")]
Private = Annotated[bool, Field(description="True keeps it on the interviewer's view. The candidate never receives it.")]
NodeId = Annotated[str, Field(description="Node id, from board_read or from the call that created it.",
                              min_length=1, max_length=32)]


# --------------------------------------------------------------------------
# Tools: the canvas
# --------------------------------------------------------------------------

def _latest(kind: str) -> Optional[Dict[str, Any]]:
    matches = [n for n in board.nodes.values() if n["type"] == kind]
    return max(matches, key=lambda n: n["seq"]) if matches else None


def _missing(ident: str) -> str:
    return _fail(f"No node or line with id {ident!r} is on the board.",
                 "Call board_read with response_format=json to see the current ids.")


@mcp.tool(
    name="board_add_doc",
    annotations=ToolAnnotations(title="Put a document on the board", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_add_doc(
    text: Annotated[str, Field(description="Contents of the document: source code, or prose when language is markdown.",
                               max_length=20000)],
    language: Annotated[Language, Field(description="Governs syntax colouring. Use markdown for prose.")] = Language.python,
    title: Annotated[Optional[str], Field(description="Heading on the node, e.g. 'Two Sum — optimal'.",
                                          max_length=120)] = None,
    private: Private = False,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Drop a document onto the canvas: source code, or prose when language is
    markdown. It sits wherever you put it and the user can drag, resize and type
    into it — there is no fixed code pane any more, so a board can hold several
    documents side by side (a brief, an attempt, a rewrite).

    Set private=true for something only the interviewer should see: a model answer
    to compare against, the hints you are holding back, a follow-up you plan to ask.
    The candidate's view never receives it.

    For small edits mid-discussion prefer board_patch_doc, so the user's eye doesn't
    lose its place.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}. Keep the id — you
    need it for board_patch_doc, board_connect and board_move.
    """
    node = board.add_node("doc", text=text, language=language.value,
                          title=title, private=private, x=x, y=y)
    return _ack("add_doc", await board.commit("claude"), id=node["id"], private=node["private"])


@mcp.tool(
    name="board_patch_doc",
    annotations=ToolAnnotations(title="Edit a document in place", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_patch_doc(
    find: Annotated[str, Field(description="Exact text to replace. Must appear exactly once in that document.",
                               min_length=1, max_length=4000)],
    replace: Annotated[str, Field(description="Replacement text. Empty string deletes the match.",
                                  max_length=8000)],
    id: Annotated[Optional[str], Field(description="Which document. Omit to patch the one most recently written.",
                                       max_length=32)] = None,
) -> str:
    """Find-and-replace a single unique snippet inside one document.

    Cheaper and far less jarring than rewriting the whole node. Fails loudly when
    `find` is missing or ambiguous, so you never silently patch the wrong line.

    Returns: JSON {ok, action, id, board, rev, viewers} or {ok: false, error, hint}.
    """
    if id:
        node = board.nodes.get(id)
        if node is None:
            return _missing(id)
        if node["type"] != "doc":
            return _fail(f"Node {id!r} is a {node['type']}, not a document.",
                         "board_patch_doc only edits documents. Use board_update for other nodes.")
    else:
        node = _latest("doc")
        if node is None:
            return _fail("There are no documents on the board yet.", "Call board_add_doc first.")

    hits = node["text"].count(find)
    if hits == 0:
        return _fail("No match for `find` in that document.",
                     "Call board_read to see the current text, or replace it with board_update.",
                     id=node["id"])
    if hits > 1:
        return _fail(f"`find` matches {hits} times; it must match exactly once.",
                     "Include surrounding lines to make the match unique.", id=node["id"])

    node["text"] = node["text"].replace(find, replace, 1)
    node["author"] = "claude"
    return _ack("patch_doc", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_add_box",
    annotations=ToolAnnotations(title="Draw a labelled box", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_add_box(
    label: Annotated[str, Field(description="Text inside the box. Keep it to a few words.", max_length=200)],
    shape: Annotated[Shape, Field(description="Box outline.")] = Shape.rect,
    private: Private = False,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Put one labelled box on the canvas — a service, a queue, a table, a stage.

    Build system-design sketches out of these plus board_connect when the layout
    matters or the user is going to rearrange it by hand. When you just need a
    structure drawn quickly and nobody will move it, board_draw_diagram is fewer
    calls.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}.
    """
    node = board.add_node("box", text=label, shape=shape.value, private=private, x=x, y=y)
    return _ack("add_box", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_add_text",
    annotations=ToolAnnotations(title="Add a floating label", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_add_text(
    text: Annotated[str, Field(description="A free-floating label on the canvas.", max_length=400)],
    private: Private = False,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Add a bare line of text to the canvas — a heading over a cluster, a constraint
    written where it can be pointed at, a question left up while the user thinks.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}.
    """
    node = board.add_node("text", text=text, private=private, x=x, y=y)
    return _ack("add_text", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_draw_diagram",
    annotations=ToolAnnotations(title="Draw a diagram", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_draw_diagram(
    mermaid: Annotated[str, Field(
        description="Mermaid source, e.g. 'graph TD; A[Client]-->B[API];'. Rendered as one node on the canvas.",
        max_length=8000)],
    title: Annotated[Optional[str], Field(description="Heading on the node.", max_length=120)] = None,
    private: Private = False,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Render a Mermaid diagram as one node on the canvas — recursion trees, call
    stacks, state machines, table schemas, system-design boxes.

    This is your fast path: one call gets a laid-out diagram, where the same picture
    in boxes and lines would be a dozen. The trade is that it renders as a single
    unit, so the user can move the whole node but not drag one box out of it.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}.
    """
    node = board.add_node("diagram", text=mermaid, title=title, private=private, x=x, y=y)
    return _ack("draw_diagram", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_connect",
    annotations=ToolAnnotations(title="Draw a line between two nodes", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_connect(
    source: Annotated[str, Field(description="Node id the line starts at.", min_length=1, max_length=32)],
    target: Annotated[str, Field(description="Node id the line ends at.", min_length=1, max_length=32)],
    label: Annotated[Optional[str], Field(description="Short text on the line.", max_length=80)] = None,
    dashed: Annotated[bool, Field(description="Dashed rather than solid.")] = False,
) -> str:
    """Draw a line from one node to another. The line follows them when either is
    dragged, so the sketch survives the user rearranging it.

    Returns: JSON {ok, action, id, board, rev, viewers} or {ok: false, error, hint}.
    """
    for ident in (source, target):
        if ident not in board.nodes:
            return _missing(ident)
    if source == target:
        return _fail("A line needs two different nodes.", "Pass distinct source and target ids.")

    src, dst = board.nodes[source], board.nodes[target]
    if src.get("private") != dst.get("private"):
        return _fail("That line would cross between the shared board and the private one.",
                     "Connect two shared nodes or two private ones. board_update can move a node across.")

    edge = board.connect(source, target, label or "", dashed)
    return _ack("connect", await board.commit("claude"), id=edge["id"])


@mcp.tool(
    name="board_update",
    annotations=ToolAnnotations(title="Change a node", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_update(
    id: NodeId,
    text: Annotated[Optional[str], Field(description="Replace the node's contents.", max_length=20000)] = None,
    title: Annotated[Optional[str], Field(description="Replace the heading.", max_length=120)] = None,
    language: Annotated[Optional[Language], Field(description="Change syntax colouring on a doc.")] = None,
    private: Annotated[Optional[bool], Field(
        description="Move the node between the shared view and the interviewer's.")] = None,
) -> str:
    """Replace a node's contents, heading or language, or move it between the shared
    board and the interviewer's private layer.

    Flipping private is how you reveal something you were holding: set private=false
    on a model answer and it appears on the candidate's screen at that moment.

    Returns: JSON {ok, action, id, board, rev, viewers} or {ok: false, error, hint}.
    """
    node = board.nodes.get(id)
    if node is None:
        return _missing(id)

    if text is not None:
        node["text"] = text
    if title is not None:
        node["title"] = title
    if language is not None:
        node["language"] = language.value
    if private is not None and private != node["private"]:
        node["private"] = private
        # A line may not straddle the two layers, so drop any that now would.
        for edge_id, edge in list(board.edges.items()):
            if id in (edge["from"], edge["to"]):
                other = board.nodes.get(edge["to"] if edge["from"] == id else edge["from"])
                if other is not None and other["private"] != private:
                    del board.edges[edge_id]
    node["author"] = "claude"
    return _ack("update", await board.commit("claude"), id=node["id"], private=node["private"])


@mcp.tool(
    name="board_move",
    annotations=ToolAnnotations(title="Move a node", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_move(
    id: NodeId,
    x: Annotated[int, Field(description="New left edge in board pixels.")],
    y: Annotated[int, Field(description="New top edge in board pixels.")],
) -> str:
    """Reposition a node — "put the diagram next to the code", "move that out of the
    way". Coordinates are board pixels with the origin at the top left; board_read
    with response_format=json gives you everything's current position.

    Returns: JSON {ok, action, id, board, rev, viewers} or {ok: false, error, hint}.
    """
    node = board.nodes.get(id)
    if node is None:
        return _missing(id)
    node["x"], node["y"] = x, y
    return _ack("move", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_remove",
    annotations=ToolAnnotations(title="Remove a node or line", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_remove(
    id: Annotated[str, Field(description="Id of the node or line to remove. Removing a node removes its lines too.",
                             min_length=1, max_length=32)],
) -> str:
    """Take one node off the board, along with any lines touching it. Also removes a
    line when given a line's id.

    Returns: JSON {ok, action, id, board, rev, viewers} or {ok: false, error, hint}.
    """
    if id in board.edges:
        del board.edges[id]
        return _ack("remove_line", await board.commit("claude"), id=id)
    if board.remove_node(id):
        return _ack("remove_node", await board.commit("claude"), id=id)
    return _missing(id)


@mcp.tool(
    name="board_arrange",
    annotations=ToolAnnotations(title="Tidy the layout", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_arrange() -> str:
    """Reflow everything onto a tidy grid in the order it was created, keeping all
    lines. Use when the canvas has drifted into a mess and the user says so.

    Returns: JSON {ok, action, board, rev, viewers, nodes}.
    """
    board.arrange()
    return _ack("arrange", await board.commit("claude"), nodes=len(board.nodes))


@mcp.tool(
    name="board_note",
    annotations=ToolAnnotations(title="Write a private note", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_note(
    text: Annotated[str, Field(description="One short line: an observation, a hint you gave, a concern.",
                               min_length=1, max_length=400)],
) -> str:
    """Append one line to the interviewer's private notes — an observation, a hint you
    just gave, a concern to come back to.

    Notes live on the interviewer's view alone. They are never sent to the shared
    board, so this is safe to call while the candidate is looking at the screen.

    Returns: JSON {ok, action, board, rev, viewers, notes}.
    """
    board.notes.append({"text": text, "at": now_iso(), "author": "claude"})
    del board.notes[:-MAX_NOTES]
    return _ack("note", await board.commit("claude"), notes=len(board.notes))


@mcp.tool(
    name="board_rubric",
    annotations=ToolAnnotations(title="Score a rubric line", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_rubric(
    label: Annotated[str, Field(description="What is being judged, e.g. 'Edge cases' or 'Complexity analysis'.",
                                min_length=1, max_length=80)],
    verdict: Annotated[Verdict, Field(description="Where the candidate currently stands.")] = Verdict.unset,
    note: Annotated[Optional[str], Field(description="One line of evidence for the verdict.",
                                         max_length=300)] = None,
) -> str:
    """Set where the candidate stands on one dimension. Calling it again with the
    same label updates that line rather than adding a second one, so you can revise
    a verdict as the interview goes.

    Private to the interviewer's view, like notes.

    Returns: JSON {ok, action, board, rev, viewers, rubric}.
    """
    row = next((r for r in board.rubric if r["label"].lower() == label.lower()), None)
    if row is None:
        row = {"label": label, "verdict": "unset", "note": ""}
        board.rubric.append(row)
    row["verdict"] = verdict.value
    if note is not None:
        row["note"] = note
    row["at"] = now_iso()
    return _ack("rubric", await board.commit("claude"), rubric=len(board.rubric))


@mcp.tool(
    name="board_clear",
    annotations=ToolAnnotations(title="Clear the board", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_clear(
    keep_notes: Annotated[bool, Field(description="Keep the interviewer's notes and rubric when clearing.")] = False,
    keep_private: Annotated[bool, Field(description="Keep private nodes, clearing only the shared canvas.")] = False,
) -> str:
    """Empty the canvas, keeping the same board name and file.

    To start a genuinely separate problem use board_new instead — that preserves the
    current board on disk under its own name.

    Returns: JSON {ok, action, board, rev, viewers, note?}.
    """
    if keep_private:
        board.nodes = {i: n for i, n in board.nodes.items() if n.get("private")}
        board.edges = {i: e for i, e in board.edges.items()
                       if e["from"] in board.nodes and e["to"] in board.nodes}
    else:
        board.nodes, board.edges = {}, {}
    if not keep_notes:
        board.notes, board.rubric = [], []
    return _ack("clear", await board.commit("claude"))


# --------------------------------------------------------------------------
# Tools: reading
# --------------------------------------------------------------------------

def _describe(state: Dict[str, Any]) -> str:
    """Read the canvas back as prose, in an order that makes sense out loud."""
    nodes = sorted(state["nodes"].values(), key=lambda n: (n["y"], n["x"]))
    lines = [f"Board: {state['name']} (revision {state['rev']}, "
             f"last edited by {state['last_editor']})"]
    if not nodes:
        lines.append("\nThe canvas is empty.")

    for node in [n for n in nodes if n["type"] == "doc"]:
        tag = " · private" if node.get("private") else ""
        head = node["title"] or "Document"
        lines += [f"\n## {head} [{node['id']}] ({node['language']}{tag})",
                  f"```{node['language']}", node["text"], "```"]

    for node in [n for n in nodes if n["type"] == "diagram"]:
        tag = " · private" if node.get("private") else ""
        lines += [f"\n## {node['title'] or 'Diagram'} [{node['id']}]{tag}",
                  "```mermaid", node["text"], "```"]

    shapes = [n for n in nodes if n["type"] in ("box", "text")]
    if shapes:
        lines.append("\n## Shapes")
        for node in shapes:
            tag = " · private" if node.get("private") else ""
            kind = node["shape"] if node["type"] == "box" else "label"
            lines.append(f"- [{node['id']}] {kind}: {node['text']}"
                         f" (at {node['x']},{node['y']}){tag}")

    if state["edges"]:
        lines.append("\n## Lines")
        for edge in state["edges"].values():
            src = state["nodes"].get(edge["from"], {}).get("text", "")[:30]
            dst = state["nodes"].get(edge["to"], {}).get("text", "")[:30]
            label = f' "{edge["label"]}"' if edge["label"] else ""
            lines.append(f"- [{edge['from']}] {src} -> [{edge['to']}] {dst}{label}")

    if state.get("notes"):
        lines.append("\n## Private notes (interviewer only)")
        lines += [f"- {n['text']}" for n in state["notes"]]

    if state.get("rubric"):
        lines.append("\n## Rubric (interviewer only)")
        for row in state["rubric"]:
            note = f" — {row['note']}" if row.get("note") else ""
            lines.append(f"- {row['label']}: {row['verdict']}{note}")

    return "\n".join(lines)


@mcp.tool(
    name="board_read",
    annotations=ToolAnnotations(title="Read the board", read_only_hint=True,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_read(
    response_format: Annotated[ResponseFormat, Field(
        description="markdown to read it back out loud, json when you need node ids and coordinates.",
    )] = ResponseFormat.markdown,
    include_private: Annotated[bool, Field(
        description="Include the interviewer's private nodes, notes and rubric.")] = True,
) -> str:
    """Read the whole canvas, including everything the user typed or dragged themselves.

    Call this whenever the user refers to what is on screen — "take a look", "is this
    right", "I've finished", "review my solution". You have no ambient view of the
    board; this call is the only way to see their edits.

    Ask for json when you need node ids and coordinates — to patch a specific
    document, connect two boxes, or move something. markdown is easier to read back
    out loud.

    Takes no required arguments: calling it with none at all reads the whole board.

    Returns: markdown describing every node, line, note and rubric row, or JSON
    {slug, name, rev, nodes{}, edges{}, notes[], rubric[], last_editor, saved[]}.
    """
    _ensure_window()
    state = board.snapshot(host=include_private)
    if response_format is ResponseFormat.json:
        return json.dumps(state, indent=2)
    return _describe(state)


@mcp.tool(
    name="board_list",
    annotations=ToolAnnotations(title="List saved boards", read_only_hint=True,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_list() -> str:
    """List every board saved on the user's machine, newest first.

    Use before board_load when the user is vague about which board they want, so you
    can offer the names out loud rather than guessing.

    Returns: JSON {count, active, boards: [{slug, name, nodes, notes, updated}]}.
    """
    return json.dumps({"count": len(store.index()), "active": board.slug, "boards": store.index()}, indent=2)


# --------------------------------------------------------------------------
# Tools: board lifecycle
# --------------------------------------------------------------------------

@mcp.tool(
    name="board_new",
    annotations=ToolAnnotations(title="Start a new board", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_new(
    name: Annotated[str, Field(description="Name for the new board, e.g. 'Two Sum' or 'Design a URL shortener'.",
                               min_length=1, max_length=120)],
) -> str:
    """Save the current board, then start a fresh empty one under a new name.

    This is the safe way to move between problems — nothing is lost, and the old
    board can be reopened later with board_load.

    Returns: JSON {ok, action, board, rev, viewers, previous, slug}.
    """
    previous = board.name
    if board.rev > 0:
        board.flush()
    board.reset(name)
    state = await board.commit("claude")
    board.flush()
    return _ack("new", state, previous=previous, slug=board.slug)


@mcp.tool(
    name="board_save",
    annotations=ToolAnnotations(title="Save the board", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_save(
    name: Annotated[Optional[str], Field(
        description="Save under a new name. Omit to save the board where it already lives.",
        max_length=120)] = None,
) -> str:
    """Write the board to disk now.

    Boards autosave every couple of seconds, so this is mainly for 'save as': pass a
    name to fork the current contents under it, leaving the original file untouched.

    Returns: JSON {ok, action, board, rev, viewers, slug, path}.
    """
    if name:
        board.name = name
        board.slug = slugify(name)
        state = await board.commit("claude")
    else:
        state = board.snapshot()
    path = board.flush()
    return _ack("save", state, slug=board.slug, path=str(path))


@mcp.tool(
    name="board_load",
    annotations=ToolAnnotations(title="Load a saved board", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_load(
    name: Annotated[str, Field(description="Name or slug of a saved board. Partial names are matched.",
                               min_length=1, max_length=120)],
) -> str:
    """Open a saved board, replacing what is currently on screen.

    The current board is saved first, so nothing is lost. Partial names match, and an
    ambiguous name returns the candidates instead of guessing — read them out and ask.

    Returns: JSON {ok, action, board, rev, viewers, slug} or {ok: false, error, hint,
    candidates?}.
    """
    matches = store.resolve(name)
    if not matches:
        available = [b["name"] for b in store.index()]
        return _fail(f"No saved board matching '{name}'.",
                     "Call board_list to see what exists, or board_new to start one.",
                     available=available)
    if len(matches) > 1:
        return _fail(f"'{name}' matches {len(matches)} boards.",
                     "Read the candidates to the user and ask which one they mean.",
                     candidates=matches)

    data = store.load(matches[0])
    if data is None:
        return _fail(f"Board '{matches[0]}' could not be read.", "The file may be corrupt; try board_list.")

    if board.rev > 0 and board.slug != matches[0]:
        board.flush()
    board.adopt(data)
    state = await board.commit("claude")
    return _ack("load", state, slug=board.slug)


@mcp.tool(
    name="board_delete",
    annotations=ToolAnnotations(title="Delete a saved board", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_delete(
    name: Annotated[str, Field(description="Name or slug of the board to delete. Must match exactly one board.",
                               min_length=1, max_length=120)],
    confirm: Annotated[bool, Field(description="Must be true. Ask the user out loud before setting this.")],
) -> str:
    """Permanently delete a saved board file. Confirm with the user out loud first.

    Returns: JSON {ok, action, deleted} or {ok: false, error, hint, candidates?}.
    """
    if not confirm:
        return _fail("Deletion not confirmed.", "Ask the user to confirm, then call again with confirm=true.")
    matches = store.resolve(name)
    if len(matches) != 1:
        return _fail(f"'{name}' matches {len(matches)} boards; need exactly one.",
                     "Call board_list and confirm the exact name with the user.",
                     candidates=matches)
    if matches[0] == board.slug:
        return _fail("That board is currently open.",
                     "Switch away with board_new or board_load first, then delete it.")
    store.delete(matches[0])
    await board.commit("claude")  # refresh the browser's saved-board list
    return json.dumps({"ok": True, "action": "delete", "deleted": matches[0]})


# --------------------------------------------------------------------------
# Web surface
# --------------------------------------------------------------------------

def _is_host(scope: Dict[str, Any]) -> bool:
    return scope.get("board_role") == "host"


async def page(_request) -> FileResponse:
    return FileResponse(PAGE)


async def host_page(request) -> Any:
    """The interviewer's view. Same canvas, plus the private layer. Guarded by its
    own token so handing the shared link to a candidate hands them nothing else."""
    if not _is_host(request.scope):
        return PlainTextResponse("This view needs the interviewer token.", status_code=403)
    return FileResponse(PAGE)


async def health(_request) -> JSONResponse:
    return JSONResponse({"ok": True, "board": board.slug, "rev": board.rev,
                         "viewers": board.viewers, "shared_viewers": board.watchers,
                         "saved": len(store.index())})


async def ws_endpoint(ws: WebSocket) -> None:
    """Live channel. Claude's writes are pushed down; the user's edits come back up,
    so board_read reflects what they actually typed and dragged.

    The socket's role comes from the token it connected with, not from anything the
    page claims, so a shared connection can neither see the private layer nor touch
    it."""
    host = _is_host(ws.scope)
    await ws.accept()
    await board.add_client(ws, host)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")

            # ---- edits aimed at one existing node ----
            if kind in ("user_text", "user_move", "user_resize", "user_language",
                        "user_title", "user_private", "user_delete"):
                ident = msg.get("id", "")
                node = board.nodes.get(ident)
                if node is None or (node.get("private") and not host):
                    continue                      # unknown, or not this socket's to touch
                if kind == "user_text":
                    node["text"] = str(msg.get("text", ""))[:20000]
                elif kind == "user_move":
                    node["x"], node["y"] = int(msg.get("x", 0)), int(msg.get("y", 0))
                elif kind == "user_resize":
                    node["w"] = max(80, min(1600, int(msg.get("w", node["w"]))))
                    node["h"] = max(40, min(1400, int(msg.get("h", node["h"]))))
                elif kind == "user_language":
                    node["language"] = str(msg.get("language", "text"))[:24]
                elif kind == "user_title":
                    node["title"] = str(msg.get("title", ""))[:120]
                elif kind == "user_private" and host:
                    node["private"] = bool(msg.get("private"))
                elif kind == "user_delete":
                    board.remove_node(ident)
                if kind != "user_delete":
                    node["author"] = "user"
                await board.commit("user")

            elif kind == "user_add":
                node_kind = msg.get("kind", "box")
                if node_kind not in NODE_TYPES or len(board.nodes) >= MAX_NODES:
                    continue
                board.add_node(
                    node_kind,
                    text=str(msg.get("text", ""))[:20000],
                    language=str(msg.get("language", "python"))[:24],
                    shape=str(msg.get("shape", "rect"))[:16],
                    # only a host connection can author into the private layer
                    private=bool(msg.get("private")) and host,
                    author="user",
                    x=msg.get("x"), y=msg.get("y"),
                )
                await board.commit("user")

            elif kind == "user_connect":
                src, dst = msg.get("from", ""), msg.get("to", "")
                if (src in board.nodes and dst in board.nodes and src != dst
                        and board.readable(src, host) and board.readable(dst, host)
                        and board.nodes[src]["private"] == board.nodes[dst]["private"]):
                    edge = board.connect(src, dst)
                    edge["author"] = "user"
                    await board.commit("user")

            elif kind == "user_disconnect":
                edge = board.edges.get(msg.get("id", ""))
                if edge and board.readable(edge["from"], host):
                    del board.edges[edge["id"]]
                    await board.commit("user")

            elif kind == "user_arrange":
                board.arrange()
                await board.commit("user")

            # ---- the private layer: host connections only ----
            elif kind == "user_note" and host:
                text = (msg.get("text") or "").strip()
                if text:
                    board.notes.append({"text": text[:400], "at": now_iso(), "author": "user"})
                    del board.notes[:-MAX_NOTES]
                    await board.commit("user")

            elif kind == "user_note_delete" and host:
                index = int(msg.get("index", -1))
                if 0 <= index < len(board.notes):
                    del board.notes[index]
                    await board.commit("user")

            elif kind == "user_rubric" and host:
                label = (msg.get("label") or "").strip()
                if label:
                    row = next((r for r in board.rubric
                                if r["label"].lower() == label.lower()), None)
                    if row is None:
                        row = {"label": label[:80], "verdict": "unset", "note": ""}
                        board.rubric.append(row)
                    row["verdict"] = str(msg.get("verdict", "unset"))[:16]
                    if msg.get("note") is not None:
                        row["note"] = str(msg["note"])[:300]
                    await board.commit("user")

            # ---- whole-board actions ----
            elif kind == "user_new":
                name = (msg.get("name") or "").strip() or "Untitled board"
                if board.rev > 0:
                    board.flush()
                board.reset(name)
                await board.commit("user")
                board.flush()

            elif kind == "user_rename":
                name = (msg.get("name") or "").strip()
                if name and slugify(name) != board.slug:
                    old = board.slug
                    board.name, board.slug = name, slugify(name)
                    board.flush()
                    store.delete(old)          # a rename moves the file, not copies it
                    await board.commit("user")

            elif kind == "user_load":
                data = store.load(msg.get("slug", ""))
                if data:
                    if board.rev > 0:
                        board.flush()
                    board.adopt(data)
                    await board.commit("user")
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError, ValueError):
        pass
    finally:
        board.drop_client(ws)


# streamable_http_app() returns a Starlette app already wired with the MCP
# session-manager lifespan and the endpoint at /mcp. Add the board's own routes
# to it so everything runs in one process on one port.
#
# transport_security must be passed explicitly, and its rebinding guard is turned
# off deliberately. Left unset the SDK sees a loopback host and allows only
# 127.0.0.1/localhost, so every request through a tunnel dies with 421 Invalid
# Host header. An allow-list doesn't fix it either: the hostname isn't knowable
# when Claude Desktop starts this server, and a Cloudflare quick tunnel invents a
# new one on every run.
#
# What the guard defends against is an unauthenticated loopback server — a page
# in your browser quietly POSTing to 127.0.0.1. Gate already refuses every
# request that doesn't carry a token, and /mcp needs the interviewer's one
# specifically, so nothing reaches this middleware unauthenticated and the Host
# check has nothing left to protect. Content-Type is still enforced.
app = mcp.streamable_http_app(
    stateless_http=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
app.router.routes[:0] = [
    Route("/", page),
    Route("/host", host_page),
    Route("/health", health),
    WebSocketRoute("/ws", ws_endpoint),
]


class Gate:
    """Rejects anyone without a token, and decides which view they get.

    There are two tokens. The shared one opens the board and nothing else; the
    interviewer's one also opens /host and /mcp. So the link you hand a candidate
    gives them the canvas without the private layer behind it, and without the
    ability to drive Claude's tools against the board.

    Deliberately answers 403 and not 401. A 401 from an MCP endpoint means "this
    resource uses OAuth" under the MCP auth spec, so Claude responds by hunting for
    a sign-in service that doesn't exist here and the connector fails to register.
    403 says "no" without starting that conversation.

    The token can arrive as an Authorization header or as ?t= on the URL, so the
    connector URL can simply end in /mcp?t=YOUR_TOKEN."""

    HOST_ONLY = ("/mcp", "/host")

    def __init__(self, inner) -> None:
        self.inner = inner

    @staticmethod
    def _role(scope) -> Optional[str]:
        headers = dict(scope.get("headers") or [])
        bearer = headers.get(b"authorization", b"").decode()
        supplied = parse_qs(scope.get("query_string", b"").decode()).get("t", [""])[0]
        for token, role in ((HOST_TOKEN, "host"), (TOKEN, "guest")):
            if (secrets.compare_digest(bearer, f"Bearer {token}")
                    or secrets.compare_digest(supplied, token)):
                return role
        return None

    def _allowed(self, scope) -> Optional[str]:
        role = self._role(scope)
        if role is None:
            return None
        path = scope.get("path", "")
        host_only = any(path == p or path.startswith(p + "/") for p in self.HOST_ONLY)
        return role if role == "host" or not host_only else None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.inner(scope, receive, send)
            return
        role = self._allowed(scope)
        if role is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Forbidden"})
            return
        scope["board_role"] = role
        await self.inner(scope, receive, send)


gated_app = Gate(app)


def _open_app_window(url: str) -> bool:
    """Run the board in its own frameless window instead of a browser tab.

    pywebview drives the macOS Cocoa loop, which has to own the main thread, so the
    server goes to a background thread and this call blocks until the window is
    closed — at which point the process is meant to end."""
    try:
        import webview
    except ImportError:
        print("  The app window needs pywebview:  ./venv/bin/pip install pywebview")
        print(f"  Serving in the browser instead:  {url}\n")
        return False
    webview.create_window("Interview board", url, width=1240, height=860,
                          min_size=(760, 540), on_top=os.environ.get("BOARD_ON_TOP") == "1")
    webview.start()
    return True


def _pick_port(preferred: int) -> int:
    """Claude Desktop may start this while a `bash run.sh` is already holding the
    usual port. Step aside rather than dying."""
    for candidate in [preferred, *range(preferred + 1, preferred + 20)]:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((HOST, candidate))
                return candidate
            except OSError:
                continue
    return preferred


def _find_owner() -> Optional[int]:
    """Look for a board server that is already running.

    Claude Desktop starts one of these per surface — the chat and a Claude Code
    session each get their own — and a `bash run.sh` may be up as well. Left
    alone they would each hold a different board, so Claude would write to one
    while you sat looking at another. Whoever got here first owns the board."""
    for candidate in range(PORT, PORT + 20):
        try:
            with urllib.request.urlopen(
                    f"http://{HOST}:{candidate}/health?t={HOST_TOKEN}", timeout=1) as response:
                if json.loads(response.read()).get("ok"):
                    return candidate
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def _proxy_error(line: str, problem: Exception) -> Optional[str]:
    try:
        ident = json.loads(line).get("id")
    except (json.JSONDecodeError, AttributeError):
        ident = None
    if ident is None:
        return None                      # a notification wants no reply, even a bad one
    return json.dumps({"jsonrpc": "2.0", "id": ident, "error": {
        "code": -32000,
        "message": f"The board server stopped answering ({problem}). "
                   "Start one with: bash run.sh --no-tunnel",
    }})


async def _run_stdio_proxy(port: int) -> None:
    """Hand this session's tool calls to the process that owns the board.

    The owner already speaks MCP over HTTP, so this forwards whole JSON-RPC
    messages rather than reimplementing any tool: every surface ends up driving
    one board, and there is only ever one window to look at."""
    endpoint = f"http://{HOST}:{port}/mcp?t={HOST_TOKEN}"
    loop = asyncio.get_running_loop()

    def forward(line: str) -> Optional[str]:
        request = urllib.request.Request(endpoint, data=line.encode(), headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode()
        for chunk in body.splitlines():
            if chunk.startswith("data: "):
                return chunk[6:]
        return body.strip() or None

    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        if not line.strip():
            continue
        try:
            reply = await loop.run_in_executor(None, forward, line)
        except Exception as problem:                      # noqa: BLE001 - report, don't die
            reply = _proxy_error(line, problem)
        if reply:
            sys.stdout.write(reply + "\n")
            sys.stdout.flush()


def _run_stdio() -> None:
    """Serve MCP over stdin/stdout for Claude Desktop's local servers, with the
    board's own page on a loopback port beside it.

    No tunnel and no connector URL: Claude starts this process itself, so there is
    nothing public to reach and nothing to re-paste when it restarts. Everything
    that would normally print must go to stderr — stdout is the JSON-RPC stream,
    and one stray line through it ends the session."""
    global WINDOW_URL
    import uvicorn

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)

    owner = _find_owner()
    if owner is not None:
        print(f"interview board already running on port {owner}; sharing it", file=sys.stderr)
        asyncio.run(_run_stdio_proxy(owner))
        return

    port = _pick_port(PORT)
    WINDOW_URL = f"http://{HOST}:{port}/host?t={HOST_TOKEN}"
    config = uvicorn.Config(gated_app, host=HOST, port=port,
                            log_level="critical", log_config=None)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    print(f"interview board on {WINDOW_URL}", file=sys.stderr)

    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    # A window is its own short-lived process; see _ensure_window.
    if "--window" in sys.argv:
        _open_app_window(sys.argv[sys.argv.index("--window") + 1])
        raise SystemExit

    if "--stdio" in sys.argv:
        _run_stdio()
        raise SystemExit

    import uvicorn

    host_url = f"http://{HOST}:{PORT}/host?t={HOST_TOKEN}"
    if os.environ.get("BOARD_BANNER") != "0":   # run.sh prints its own
        print("\n  Interview board")
        print(f"  Yours    {host_url}")
        print(f"  Share    http://{HOST}:{PORT}/?t={TOKEN}")
        print(f"  MCP      http://{HOST}:{PORT}/mcp?t={HOST_TOKEN}")
        print(f"  Saving   {BOARDS_DIR}  ({len(store.index())} saved)\n")

    config = uvicorn.Config(gated_app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    if "--app" in sys.argv:
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started and thread.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        if not _open_app_window(host_url):
            thread.join()      # no window to be had; behave like a plain server
    else:
        server.run()
