from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shichimimi_agent.mcp.x_readonly_server import run_server


class _StubXApiHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/2/tweets/search/recent"):
            body = {
                "data": [
                    {
                        "id": "1234",
                        "author_id": "u1",
                        "created_at": "2026-07-01T00:00:00.000Z",
                        "text": "hello world api_key=shouldredact",
                        "public_metrics": {"like_count": 5, "retweet_count": 1, "reply_count": 0, "quote_count": 0},
                        "entities": {"urls": [{"expanded_url": "https://example.com"}]},
                    }
                ],
                "includes": {"users": [{"id": "u1", "username": "alice"}]},
            }
            self._send(200, body)
            return
        if self.path.startswith("/2/users"):
            self._send(401, {"errors": [{"title": "Unauthorized"}]})
            return
        self._send(404, {"errors": [{"title": "not found"}]})

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class XMcpReadonlyServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stub_server = ThreadingHTTPServer(("127.0.0.1", 0), _StubXApiHandler)
        cls.stub_port = cls.stub_server.server_address[1]
        cls.stub_thread = threading.Thread(target=cls.stub_server.serve_forever, daemon=True)
        cls.stub_thread.start()

        os.environ["X_API_BASE_URL"] = f"http://127.0.0.1:{cls.stub_port}"

        cls.mcp_server = run_server(host="127.0.0.1", port=0)
        cls.mcp_port = cls.mcp_server.server_address[1]
        cls.mcp_thread = threading.Thread(target=cls.mcp_server.serve_forever, daemon=True)
        cls.mcp_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mcp_server.shutdown()
        cls.mcp_server.server_close()
        cls.stub_server.shutdown()
        cls.stub_server.server_close()
        os.environ.pop("X_API_BASE_URL", None)

    def setUp(self) -> None:
        os.environ.pop("X_BEARER_TOKEN", None)

    def _rpc(self, method: str, params: dict | None = None, request_id: int = 1) -> dict | None:
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.mcp_port}/mcp",
            data=json.dumps(message).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def test_healthz(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.mcp_port}/healthz", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(body, {"status": "ok"})

    def test_initialize_handshake(self) -> None:
        response = self._rpc("initialize")
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"]["name"], "x-mcp-readonly")
        # notification should not produce an HTTP body error
        notif = self._rpc("notifications/initialized")
        self.assertIsNone(notif)

    def test_tools_list_has_exactly_four_readonly_tools(self) -> None:
        response = self._rpc("tools/list")
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {"x.search_posts_recent", "x.get_posts", "x.get_users", "x.get_users_by_username"},
        )

    def test_tools_call_search_happy_path(self) -> None:
        os.environ["X_BEARER_TOKEN"] = "test-token"
        response = self._rpc(
            "tools/call",
            {"name": "x.search_posts_recent", "arguments": {"query": "MCP", "max_results": 10}},
        )
        result = response["result"]
        self.assertNotIn("isError", result)
        payload = json.loads(result["content"][0]["text"])
        posts = payload["posts"]
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["id"], "1234")
        self.assertEqual(post["url"], "https://x.com/alice/status/1234")
        self.assertEqual(post["author_handle"], "alice")
        self.assertEqual(post["engagement"]["like_count"], 5)
        self.assertIn("[REDACTED:", post["text_redacted"])
        self.assertNotIn("api_key=", post["text_redacted"])

    def test_write_tool_rejected(self) -> None:
        response = self._rpc(
            "tools/call",
            {"name": "x.create_post", "arguments": {"text": "hi"}},
        )
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32602)

    def test_missing_bearer_token_is_error_without_leaking_env(self) -> None:
        response = self._rpc(
            "tools/call",
            {"name": "x.search_posts_recent", "arguments": {"query": "MCP"}},
        )
        result = response["result"]
        self.assertTrue(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertNotIn("test-token", text)
        self.assertIn("X_BEARER_TOKEN", text)

    def test_upstream_401_is_error_with_status_no_token_leak(self) -> None:
        os.environ["X_BEARER_TOKEN"] = "super-secret-token"
        response = self._rpc(
            "tools/call",
            {"name": "x.get_users", "arguments": {"ids": ["1"]}},
        )
        result = response["result"]
        self.assertTrue(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertNotIn("super-secret-token", text)
        self.assertIn("401", text)

    def test_unknown_method(self) -> None:
        response = self._rpc("not/a/method")
        self.assertEqual(response["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
