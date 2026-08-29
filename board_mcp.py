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


# Footprint per node type, in board pixels. When no coordinates are given the
# board finds a free slot itself, so a voice-driven call never has to do layout
# arithmetic mid-sentence.
NODE_SIZES: Dict[str, tuple] = {
    "doc":     (460, 320),
    "diagram": (440, 300),
    "box":     (190, 96),
    "sticky":  (170, 150),
    "text":    (220, 44),
}
NODE_TYPES = tuple(NODE_SIZES)

# Named colours rather than hex, so "make it yellow" works out loud and the page
# can pick shades that stay legible in both light and dark.
COLORS = ("none", "grey", "red", "orange", "yellow", "green", "blue", "purple", "pink")
SIZES = ("s", "m", "l")

GRID_X, GRID_STEP, MARGIN, GUTTER = 500, 60, 40, 24
HISTORY_LIMIT = 60


class Board:
    """The board on screen: positioned nodes and the edges between them, plus
    websocket fan-out, undo history and autosave.

    One board, one view. Everyone connected sees exactly the same thing."""

    def __init__(self) -> None:
        self._clients: set = set()
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
        self.seq = 0
        self.rev = 0
        self.last_editor = "claude"
        self._history: List[Dict[str, Any]] = []
        self._future: List[Dict[str, Any]] = []
        self._before = self.document()

    # ---- nodes and edges ----

    def _fresh_id(self) -> str:
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
            "fill": "yellow" if kind == "sticky" else "none",
            "stroke": "grey", "size": "m", "bold": False,
            "fitted": True,             # page may auto-size it until a human drags
            "author": "claude",
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

    def connect(self, src: str, dst: str, label: str = "", style: str = "solid",
                color: str = "grey", width: int = 2) -> Dict[str, Any]:
        edge = {"id": self._fresh_id(), "from": src, "to": dst, "label": label,
                "style": style, "color": color, "width": width, "author": "claude"}
        self.edges[edge["id"]] = edge
        return edge

    # ---- layout ----

    def arrange(self, layout: str = "grid") -> None:
        if layout == "layered":
            self._layered()
        else:
            self._grid()

    def _grid(self) -> None:
        order = sorted(self.nodes.values(), key=lambda n: n["seq"])
        for node in order:
            node["x"], node["y"] = -100_000, -100_000
        for node in order:
            node["x"], node["y"] = self.place(node["w"], node["h"])

    def _layered(self) -> None:
        """Rank nodes by how deep they sit in the edge graph, then lay the ranks
        out left to right. What you want after describing a flow: the geometry
        follows the structure instead of the order you happened to say things."""
        depth: Dict[str, int] = {i: 0 for i in self.nodes}
        incoming = {i: 0 for i in self.nodes}
        for edge in self.edges.values():
            if edge["to"] in incoming:
                incoming[edge["to"]] += 1

        # longest-path ranking, with a bound so a cycle can't spin forever
        for _ in range(len(self.nodes) + 1):
            changed = False
            for edge in self.edges.values():
                a, b = edge["from"], edge["to"]
                if a in depth and b in depth and depth[b] < depth[a] + 1:
                    depth[b] = depth[a] + 1
                    changed = True
            if not changed:
                break

        ranks: Dict[int, List[Dict[str, Any]]] = {}
        for ident, level in depth.items():
            ranks.setdefault(level, []).append(self.nodes[ident])

        x = MARGIN
        for level in sorted(ranks):
            column = sorted(ranks[level], key=lambda n: n["seq"])
            width = max(n["w"] for n in column)
            y = MARGIN
            for node in column:
                node["x"], node["y"] = x, y
                y += node["h"] + GUTTER * 2
            x += width + GUTTER * 4

    # ---- undo ----

    def _remember(self) -> None:
        self._history.append(self._before)
        del self._history[:-HISTORY_LIMIT]
        self._future.clear()

    def _restore(self, data: Dict[str, Any]) -> None:
        rev = self.rev
        self.adopt(data)
        self.rev = rev

    def undo(self) -> bool:
        if not self._history:
            return False
        self._future.append(self.document())
        self._restore(self._history.pop())
        return True

    def redo(self) -> bool:
        if not self._future:
            return False
        self._history.append(self.document())
        self._restore(self._future.pop())
        return True

    # ---- persistence ----

    def adopt(self, data: Dict[str, Any]) -> None:
        self.name = data.get("name", "Untitled board")
        self.slug = data.get("slug", slugify(self.name))
        self.nodes = {i: dict(n) for i, n in (data.get("nodes") or {}).items()}
        self.edges = {i: dict(e) for i, e in (data.get("edges") or {}).items()}
        self.seq = max([n.get("seq", 0) for n in self.nodes.values()] or [0])
        self.rev = int(data.get("rev", 0))
        self.last_editor = "claude"
        for node in self.nodes.values():
            node.setdefault("fill", "yellow" if node.get("type") == "sticky" else "none")
            node.setdefault("stroke", "grey")
            node.setdefault("size", "m")
            node.setdefault("bold", False)
            node.setdefault("fitted", True)
            node.pop("private", None)            # boards from the two-view version
        for edge in self.edges.values():
            edge.setdefault("style", "dashed" if edge.pop("dashed", False) else "solid")
            edge.setdefault("color", "grey")
            edge.setdefault("width", 2)
        if not self.nodes:
            self._migrate_panes(data)

    def _migrate_panes(self, data: Dict[str, Any]) -> None:
        """Boards written by the three-pane version keep opening."""
        code = data.get("code") or {}
        diagram = data.get("diagram") or {}
        if (code.get("source") or "").strip():
            self.add_node("doc", title=code.get("title") or "", text=code["source"],
                          language=code.get("language") or "python")
        if (diagram.get("mermaid") or "").strip():
            self.add_node("diagram", title=diagram.get("title") or "", text=diagram["mermaid"])

    def document(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "nodes": {i: dict(n) for i, n in self.nodes.items()},
            "edges": {i: dict(e) for i, e in self.edges.items()},
            "rev": self.rev,
            "updated": now_iso(),
        }

    def snapshot(self) -> Dict[str, Any]:
        doc = self.document()
        doc["nodes"] = {i: {k: v for k, v in n.items() if k != "seq"}
                        for i, n in self.nodes.items()}
        doc["last_editor"] = self.last_editor
        doc["saved"] = store.index()
        doc["can_undo"] = bool(self._history)
        doc["can_redo"] = bool(self._future)
        return doc

    # ---- plumbing ----

    async def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        await ws.send_text(json.dumps({"type": "state", "board": self.snapshot()}))

    def drop_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def commit(self, editor: str, remember: bool = True) -> Dict[str, Any]:
        """Bump the revision, push to every open page, queue an autosave.

        `remember` is false for undo and redo, which move through the history
        rather than adding to it."""
        async with self._lock:
            if remember:
                self._remember()
            self.rev += 1
            self.last_editor = editor
            self._dirty = True
            self._before = self.document()
            state = self.snapshot()
        payload = json.dumps({"type": "state", "board": state, "origin": editor})
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                self.drop_client(ws)
        self._ensure_saver()
        return state

    def _ensure_saver(self) -> None:
        if self._saver is None or self._saver.done():
            self._saver = asyncio.ensure_future(self._autosave_loop())

    async def _autosave_loop(self) -> None:
        while True:
            await asyncio.sleep(AUTOSAVE_SECONDS)
            if not self._dirty:
                return
            self._dirty = False
            try:
                store.save(self.document())
            except OSError:
                self._dirty = True

    def flush(self) -> Path:
        self._dirty = False
        return store.save(self.document())

    @property
    def viewers(self) -> int:
        return len(self._clients)


