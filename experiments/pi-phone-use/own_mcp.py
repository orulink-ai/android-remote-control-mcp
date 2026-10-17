"""Local benchmark inspection helper; credentials never enter saved artifacts."""

import argparse
import base64
import json
import os
import re
import time

import httpx

from config import mcp_settings, output_dir

BASE = output_dir()


class McpError(RuntimeError):
    """Safe diagnostic with no raw URLs, headers or response bodies."""

    def __init__(self, message, *, stage, exception_type):
        self.stage = stage
        self.exception_type = exception_type
        super().__init__(f"{message} ({exception_type}; stage={stage})")


class OwnMcp:
    def __init__(self, *, client=None, url=None, headers=None):
        configured_url, configured_headers = (
            mcp_settings() if url is None or headers is None else (url, headers)
        )
        self.url = url if url is not None else configured_url
        self.client = client if client is not None else httpx.Client(timeout=35, trust_env=False)
        self.client.headers.update(
            {
                **(configured_headers if headers is None else headers),
                "Accept": "application/json, text/event-stream",
            }
        )
        self.sequence = 0
        self.session_id = None
        self.closed = False
        self.close_error = None
        stage = "initialize"
        try:
            response = self.client.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "controlled-like-benchmark", "version": "1"},
                    },
                },
            )
            response.raise_for_status()
            self.session_id = response.headers.get("mcp-session-id")
            if self.session_id:
                self.client.headers["Mcp-Session-Id"] = self.session_id
            self.server_info = response.json()["result"]["serverInfo"]
            stage = "initialized"
            response = self.client.post(
                self.url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
            response.raise_for_status()
        except Exception as error:
            self.close()
            raise McpError(
                "MCP initialization failed", stage=stage, exception_type=type(error).__name__
            ) from None

    def call(self, name, arguments):
        if self.closed:
            raise McpError(
                "MCP session is closed", stage="tool_call", exception_type="ClosedSession"
            )
        self.sequence += 1
        try:
            response = self.client.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": self.sequence + 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            response.raise_for_status()
            body = response.json()
            if "error" in body:
                raise McpError(
                    "MCP protocol rejected request",
                    stage="tool_call",
                    exception_type="JsonRpcError",
                )
            result = body["result"]
            if result.get("isError"):
                # Only this specific pre-dispatch guarantee is retained for safe
                # stale-reference recovery. Never echo other server text.
                message = "MCP tool failed"
                if "not found in accessibility tree" in content_text(result):
                    message = "Node not found in accessibility tree"
                raise McpError(message, stage="tool_call", exception_type="ToolError")
            return result
        except McpError:
            raise
        except Exception as error:
            raise McpError(
                "MCP request failed", stage="tool_call", exception_type=type(error).__name__
            ) from None

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.session_id:
                response = self.client.delete(self.url)
                # The server may already have expired the session or not support
                # explicit termination; the underlying connection still closes.
                if response.status_code not in (404, 405):
                    response.raise_for_status()
        except Exception as error:
            self.close_error = {"exceptionType": type(error).__name__, "stage": "close_session"}
        finally:
            self.client.close()


def content_text(result):
    return "\n".join(
        item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"
    )


def own_nodes(text):
    nodes = []
    package = ""
    for line in text.splitlines():
        if line.startswith("--- window:"):
            match = re.search(r"pkg:(\S+)", line)
            package = match.group(1) if match else ""
        parts = line.split("\t")
        if len(parts) != 7 or not re.fullmatch(r"node_[0-9a-f]+", parts[0]):
            continue
        nodes.append(
            {
                "id": parts[0],
                "class": parts[1],
                "text": "" if parts[2] == "-" else parts[2],
                "description": "" if parts[3] == "-" else parts[3],
                "resource_id": "" if parts[4] == "-" else parts[4],
                "bounds": [int(x) for x in parts[5].split(",")],
                "flags": parts[6].split(","),
                "package": package,
            }
        )
    return nodes


def save_private(name, data):
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = BASE / name
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)
    os.chmod(path, 0o600)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["state", "back", "click", "scroll", "open"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("--image", action="store_true")
    parser.add_argument("--tag", default="inspection-latest")
    args = parser.parse_args()
    mcp = OwnMcp()
    try:
        if args.action != "state":
            names = {
                "back": ("android_press_back", {}),
                "click": ("android_click_node", {"node_id": args.value}),
                "scroll": (
                    "android_scroll",
                    {"direction": args.value or "down", "amount": "medium", "variance": 0},
                ),
                "open": ("android_open_app", {"package_id": args.value or "com.xingin.xhs"}),
            }
            name, values = names[args.action]
            started = time.perf_counter()
            mcp.call(name, values)
            print(
                json.dumps(
                    {"action": args.action, "ms": round((time.perf_counter() - started) * 1000, 2)}
                )
            )
            time.sleep(0.7)
        started = time.perf_counter()
        result = mcp.call("android_get_screen_state", {"include_screenshot": args.image})
        print(json.dumps({"observation_ms": round((time.perf_counter() - started) * 1000, 2)}))
        text = content_text(result)
        save_private(args.tag + ".txt", text)
        for item in result.get("content", []):
            if item.get("type") == "image":
                save_private(args.tag + ".jpg", base64.b64decode(item["data"]))
        for node in own_nodes(text):
            if (
                node["package"] == "com.xingin.xhs"
                and "on" in node["flags"]
                and (node["text"] or node["description"] or "clk" in node["flags"])
            ):
                print(json.dumps(node, ensure_ascii=False))
    finally:
        mcp.close()


if __name__ == "__main__":
    main()
