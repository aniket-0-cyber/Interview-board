"""
HTTP twin of test.py — same tools, same JSON-RPC messages, but listening on a
real network port instead of stdin/stdout, so a Cloudflare tunnel has
something to forward to.

Run:   python test_http.py
Then:  cloudflared tunnel --url http://localhost:8787
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8787"))

# Optional shared-secret check. Leave TEST_TOKEN unset while you're just
# poking at this yourself; set it before you actually put the tunnel URL
# anywhere else, since read_file/write_file let a caller touch any path on
# this machine.
TOKEN = os.environ.get("TEST_TOKEN", "")


def handle_read_file(args):
    path = args.get("path")
    if not path or not os.path.exists(path):
        return f"Error: File '{path}' not found."
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"


def handle_write_file(args):
    path = args.get("path")
    content = args.get("content", "")
    if not path:
        return "Error: Path is required."
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to '{path}'"
    except Exception as e:
        return f"Error writing file: {str(e)}"


TOOLS = [
    {
        "name": "read_file",
        "description": "Read the text contents of a file at the specified path.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute or relative path to the file"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write or overwrite text content to a file at the specified path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file"},
                "content": {"type": "string", "description": "Text content to write into the file"},
            },
            "required": ["path", "content"],
        },
    },
]


def handle_rpc(req):
    """Same branch logic as test.py's main loop, just returning the reply
    instead of writing it to stdout directly. Returns None for a
    notification (no id), meaning: send nothing back."""
    method = req.get("method")
    req_id = req.get("id")

    if req_id is None and method != "tools/call":
        # A message with no id is a notification (e.g. notifications/initialized) —
        # the spec says don't reply to these at all.
        pass

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "scratch-file-server", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "read_file":
            output_text = handle_read_file(arguments)
        elif name == "write_file":
            output_text = handle_write_file(arguments)
        else:
            output_text = f"Error: Unknown tool '{name}'"
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": output_text}]},
        }

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


class Handler(BaseHTTPRequestHandler):
    def _authorized(self):
        if not TOKEN:
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {TOKEN}"

    def do_POST(self):
        if self.path.split("?")[0] != "/mcp":
            self.send_response(404)
            self.end_headers()
            return
        if not self._authorized():
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        reply = handle_rpc(req)

        if reply is None:
            # Notification: nothing to send back, but still need a response line.
            self.send_response(202)
            self.end_headers()
            return

        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet; comment this out if you want request logs


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on http://0.0.0.0:{PORT}/mcp")
    if TOKEN:
        print("Requiring Authorization: Bearer <TEST_TOKEN>")
    else:
        print("No token set — anyone who gets the tunnel URL can call these tools. "
              "Set TEST_TOKEN before sharing the tunnel URL anywhere.")
    server.serve_forever()


if __name__ == "__main__":
    main()
