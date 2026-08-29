"""Exercises the board without Claude, so you can debug in isolation.

    BOARD_TOKEN=$(cat .token) python smoke_test.py
"""

import asyncio
import json
import os
import urllib.request

import websockets

BASE = os.environ.get("BOARD_BASE", "http://127.0.0.1:8765")
TOKEN = os.environ.get("BOARD_TOKEN", "")
WS = BASE.replace("http", "ws")
_id = [0]


def rpc(method, params):
    _id[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params}).encode()
    req = urllib.request.Request(f"{BASE}/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {TOKEN}",
    })
    raw = urllib.request.urlopen(req).read().decode()
    data = [l for l in raw.splitlines() if l.startswith("data: ")][-1]
    return json.loads(data[6:])["result"]


def tool(name, args=None):
    return rpc("tools/call", {"name": name, "arguments": args or {}})["content"][0]["text"]


def call(name, args=None):
    return json.loads(tool(name, args))


async def main():
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "1"}})

    async with websockets.connect(f"{WS}/ws?t={TOKEN}") as ws:
        await ws.recv()

        async def pushed():
            return json.loads(await asyncio.wait_for(ws.recv(), 3))

        print("new board   ->", tool("board_new", {"name": "Smoke Test — Canvas"}))
        await pushed()

        # ---- one call builds a whole diagram ----------------------------
        built = call("board_apply", {
            "nodes": [
                {"key": "client", "kind": "box", "text": "Client", "fill": "blue"},
                {"key": "api", "kind": "box", "text": "API", "fill": "green"},
                {"key": "db", "kind": "box", "text": "Postgres", "fill": "orange"},
                {"key": "cache", "kind": "box", "text": "Redis", "fill": "red"},
                {"key": "note", "kind": "sticky", "text": "Cache invalidation?", "fill": "yellow"},
            ],
            "edges": [
                {"from": "client", "to": "api", "text": "https"},
                {"from": "api", "to": "db", "text": "writes"},
                {"from": "api", "to": "cache", "text": "reads", "style": "dashed", "color": "red"},
            ],
            "layout": "layered",
        })
        print("batch build ->", {k: built[k] for k in ("ok", "nodes", "edges")})
        assert built["ok"] and built["nodes"] == 5 and built["edges"] == 3
        await pushed()
        ids = built["ids"]

        # layered layout should put client left of api left of db
        state = json.loads(tool("board_read", {"response_format": "json"}))
        xs = {k: state["nodes"][v]["x"] for k, v in ids.items()}
        assert xs["client"] < xs["api"] < xs["db"], f"layered layout wrong: {xs}"
        print("layout      -> client < api < db, so it followed the arrows")

        # ---- styling, singly and in bulk --------------------------------
        print("style one   ->", tool("board_update", {"id": ids["api"], "fill": "purple", "bold": True}))
        await pushed()
        print("style many  ->", tool("board_update",
                                     {"ids": [ids["db"], ids["cache"]], "stroke": "grey"}))
        await pushed()
        after = json.loads(tool("board_read", {"response_format": "json"}))
        assert after["nodes"][ids["api"]]["fill"] == "purple"
        assert after["nodes"][ids["api"]]["bold"] is True
        assert all(after["nodes"][ids[k]]["stroke"] == "grey" for k in ("db", "cache"))
        print("             colours and weight stuck")

        print("bad colour  ->", tool("board_apply",
                                     {"nodes": [{"key": "x", "text": "no", "fill": "chartreuse"}]}))
        assert json.loads(tool("board_apply",
                               {"nodes": [{"key": "x", "text": "no", "fill": "chartreuse"}]}))["ok"] is False

        # ---- undo / redo -------------------------------------------------
        before_rev = after["rev"]
        doc = call("board_add_doc", {"text": "def f():\n    pass", "title": "Sketch"})
        await pushed()
        assert doc["id"] in json.loads(tool("board_read", {"response_format": "json"}))["nodes"]

        print("undo        ->", tool("board_undo"))
        await pushed()
        undone = json.loads(tool("board_read", {"response_format": "json"}))
        assert doc["id"] not in undone["nodes"], "undo didn't remove the document"
        assert undone["can_redo"] is True

        print("redo        ->", tool("board_redo"))
        await pushed()
        redone = json.loads(tool("board_read", {"response_format": "json"}))
        assert doc["id"] in redone["nodes"], "redo didn't put it back"
        print("             history works both ways")

        # ---- the page's own edits reach Claude ---------------------------
        await ws.send(json.dumps({"type": "user_move",
                                  "moves": [{"id": ids["client"], "x": 900, "y": 120}]}))
        await pushed()
        await ws.send(json.dumps({"type": "user_style", "ids": [ids["client"]], "fill": "pink"}))
        await pushed()
        await ws.send(json.dumps({"type": "user_text", "id": ids["note"], "text": "typed here"}))
        await pushed()

        live = json.loads(tool("board_read", {"response_format": "json"}))
        assert live["nodes"][ids["client"]]["x"] == 900, "drag invisible to Claude"
        assert live["nodes"][ids["client"]]["fill"] == "pink", "restyle invisible to Claude"
        assert live["nodes"][ids["note"]]["text"] == "typed here", "typing invisible to Claude"
        assert live["last_editor"] == "user"
        print("read back   -> Claude sees drags, restyles and typing")

        # ---- one board for everyone --------------------------------------
        async with websockets.connect(f"{WS}/ws?t={TOKEN}") as second:
            theirs = json.loads(await second.recv())["board"]
            assert set(theirs["nodes"]) == set(live["nodes"]), "second viewer saw a different board"
            assert "notes" not in theirs and "rubric" not in theirs
            await second.send(json.dumps({"type": "user_style",
                                          "ids": [ids["db"]], "fill": "green"}))
            await asyncio.sleep(0.3)
        mine = json.loads(tool("board_read", {"response_format": "json"}))
        assert mine["nodes"][ids["db"]]["fill"] == "green", "second viewer's edit didn't land"
        print("two viewers -> same board, edits from either side land")

        await asyncio.sleep(2.5)  # let autosave happen

        print("second board->", tool("board_new", {"name": "Smoke Test — Empty"}))
        await pushed()
        listed = call("board_list")
        assert "Smoke Test — Canvas" in [b["name"] for b in listed["boards"]]

        print("load        ->", tool("board_load", {"name": "canvas"}))
        await pushed()
        back = json.loads(tool("board_read", {"response_format": "json"}))
        assert len(back["nodes"]) >= 5 and back["edges"], "board didn't survive the round trip"
        assert any(n["fill"] == "pink" for n in back["nodes"].values()), "styling lost on reload"
        print("             reloaded with its nodes, lines and colours")

        print("tidy        ->", tool("board_arrange", {"layout": "grid"}))
        await pushed()
        print("delete      ->", tool("board_delete", {"name": "empty", "confirm": True}))
        await pushed()

        print("\nPASS: batch build, layout, styling, undo/redo, live sync and reload all work.")


asyncio.run(main())
