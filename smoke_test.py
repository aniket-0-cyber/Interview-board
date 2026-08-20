"""Exercises the board without Claude, so you can debug in isolation.

    BOARD_HOST_TOKEN=$(cat .token.host) BOARD_TOKEN=$(cat .token) python smoke_test.py

Both tokens are needed: the point of half these checks is that the shared one
cannot reach what the interviewer's one can.
"""

import asyncio
import json
import os
import urllib.request

import websockets

BASE = os.environ.get("BOARD_BASE", "http://127.0.0.1:8765")
HOST_TOKEN = os.environ.get("BOARD_HOST_TOKEN", "")
GUEST_TOKEN = os.environ.get("BOARD_TOKEN", "")
WS = BASE.replace("http", "ws")
_id = [0]

def rpc(method, params):
    _id[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params}).encode()
    req = urllib.request.Request(f"{BASE}/mcp", body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {HOST_TOKEN}",
    })
    raw = urllib.request.urlopen(req).read().decode()
    data = [l for l in raw.splitlines() if l.startswith("data: ")][-1]
    return json.loads(data[6:])["result"]


def tool(name, args=None):
    payload = {"name": name, "arguments": args or {}}
    return rpc("tools/call", payload)["content"][0]["text"]


def call(name, args=None):
    """A tool call whose JSON result you want as a dict."""
    return json.loads(tool(name, args))