board = Board()


def _ensure_window() -> None:
    """Open the board window the first time Claude actually touches the board.

    Claude Desktop starts its local servers when the app launches, and a window
    appearing then would be a window nobody asked for. Tracking the process rather
    than a flag means closing the window isn't permanent — the next thing Claude
    draws brings it back."""
    global _window_proc
    if not WINDOW_URL:
        return
    if _window_proc is not None and _window_proc.poll() is None:
        return
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
    }
    if board.viewers == 0:
        payload["note"] = "No window has the board open, so nobody can see this."
    payload.update(extra)
    return json.dumps(payload)


def _fail(error: str, hint: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": error, "hint": hint, **extra})


# --------------------------------------------------------------------------
# Tool inputs
# --------------------------------------------------------------------------
#
# Every tool takes its fields directly rather than nesting them under a wrapper
# model: a tool whose fields all have defaults looks callable with no arguments,
# the wrapper goes missing, and the call dies on validation — which the client
# reports as a bare "tool call failed" with nothing to act on.
#
# Names are deliberately the same everywhere. Contents are always `text`, never
# `label` or `source` or `mermaid`; a node is always `id`; an edge always runs
# from_id -> to_id, matching what board_read prints back.

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


class Color(str, Enum):
    none = "none"
    grey = "grey"
    red = "red"
    orange = "orange"
    yellow = "yellow"
    green = "green"
    blue = "blue"
    purple = "purple"
    pink = "pink"


class TextSize(str, Enum):
    s = "s"
    m = "m"
    l = "l"


class EdgeStyle(str, Enum):
    solid = "solid"
    dashed = "dashed"


class Layout(str, Enum):
    layered = "layered"
    grid = "grid"


XPos = Annotated[Optional[int], Field(description="Left edge in board pixels. Omit and the board finds a free spot.")]
YPos = Annotated[Optional[int], Field(description="Top edge in board pixels. Omit and the board finds a free spot.")]
NodeId = Annotated[str, Field(description="Node id, from board_read or from the call that created it.",
                              min_length=1, max_length=32)]
FillArg = Annotated[Optional[Color], Field(description="Background colour. 'none' leaves it plain.")]
StrokeArg = Annotated[Optional[Color], Field(description="Border and text accent colour.")]


# --------------------------------------------------------------------------
# Tools: the canvas
# --------------------------------------------------------------------------

def _latest(kind: str) -> Optional[Dict[str, Any]]:
    matches = [n for n in board.nodes.values() if n["type"] == kind]
    return max(matches, key=lambda n: n["seq"]) if matches else None


def _missing(ident: str) -> str:
    return _fail(f"No node or line with id {ident!r} is on the board.",
                 "Call board_read with response_format=json to see the current ids.")


def _style(**pairs: Any) -> Dict[str, Any]:
    """Drop the styling arguments that weren't supplied, and unwrap the enums."""
    out = {}
    for key, value in pairs.items():
        if value is not None:
            out[key] = value.value if isinstance(value, Enum) else value
    return out


@mcp.tool(
    name="board_add_doc",
    annotations=ToolAnnotations(title="Put a document on the board", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_add_doc(
    text: Annotated[str, Field(description="Contents: source code, or prose when language is markdown.",
                               max_length=20000)],
    language: Annotated[Language, Field(description="Governs syntax colouring. Use markdown for prose.")] = Language.python,
    title: Annotated[Optional[str], Field(description="Heading on the node.", max_length=120)] = None,
    fill: FillArg = None,
    stroke: StrokeArg = None,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Drop a document onto the canvas: source code, or prose when language is
    markdown. It can be dragged, resized and typed into, and a board can hold as
    many as you like side by side.

    For small edits mid-discussion prefer board_patch_doc, so the reader's eye
    doesn't lose its place.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}. Keep the id — you
    need it for board_patch_doc, board_connect and board_move.
    """
    node = board.add_node("doc", text=text, language=language.value, title=title,
                          x=x, y=y, **_style(fill=fill, stroke=stroke))
    return _ack("add_doc", await board.commit("claude"), id=node["id"])


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
    text: Annotated[str, Field(description="Text inside the box. Keep it to a few words.", max_length=200)],
    shape: Annotated[Shape, Field(description="Box outline.")] = Shape.rect,
    fill: FillArg = None,
    stroke: StrokeArg = None,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Put one labelled box on the canvas — a service, a queue, a table, a stage.
    The box grows to fit its text.

    Building a whole diagram? Use board_apply instead: one call for every box and
    line at once, laid out for you, rather than a dozen calls and hand-computed
    coordinates.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}.
    """
    node = board.add_node("box", text=text, shape=shape.value, x=x, y=y,
                          **_style(fill=fill, stroke=stroke))
    return _ack("add_box", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_add_sticky",
    annotations=ToolAnnotations(title="Add a sticky note", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_add_sticky(
    text: Annotated[str, Field(description="What the sticky says. A line or two.", max_length=600)],
    fill: Annotated[Color, Field(description="Sticky colour. Cluster related ideas by colour.")] = Color.yellow,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Add a sticky note — smaller and softer than a box, meant for clustering
    ideas, questions and observations rather than naming parts of a system.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}.
    """
    node = board.add_node("sticky", text=text, fill=fill.value, x=x, y=y)
    return _ack("add_sticky", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_add_text",
    annotations=ToolAnnotations(title="Add a floating label", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_add_text(
    text: Annotated[str, Field(description="A free-floating label on the canvas.", max_length=400)],
    size: Annotated[TextSize, Field(description="Text size: s, m, or l. Use l for headings.")] = TextSize.m,
    bold: Annotated[bool, Field(description="Bold the text.")] = False,
    stroke: StrokeArg = None,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Add a bare line of text — a heading over a cluster, a constraint written
    where it can be pointed at, a question left up while someone thinks.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}.
    """
    node = board.add_node("text", text=text, size=size.value, bold=bold, x=x, y=y,
                          **_style(stroke=stroke))
    return _ack("add_text", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_draw_diagram",
    annotations=ToolAnnotations(title="Draw a diagram", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_draw_diagram(
    text: Annotated[str, Field(
        description="Mermaid source, e.g. 'graph TD; A[Client]-->B[API];'. Rendered as one node.",
        max_length=8000)],
    title: Annotated[Optional[str], Field(description="Heading on the node.", max_length=120)] = None,
    x: XPos = None,
    y: YPos = None,
) -> str:
    """Render a Mermaid diagram as one node — recursion trees, call stacks, state
    machines, table schemas, sequence diagrams.

    Use this when the picture is illustration and nobody will rearrange it. Use
    board_apply when the boxes are the subject and someone will want to drag them.

    Returns: JSON {ok, action, id, board, rev, viewers, note?}.
    """
    node = board.add_node("diagram", text=text, title=title, x=x, y=y)
    return _ack("draw_diagram", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_connect",
    annotations=ToolAnnotations(title="Draw a line between two nodes", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_connect(
    from_id: Annotated[str, Field(description="Node id the line starts at.", min_length=1, max_length=32)],
    to_id: Annotated[str, Field(description="Node id the line ends at.", min_length=1, max_length=32)],
    text: Annotated[Optional[str], Field(description="Short label on the line.", max_length=80)] = None,
    style: Annotated[EdgeStyle, Field(description="Solid or dashed.")] = EdgeStyle.solid,
    color: Annotated[Color, Field(description="Line colour.")] = Color.grey,
    width: Annotated[int, Field(description="Line thickness in pixels, 1 to 8.", ge=1, le=8)] = 2,
) -> str:
    """Draw a line from one node to another. It follows them when either is
    dragged, so the sketch survives being rearranged.

    Returns: JSON {ok, action, id, board, rev, viewers} or {ok: false, error, hint}.
    """
    for ident in (from_id, to_id):
        if ident not in board.nodes:
            return _missing(ident)
    if from_id == to_id:
        return _fail("A line needs two different nodes.", "Pass distinct from_id and to_id.")
    edge = board.connect(from_id, to_id, text or "", style.value, color.value, width)
    return _ack("connect", await board.commit("claude"), id=edge["id"])


@mcp.tool(
    name="board_apply",
    annotations=ToolAnnotations(title="Build a whole diagram at once", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_apply(
    nodes: Annotated[List[Dict[str, Any]], Field(description=(
        "Nodes to create. Each is an object: {\"key\": \"api\", \"kind\": \"box\", \"text\": \"API\"} "
        "where key is yours to name and is what the edges below refer to. kind is box, sticky, text, "
        "doc or diagram (default box). Optional: fill, stroke, shape, size, bold, title, language, x, y."
    ))],
    edges: Annotated[Optional[List[Dict[str, Any]]], Field(description=(
        "Lines to draw. Each is {\"from\": \"api\", \"to\": \"db\", \"text\": \"writes\"} where from and to "
        "are keys from `nodes` above, or ids of nodes already on the board. "
        "Optional: style (solid/dashed), color, width."
    ))] = None,
    layout: Annotated[Layout, Field(
        description="layered follows the arrows and is what you want for a flow; grid just tidies.")] = Layout.layered,
    replace: Annotated[bool, Field(description="Clear the board first, rather than adding to it.")] = False,
) -> str:
    """Build an entire diagram in one call, laid out for you.

    This is the tool to reach for whenever you're drawing more than one thing.
    A flowchart that would be fifteen separate calls with hand-computed
    coordinates is one call here, and the layout follows the arrows rather than
    the order you happened to mention things.

    Returns: JSON {ok, action, ids: {your key -> node id}, nodes, edges, board,
    rev, viewers} or {ok: false, error, hint}.
    """
    if not nodes:
        return _fail("No nodes given.", "Pass at least one node, e.g. [{\"key\":\"a\",\"text\":\"Start\"}].")
    if len(nodes) > MAX_NODES:
        return _fail(f"{len(nodes)} nodes is more than the board holds ({MAX_NODES}).",
                     "Split it across a couple of calls, or raise MAX_NODES.")

    if replace:
        board.nodes, board.edges = {}, {}

    made: Dict[str, str] = {}
    for index, spec in enumerate(nodes):
        if not isinstance(spec, dict):
            return _fail(f"Node {index} is not an object.", "Each node is {\"key\": ..., \"text\": ...}.")
        kind = str(spec.get("kind") or "box")
        if kind not in NODE_TYPES:
            return _fail(f"Node {index} has kind {kind!r}.",
                         f"Use one of: {', '.join(NODE_TYPES)}.")
        fields = {k: spec[k] for k in
                  ("text", "title", "language", "shape", "fill", "stroke", "size", "bold", "x", "y")
                  if k in spec}
        for key in ("fill", "stroke"):
            if key in fields and fields[key] not in COLORS:
                return _fail(f"Node {index} has {key}={fields[key]!r}.",
                             f"Colours are: {', '.join(COLORS)}.")
        node = board.add_node(kind, **fields)
        made[str(spec.get("key", node["id"]))] = node["id"]

    def resolve(ref: Any) -> Optional[str]:
        ref = str(ref)
        if ref in made:
            return made[ref]
        return ref if ref in board.nodes else None

    drawn = 0
    for index, spec in enumerate(edges or []):
        if not isinstance(spec, dict):
            return _fail(f"Edge {index} is not an object.", "Each edge is {\"from\": ..., \"to\": ...}.")
        src, dst = resolve(spec.get("from")), resolve(spec.get("to"))
        if src is None or dst is None:
            return _fail(f"Edge {index} points at something that doesn't exist "
                         f"({spec.get('from')!r} -> {spec.get('to')!r}).",
                         "Use a key from `nodes`, or the id of a node already on the board.",
                         keys=sorted(made))
        if src == dst:
            continue
        board.connect(src, dst, str(spec.get("text") or ""),
                      str(spec.get("style") or "solid"),
                      str(spec.get("color") or "grey"),
                      int(spec.get("width") or 2))
        drawn += 1

    board.arrange(layout.value)
    return _ack("apply", await board.commit("claude"),
                ids=made, nodes=len(made), edges=drawn)


@mcp.tool(
    name="board_update",
    annotations=ToolAnnotations(title="Change or restyle nodes", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_update(
    id: Annotated[Optional[str], Field(description="One node id.", max_length=32)] = None,
    ids: Annotated[Optional[List[str]], Field(
        description="Several node ids, to restyle them together in one call.")] = None,
    text: Annotated[Optional[str], Field(description="Replace the node's contents.", max_length=20000)] = None,
    title: Annotated[Optional[str], Field(description="Replace the heading.", max_length=120)] = None,
    language: Annotated[Optional[Language], Field(description="Change syntax colouring on a doc.")] = None,
    fill: FillArg = None,
    stroke: StrokeArg = None,
    size: Annotated[Optional[TextSize], Field(description="Text size: s, m or l.")] = None,
    bold: Annotated[Optional[bool], Field(description="Bold the text.")] = None,
    shape: Annotated[Optional[Shape], Field(description="Box outline.")] = None,
) -> str:
    """Change what a node says, or how it looks.

    Pass `ids` instead of `id` to restyle a group in one call — colour-coding six
    boxes is one call, not six.

    Returns: JSON {ok, action, ids, board, rev, viewers} or {ok: false, error, hint}.
    """
    targets = [i for i in ([id] if id else []) + list(ids or []) if i]
    if not targets:
        return _fail("No node named.", "Pass id for one node, or ids for several.")

    changes = _style(text=text, title=title, language=language, fill=fill,
                     stroke=stroke, size=size, bold=bold, shape=shape)
    if not changes:
        return _fail("Nothing to change.", "Pass at least one of text, title, fill, stroke, size, bold, shape.")

    touched = []
    for ident in targets:
        node = board.nodes.get(ident)
        if node is None:
            return _missing(ident)
        node.update(changes)
        node["author"] = "claude"
        touched.append(ident)
    return _ack("update", await board.commit("claude"), ids=touched)


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
    """Reposition a node. Coordinates are board pixels with the origin top left;
    board_read with response_format=json gives you everything's position.

    For anything more than a nudge, board_arrange or board_apply will lay the
    whole board out better than coordinates picked by hand.

    Returns: JSON {ok, action, id, board, rev, viewers} or {ok: false, error, hint}.
    """
    node = board.nodes.get(id)
    if node is None:
        return _missing(id)
    node["x"], node["y"] = x, y
    node["fitted"] = False
    return _ack("move", await board.commit("claude"), id=node["id"])


@mcp.tool(
    name="board_remove",
    annotations=ToolAnnotations(title="Remove nodes or lines", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_remove(
    id: Annotated[Optional[str], Field(description="Id of one node or line.", max_length=32)] = None,
    ids: Annotated[Optional[List[str]], Field(description="Several ids, removed together.")] = None,
) -> str:
    """Take nodes off the board, along with any lines touching them. Also removes
    a line when given a line's id.

    Returns: JSON {ok, action, removed, board, rev, viewers} or {ok: false, error, hint}.
    """
    targets = [i for i in ([id] if id else []) + list(ids or []) if i]
    if not targets:
        return _fail("Nothing named.", "Pass id for one, or ids for several.")
    removed = []
    for ident in targets:
        if ident in board.edges:
            del board.edges[ident]
            removed.append(ident)
        elif board.remove_node(ident):
            removed.append(ident)
        else:
            return _missing(ident)
    return _ack("remove", await board.commit("claude"), removed=removed)


@mcp.tool(
    name="board_arrange",
    annotations=ToolAnnotations(title="Lay the board out", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_arrange(
    layout: Annotated[Layout, Field(
        description="layered follows the arrows; grid just tidies into columns.")] = Layout.layered,
) -> str:
    """Lay every node out again, keeping all lines.

    layered ranks nodes by how deep they sit in the graph and puts each rank in
    its own column, so a flow reads left to right however it was built. grid
    ignores the arrows and just packs things tidily.

    Returns: JSON {ok, action, board, rev, viewers, nodes, layout}.
    """
    board.arrange(layout.value)
    return _ack("arrange", await board.commit("claude"),
                nodes=len(board.nodes), layout=layout.value)


@mcp.tool(
    name="board_undo",
    annotations=ToolAnnotations(title="Undo the last change", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=False, open_world_hint=False),
)
async def board_undo() -> str:
    """Step the whole board back one change — yours or anyone else's.

    Returns: JSON {ok, action, board, rev, viewers} or {ok: false, error, hint}.
    """
    if not board.undo():
        return _fail("Nothing to undo.", "This is as far back as the history goes.")
    return _ack("undo", await board.commit("claude", remember=False))


@mcp.tool(
    name="board_redo",
    annotations=ToolAnnotations(title="Redo the last undo", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=False, open_world_hint=False),
)
async def board_redo() -> str:
    """Put back what board_undo just took away.

    Returns: JSON {ok, action, board, rev, viewers} or {ok: false, error, hint}.
    """
    if not board.redo():
        return _fail("Nothing to redo.", "Nothing has been undone, or the board changed since.")
    return _ack("redo", await board.commit("claude", remember=False))


@mcp.tool(
    name="board_clear",
    annotations=ToolAnnotations(title="Clear the board", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_clear() -> str:
    """Empty the canvas, keeping the same board name and file. Undoable.

    To start a genuinely separate subject use board_new instead — that preserves
    the current board on disk under its own name.

    Returns: JSON {ok, action, board, rev, viewers, note?}.
    """
    board.nodes, board.edges = {}, {}
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
        lines += [f"\n## {node['title'] or 'Document'} [{node['id']}] ({node['language']})",
                  f"```{node['language']}", node["text"], "```"]

    for node in [n for n in nodes if n["type"] == "diagram"]:
        lines += [f"\n## {node['title'] or 'Diagram'} [{node['id']}]",
                  "```mermaid", node["text"], "```"]

    shapes = [n for n in nodes if n["type"] in ("box", "sticky", "text")]
    if shapes:
        lines.append("\n## Shapes")
        for node in shapes:
            kind = {"box": node["shape"], "sticky": "sticky", "text": "label"}[node["type"]]
            paint = [f"{k}={node[k]}" for k in ("fill", "stroke") if node.get(k) not in (None, "none", "grey")]
            extra = f" [{', '.join(paint)}]" if paint else ""
            lines.append(f"- [{node['id']}] {kind}: {node['text']}"
                         f" (at {node['x']},{node['y']}){extra}")

    if state["edges"]:
        lines.append("\n## Lines")
        for edge in state["edges"].values():
            src = state["nodes"].get(edge["from"], {}).get("text", "")[:30]
            dst = state["nodes"].get(edge["to"], {}).get("text", "")[:30]
            label = f' "{edge["label"]}"' if edge["label"] else ""
            lines.append(f"- [{edge['id']}] from_id={edge['from']} ({src}) "
                         f"-> to_id={edge['to']} ({dst}){label}")

    return "\n".join(lines)


@mcp.tool(
    name="board_read",
    annotations=ToolAnnotations(title="Read the board", read_only_hint=True,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_read(
    response_format: Annotated[ResponseFormat, Field(
        description="markdown to read it back out loud, json when you need ids, colours and coordinates.",
    )] = ResponseFormat.markdown,
) -> str:
    """Read the whole canvas, including everything anyone typed or dragged.

    Call this whenever someone refers to what is on screen — "take a look", "is
    this right", "I've finished". You have no ambient view of the board; this
    call is the only way to see their edits.

    Takes no required arguments: calling it with none at all reads the whole board.

    Returns: markdown describing every node and line, or JSON
    {slug, name, rev, nodes{}, edges{}, last_editor, saved[], can_undo, can_redo}.
    """
    state = board.snapshot()
    if response_format is ResponseFormat.json:
        return json.dumps(state, indent=2)
    return _describe(state)


@mcp.tool(
    name="board_list",
    annotations=ToolAnnotations(title="List saved boards", read_only_hint=True,
                               destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def board_list() -> str:
    """List every board saved on this machine, newest first.

    Use before board_load when someone is vague about which board they want, so
    you can offer the names out loud rather than guessing.

    Returns: JSON {count, active, boards: [{slug, name, nodes, updated}]}.
    """
    return json.dumps({"count": len(store.index()), "active": board.slug,
                       "boards": store.index()}, indent=2)


# --------------------------------------------------------------------------
# Tools: board lifecycle
# --------------------------------------------------------------------------

@mcp.tool(
    name="board_new",
    annotations=ToolAnnotations(title="Start a new board", read_only_hint=False,
                               destructive_hint=False, idempotent_hint=False, open_world_hint=False),
)
async def board_new(
    name: Annotated[str, Field(description="Name for the new board, e.g. 'Payments redesign'.",
                               min_length=1, max_length=120)],
) -> str:
    """Save the current board, then start a fresh empty one under a new name.

    The safe way to move between subjects — nothing is lost, and the old board
    can be reopened later with board_load.

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
        description="Save under a new name. Omit to save where it already lives.",
        max_length=120)] = None,
) -> str:
    """Write the board to disk now.

    Boards autosave every couple of seconds, so this is mainly for 'save as':
    pass a name to fork the current contents, leaving the original untouched.

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

    The current board is saved first, so nothing is lost. Partial names match, and
    an ambiguous name returns the candidates instead of guessing.

    Returns: JSON {ok, action, board, rev, viewers, slug} or {ok: false, error,
    hint, candidates?}.
    """
    matches = store.resolve(name)
    if not matches:
        return _fail(f"No saved board matching '{name}'.",
                     "Call board_list to see what exists, or board_new to start one.",
                     available=[b["name"] for b in store.index()])
    if len(matches) > 1:
        return _fail(f"'{name}' matches {len(matches)} boards.",
                     "Read the candidates out and ask which one they mean.",
                     candidates=matches)

    data = store.load(matches[0])
    if data is None:
        return _fail(f"Board '{matches[0]}' could not be read.", "The file may be corrupt; try board_list.")

    if board.rev > 0 and board.slug != matches[0]:
        board.flush()
    board.adopt(data)
    return _ack("load", await board.commit("claude"), slug=board.slug)


@mcp.tool(
    name="board_delete",
    annotations=ToolAnnotations(title="Delete a saved board", read_only_hint=False,
                               destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
async def board_delete(
    name: Annotated[str, Field(description="Name or slug of the board to delete. Must match exactly one.",
                               min_length=1, max_length=120)],
    confirm: Annotated[bool, Field(description="Must be true. Ask the user out loud before setting this.")],
) -> str:
    """Permanently delete a saved board file. Confirm out loud first.

    Returns: JSON {ok, action, deleted} or {ok: false, error, hint, candidates?}.
    """
    if not confirm:
        return _fail("Deletion not confirmed.", "Ask the user to confirm, then call again with confirm=true.")
    matches = store.resolve(name)
    if len(matches) != 1:
        return _fail(f"'{name}' matches {len(matches)} boards; need exactly one.",
                     "Call board_list and confirm the exact name.", candidates=matches)
    if matches[0] == board.slug:
        return _fail("That board is currently open.",
                     "Switch away with board_new or board_load first, then delete it.")
    store.delete(matches[0])
    await board.commit("claude")
    return json.dumps({"ok": True, "action": "delete", "deleted": matches[0]})


# --------------------------------------------------------------------------
# Web surface
# --------------------------------------------------------------------------

async def page(_request) -> FileResponse:
    return FileResponse(PAGE)


async def health(_request) -> JSONResponse:
    return JSONResponse({"ok": True, "board": board.slug, "rev": board.rev,
                         "viewers": board.viewers, "saved": len(store.index())})


def _clean(value: Any, allowed: tuple, fallback: Any) -> Any:
    return value if value in allowed else fallback


async def ws_endpoint(ws: WebSocket) -> None:
    """Live channel. Claude's writes are pushed down; everyone's edits come back
    up, so board_read reflects what people actually typed and dragged.

    Every connection is equal — one board, one view."""
    await ws.accept()
    await board.add_client(ws)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")

            if kind in ("user_text", "user_resize", "user_language", "user_title"):
                node = board.nodes.get(msg.get("id", ""))
                if node is None:
                    continue
                if kind == "user_text":
                    node["text"] = str(msg.get("text", ""))[:20000]
                elif kind == "user_resize":
                    node["w"] = max(60, min(1600, int(msg.get("w", node["w"]))))
                    node["h"] = max(32, min(1400, int(msg.get("h", node["h"]))))
                    node["fitted"] = bool(msg.get("fitted", False))
                elif kind == "user_language":
                    node["language"] = str(msg.get("language", "text"))[:24]
                elif kind == "user_title":
                    node["title"] = str(msg.get("title", ""))[:120]
                node["author"] = "user"
                await board.commit("user")

            elif kind == "user_move":
                moved = False
                for spec in msg.get("moves") or []:
                    node = board.nodes.get(spec.get("id", ""))
                    if node is None:
                        continue
                    node["x"], node["y"] = int(spec.get("x", 0)), int(spec.get("y", 0))
                    node["author"] = "user"
                    moved = True
                if moved:
                    await board.commit("user")

            elif kind == "user_style":
                changes: Dict[str, Any] = {}
                for key, allowed in (("fill", COLORS), ("stroke", COLORS), ("size", SIZES),
                                     ("shape", ("rect", "ellipse", "diamond"))):
                    if msg.get(key) in allowed:
                        changes[key] = msg[key]
                if "bold" in msg:
                    changes["bold"] = bool(msg["bold"])
                if changes:
                    hit = False
                    for ident in msg.get("ids") or []:
                        node = board.nodes.get(ident)
                        if node is not None:
                            node.update(changes)
                            node["author"] = "user"
                            hit = True
                    for ident in msg.get("edge_ids") or []:
                        edge = board.edges.get(ident)
                        if edge is not None and "stroke" in changes:
                            edge["color"] = changes["stroke"]
                            hit = True
                    if hit:
                        await board.commit("user")

            elif kind == "user_add":
                node_kind = msg.get("kind", "box")
                if node_kind not in NODE_TYPES or len(board.nodes) >= MAX_NODES:
                    continue
                board.add_node(
                    node_kind,
                    text=str(msg.get("text", ""))[:20000],
                    language=str(msg.get("language", "python"))[:24],
                    fill=_clean(msg.get("fill"), COLORS, "yellow" if node_kind == "sticky" else "none"),
                    author="user",
                    x=msg.get("x"), y=msg.get("y"),
                )
                await board.commit("user")

            elif kind == "user_delete":
                gone = False
                for ident in msg.get("ids") or []:
                    if ident in board.edges:
                        del board.edges[ident]
                        gone = True
                    elif board.remove_node(ident):
                        gone = True
                if gone:
                    await board.commit("user")

            elif kind == "user_connect":
                src, dst = msg.get("from", ""), msg.get("to", "")
                if src in board.nodes and dst in board.nodes and src != dst:
                    edge = board.connect(src, dst)
                    edge["author"] = "user"
                    await board.commit("user")

            elif kind == "user_arrange":
                board.arrange(_clean(msg.get("layout"), ("layered", "grid"), "layered"))
                await board.commit("user")

            elif kind == "user_undo":
                if board.undo():
                    await board.commit("user", remember=False)

            elif kind == "user_redo":
                if board.redo():
                    await board.commit("user", remember=False)

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
# The SDK's DNS-rebinding guard is off deliberately. Left on it allows only
# 127.0.0.1/localhost, so every request through a tunnel dies with 421 Invalid
# Host header — and an allow-list can't fix that, because the hostname isn't
# knowable when the server starts and a quick tunnel invents a new one each run.
# What the guard defends is an unauthenticated loopback server; Gate refuses
# every request without the token, so there is nothing left for it to protect.
app = mcp.streamable_http_app(
    stateless_http=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
app.router.routes[:0] = [
    Route("/", page),
    Route("/health", health),
    WebSocketRoute("/ws", ws_endpoint),
]


class Gate:
    """Rejects anyone without the token.

    Deliberately answers 403 and not 401. A 401 from an MCP endpoint means "this
    resource uses OAuth" under the MCP auth spec, so Claude responds by hunting for
    a sign-in service that doesn't exist here and the connector fails to register.
    403 says "no" without starting that conversation.

    The token can arrive as an Authorization header or as ?t= on the URL, so the
    connector URL can simply end in /mcp?t=YOUR_TOKEN."""

    def __init__(self, inner) -> None:
        self.inner = inner

    @staticmethod
    def _authorized(scope) -> bool:
        headers = dict(scope.get("headers") or [])
        if secrets.compare_digest(headers.get(b"authorization", b"").decode(), f"Bearer {TOKEN}"):
            return True
        supplied = parse_qs(scope.get("query_string", b"").decode()).get("t", [""])[0]
        return secrets.compare_digest(supplied, TOKEN)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.inner(scope, receive, send)
            return
        if not self._authorized(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Forbidden"})
            return
        await self.inner(scope, receive, send)


gated_app = Gate(app)


def _open_app_window(url: str) -> bool:
    """Run the board in its own frameless window instead of a browser tab.

    pywebview drives the macOS Cocoa loop, which has to own the main thread, so
    this blocks until the window is closed."""
    try:
        import webview
    except ImportError:
        print("  The app window needs pywebview:  ./venv/bin/pip install pywebview")
        print(f"  Serving in the browser instead:  {url}\n")
        return False
    webview.create_window("Board", url, width=1240, height=860,
                          min_size=(760, 540), on_top=os.environ.get("BOARD_ON_TOP") == "1")
    webview.start()
    return True


def _pick_port(preferred: int) -> int:
    """Another board may already be holding the usual port. Step aside."""
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
    """Look for a board server that is already running, so several MCP clients
    share one board rather than each holding an invisible one of its own."""
    for candidate in range(PORT, PORT + 20):
        try:
            with urllib.request.urlopen(
                    f"http://{HOST}:{candidate}/health?t={TOKEN}", timeout=1) as response:
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
        return None
    return json.dumps({"jsonrpc": "2.0", "id": ident, "error": {
        "code": -32000,
        "message": f"The board server stopped answering ({problem}). "
                   "Start one with: bash run.sh --no-tunnel",
    }})


async def _run_stdio_proxy(port: int) -> None:
    """Hand this session's tool calls to the process that owns the board, by
    forwarding whole JSON-RPC messages to the MCP endpoint it already serves."""
    endpoint = f"http://{HOST}:{port}/mcp?t={TOKEN}"
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
    """Serve MCP over stdin/stdout, with the board's page on a loopback port.

    Everything that would normally print must go to stderr — stdout is the
    JSON-RPC stream, and one stray line through it ends the session."""
    global WINDOW_URL
    import uvicorn

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)

    owner = _find_owner()
    if owner is not None:
        print(f"board already running on port {owner}; sharing it", file=sys.stderr)
        asyncio.run(_run_stdio_proxy(owner))
        return

    port = _pick_port(PORT)
    WINDOW_URL = f"http://{HOST}:{port}/?t={TOKEN}"
    config = uvicorn.Config(gated_app, host=HOST, port=port,
                            log_level="critical", log_config=None)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    print(f"board on {WINDOW_URL}", file=sys.stderr)

    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    if "--window" in sys.argv:
        _open_app_window(sys.argv[sys.argv.index("--window") + 1])
        raise SystemExit

    if "--stdio" in sys.argv:
        _run_stdio()
        raise SystemExit

    import uvicorn

    board_url = f"http://{HOST}:{PORT}/?t={TOKEN}"
    if os.environ.get("BOARD_BANNER") != "0":   # run.sh prints its own
        print("\n  Board")
        print(f"  Open    {board_url}")
        print(f"  MCP     http://{HOST}:{PORT}/mcp?t={TOKEN}")
        print(f"  Saving  {BOARDS_DIR}  ({len(store.index())} saved)\n")

    config = uvicorn.Config(gated_app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    if "--app" in sys.argv:
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started and thread.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        if not _open_app_window(board_url):
            thread.join()
    else:
        server.run()
