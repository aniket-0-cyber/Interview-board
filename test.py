import sys
import json
import os

def send_response(response):
    """Encodes JSON and flushes directly to stdout so the client receives it immediately."""
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()

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
        # Create directory structure if it doesn't exist
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to '{path}'"
    except Exception as e:
        return f"Error writing file: {str(e)}"

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method")
        req_id = req.get("id")

        # 1. Handshake: Client initializes the connection
        if method == "initialize":
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "scratch-file-server", "version": "1.0.0"}
                }
            })

        # 2. Handshake Acknowledgment (Notification - no response expected)
        elif method == "notifications/initialized":
            pass

        # 3. Tool Discovery: Return list of supported tools and their JSON schemas
        elif method == "tools/list":
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read the text contents of a file at the specified path.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Absolute or relative path to the file"}
                                },
                                "required": ["path"]
                            }
                        },
                        {
                            "name": "write_file",
                            "description": "Write or overwrite text content to a file at the specified path.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Absolute or relative path to the file"},
                                    "content": {"type": "string", "description": "Text content to write into the file"}
                                },
                                "required": ["path", "content"]
                            }
                        }
                    ]
                }
            })

        # 4. Tool Execution: Client calls one of our tools
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})

            if name == "read_file":
                output_text = handle_read_file(arguments)
            elif name == "write_file":
                output_text = handle_write_file(arguments)
            else:
                output_text = f"Error: Unknown tool '{name}'"

            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": output_text
                        }
                    ]
                }
            })

if __name__ == "__main__":
    main()