async def main():
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "1"}})

    async with websockets.connect(f"{WS}/ws?t={HOST_TOKEN}") as ws:
        await ws.recv()

        async def pushed():
            return json.loads(await asyncio.wait_for(ws.recv(), 3))

        print("new board   ->", tool("board_new", {"name": "Smoke Test — Two Sum"}))
        await pushed()

        doc = call("board_add_doc", {"text": "def two_sum(nums, target):\n    pass\n",
                                     "language": "python", "title": "Two Sum"})
        print("add doc     ->", doc)
        p = await pushed()
        print("             browser got rev", p["board"]["rev"], "from", p["origin"])

        print("patch       ->", tool("board_patch_doc", {
            "id": doc["id"], "find": "    pass", "replace": "    seen = {}\n    return seen"}))
        await pushed()

        print("bad patch   ->", tool("board_patch_doc", {"find": "nope", "replace": "x"}))

        api = call("board_add_box", {"label": "API"})
        db = call("board_add_box", {"label": "Postgres"})
        await pushed(); await pushed()
        print("connect     ->", tool("board_connect", {"source": api["id"], "target": db["id"],
                                                       "label": "write"}))
        await pushed()

        print("diagram     ->", tool("board_draw_diagram", {
            "mermaid": "graph TD; A[scan]-->B[hash lookup];", "title": "Approach"}))
        await pushed()

        # the interviewer's own layer
        secret = call("board_add_doc", {"text": "# model answer: one pass, hash map",
                                        "language": "python", "title": "Model answer",
                                        "private": True})
        await pushed()
        print("private doc ->", secret)
        assert secret["private"] is True

        print("crossing    ->", tool("board_connect", {"source": api["id"], "target": secret["id"]}))
        assert json.loads(tool("board_connect", {"source": api["id"],
                                                 "target": secret["id"]}))["ok"] is False

        tool("board_note", {"text": "O(n) target stated unprompted"})
        await pushed()
        tool("board_rubric", {"label": "Complexity", "verdict": "strong", "note": "no prompting"})
        await pushed()

        # the user types over Claude's document and drags a box
        await ws.send(json.dumps({"type": "user_text", "id": doc["id"],
                                  "text": "# my own attempt\nfor i in range(n):\n    ..."}))
        await pushed()
        await ws.send(json.dumps({"type": "user_move", "id": api["id"], "x": 900, "y": 120}))
        await pushed()

        state = json.loads(tool("board_read", {"response_format": "json"}))
        assert "my own attempt" in state["nodes"][doc["id"]]["text"], "user edit invisible to Claude"
        assert state["nodes"][api["id"]]["x"] == 900, "user drag invisible to Claude"
        assert state["last_editor"] == "user"
        assert len(state["notes"]) == 1 and len(state["rubric"]) == 1
        print("read back   -> Claude sees the user's live edits and drags")

        # ---- what the shared view can and cannot reach -------------------
        async with websockets.connect(f"{WS}/ws?t={GUEST_TOKEN}") as guest:
            shared = json.loads(await guest.recv())["board"]
            assert secret["id"] not in shared["nodes"], "private node reached the shared view"
            assert "notes" not in shared and "rubric" not in shared, "private layer leaked"
            assert shared["host"] is False
            print("shared view ->", len(shared["nodes"]), "of", len(state["nodes"]),
                  "nodes, no notes, no rubric")

            await guest.send(json.dumps({"type": "user_text", "id": secret["id"], "text": "PWNED"}))
            await guest.send(json.dumps({"type": "user_note", "text": "sneaky"}))
            await guest.send(json.dumps({"type": "user_add", "kind": "box",
                                         "text": "sneaky box", "private": True}))
            await asyncio.sleep(0.4)

        after = json.loads(tool("board_read", {"response_format": "json"}))
        assert after["nodes"][secret["id"]]["text"].startswith("# model answer"), \
            "shared view edited a private node"
        assert len(after["notes"]) == 1, "shared view wrote a private note"
        sneaky = [n for n in after["nodes"].values() if n["text"] == "sneaky box"]
        assert sneaky and sneaky[0]["private"] is False, "shared view authored into the private layer"
        print("guards      -> shared view cannot read, edit or author the private layer")

        hidden = json.loads(tool("board_read", {"response_format": "json",
                                                "include_private": False}))
        assert secret["id"] not in hidden["nodes"] and "notes" not in hidden
        print("read shared -> board_read can also stand in the candidate's shoes")

        await asyncio.sleep(2.5)  # let autosave land

        # switch away, then come back to it
        print("second board->", tool("board_new", {"name": "Smoke Test — LRU Cache"}))
        await pushed()
        tool("board_add_doc", {"text": "class LRUCache: ...", "language": "python"})
        await pushed()
        await asyncio.sleep(2.5)

        listed = call("board_list")
        names = [b["name"] for b in listed["boards"]]
        print("list        ->", listed["count"], "boards:", names)
        assert "Smoke Test — Two Sum" in names and "Smoke Test — LRU Cache" in names

        print("load        ->", tool("board_load", {"name": "two sum"}))
        await pushed()
        back = json.loads(tool("board_read", {"response_format": "json"}))
        assert any("my own attempt" in n["text"] for n in back["nodes"].values()), \
            "board did not survive the round trip"
        assert any(n["private"] for n in back["nodes"].values()), "private layer lost on reload"
        assert len(back["notes"]) == 1 and len(back["rubric"]) == 1
        assert any(n["type"] == "diagram" for n in back["nodes"].values())
        assert back["edges"], "lines lost on reload"
        print("             reloaded board still has documents, lines, notes and the private layer")

        print("tidy        ->", tool("board_arrange"))
        await pushed()

        print("ambiguous   ->", tool("board_load", {"name": "smoke test"}))
        print("missing     ->", tool("board_load", {"name": "does-not-exist"}))
        print("unconfirmed ->", tool("board_delete", {"name": "lru", "confirm": False}))
        print("delete open ->", tool("board_delete", {"name": "two sum", "confirm": True}))
        print("delete      ->", tool("board_delete", {"name": "lru cache", "confirm": True}))
        await pushed()

        remaining = [b["name"] for b in call("board_list")["boards"]]
        assert "Smoke Test — LRU Cache" not in remaining
        print("\nPASS: canvas, live sync, the private layer, autosave, reload and delete all work.")


asyncio.run(main())
