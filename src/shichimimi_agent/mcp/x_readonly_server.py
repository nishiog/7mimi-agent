"""MCP protocol (JSON-RPC 2.0, Streamable HTTP) read-only server for the X API.

Implements ADR-015: exposes exactly four read-only tools
(x.search_posts_recent, x.get_posts, x.get_users, x.get_users_by_username).
No write tool is implemented. The X API credential (X_BEARER_TOKEN) is read
from this process's environment only, never forwarded to callers.

stdlib only, no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from shichimimi_agent.hooks.redaction import Redactor
from shichimimi_agent.util.time import iso_now

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "x-mcp-readonly"
SERVER_VERSION = "0.1.0"

DEFAULT_X_API_BASE_URL = "https://api.x.com"

# Default redaction patterns mirror config/policy.yaml redaction_policy so the
# MCP server degrades gracefully even if it cannot load project config.
_DEFAULT_REDACTION_PATTERNS = [
    {"name": "env_assignment", "regex": r"(?i)(api[_-]?key|secret|token|password)\s*="},
]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "x.search_posts_recent",
        "description": "Search recent X posts (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 10, "maximum": 100, "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "x.get_posts",
        "description": "Get X posts by id (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "x.get_users",
        "description": "Get X users by id (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "x.get_users_by_username",
        "description": "Get X users by username (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "usernames": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["usernames"],
        },
    },
]

_TOOL_NAMES = {tool["name"] for tool in TOOLS}


class XApiError(Exception):
    def __init__(self, status: int, title: str) -> None:
        super().__init__(f"X API error {status}: {title}")
        self.status = status
        self.title = title


def _x_api_base_url() -> str:
    return os.environ.get("X_API_BASE_URL", DEFAULT_X_API_BASE_URL).rstrip("/")


def _bearer_token() -> str | None:
    return os.environ.get("X_BEARER_TOKEN")


def _http_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    token = _bearer_token()
    if not token:
        raise RuntimeError("X_BEARER_TOKEN is not configured")
    query = urllib.parse.urlencode(params)
    url = f"{_x_api_base_url()}{path}?{query}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body: dict[str, Any] = {}
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        title = None
        if isinstance(body, dict):
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                title = errors[0].get("title") or errors[0].get("message")
            title = title or body.get("title") or body.get("detail")
        raise XApiError(exc.code, title or "X API request failed") from None
    except urllib.error.URLError as exc:
        raise XApiError(0, str(exc.reason)) from None


def _redactor() -> Redactor:
    return Redactor(_DEFAULT_REDACTION_PATTERNS)


def _post_url(*, post_id: str, username: str | None) -> str:
    handle = username or "i/web"
    return f"https://x.com/{handle}/status/{post_id}"


def _extract_urls(entities: dict[str, Any] | None) -> list[str]:
    if not entities:
        return []
    urls = entities.get("urls") or []
    result = []
    for item in urls:
        expanded = item.get("expanded_url") or item.get("url")
        if expanded:
            result.append(expanded)
    return result


def _normalize_posts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = [data]
    users_by_id: dict[str, dict[str, Any]] = {}
    includes = payload.get("includes") or {}
    for user in includes.get("users") or []:
        if user.get("id"):
            users_by_id[user["id"]] = user

    redactor = _redactor()
    posts = []
    for post in data:
        post_id = post.get("id", "")
        author_id = post.get("author_id")
        author = users_by_id.get(author_id) if author_id else None
        username = author.get("username") if author else None
        metrics = post.get("public_metrics") or {}
        posts.append(
            {
                "id": post_id,
                "url": _post_url(post_id=post_id, username=username),
                "author_handle": username or "",
                "created_at": post.get("created_at", ""),
                "text_redacted": redactor.redact(post.get("text", "")),
                "urls": _extract_urls(post.get("entities")),
                "topics": [],
                "engagement": {
                    "like_count": metrics.get("like_count", 0),
                    "repost_count": metrics.get("retweet_count", 0),
                    "reply_count": metrics.get("reply_count", 0),
                    "quote_count": metrics.get("quote_count", 0),
                },
                "collected_at": iso_now(),
            }
        )
    return posts


def _normalize_users(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = [data]
    users = []
    for user in data:
        metrics = user.get("public_metrics") or {}
        users.append(
            {
                "id": user.get("id", ""),
                "username": user.get("username", ""),
                "name": user.get("name", ""),
                "followers_count": metrics.get("followers_count", 0),
                "following_count": metrics.get("following_count", 0),
            }
        )
    return users


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call. Returns an MCP tools/call result dict.

    Never raises for expected failure modes (missing token, upstream errors) -
    those are surfaced as {"isError": True, ...} results instead.
    """
    if name not in _TOOL_NAMES:
        raise ValueError(f"unknown tool: {name}")

    try:
        if name == "x.search_posts_recent":
            query = arguments.get("query", "")
            max_results = int(arguments.get("max_results", 10))
            payload = _http_get(
                "/2/tweets/search/recent",
                {
                    "query": query,
                    "max_results": max_results,
                    "tweet.fields": "created_at,public_metrics,entities,author_id",
                    "expansions": "author_id",
                    "user.fields": "username",
                },
            )
            result = {"posts": _normalize_posts(payload)}
        elif name == "x.get_posts":
            ids = arguments.get("ids") or []
            payload = _http_get(
                "/2/tweets",
                {
                    "ids": ",".join(ids),
                    "tweet.fields": "created_at,public_metrics,entities,author_id",
                    "expansions": "author_id",
                    "user.fields": "username",
                },
            )
            result = {"posts": _normalize_posts(payload)}
        elif name == "x.get_users":
            ids = arguments.get("ids") or []
            payload = _http_get("/2/users", {"ids": ",".join(ids), "user.fields": "username,name,public_metrics"})
            result = {"users": _normalize_users(payload)}
        elif name == "x.get_users_by_username":
            usernames = arguments.get("usernames") or []
            payload = _http_get(
                "/2/users/by", {"usernames": ",".join(usernames), "user.fields": "username,name,public_metrics"}
            )
            result = {"users": _normalize_users(payload)}
        else:  # pragma: no cover - guarded above
            raise ValueError(f"unknown tool: {name}")
    except RuntimeError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except XApiError as exc:
        return {
            "content": [{"type": "text", "text": f"X API error (status={exc.status}): {exc.title}"}],
            "isError": True,
        }

    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_jsonrpc(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle a single JSON-RPC request/notification. Returns None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in _TOOL_NAMES:
            return _jsonrpc_error(request_id, -32602, f"unknown or unsupported tool: {name}")
        result = call_tool(name, arguments)
        return _jsonrpc_result(request_id, result)

    return _jsonrpc_error(request_id, -32601, f"unknown method: {method}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "XMcpReadonly/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence default logging
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        if self.path != "/mcp":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            message = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(200, _jsonrpc_error(None, -32700, "parse error"))
            return
        response = handle_jsonrpc(message)
        if response is None:
            self._send_json(204, None)
            return
        self._send_json(200, response)

    def _send_json(self, status: int, payload: dict[str, Any] | None) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def run_server(host: str = "127.0.0.1", port: int = 18082) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    return server


def serve_forever(host: str = "127.0.0.1", port: int = 18082) -> None:
    server = run_server(host=host, port=port)
    print(f"x-mcp-readonly listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
