"""Cross-language contract parity test (ADR-023, Issue #15).

Builds the real Go auth-proxy binary (services/auth-proxy), starts it on an
ephemeral port with X_BEARER_TOKEN set and X_API_BASE_URL pointed at a local
stub HTTP server that mimics the X API v2 /2/tweets/search/recent response
(with a users expansion), then drives it with the REAL Python MCP client
(shichimimi_agent.mcp.client.McpHttpClient) end to end:

    initialize -> notifications/initialized -> tools/list (4 tools)
    -> tools/call x.search_posts_recent (normalized post fields)

This proves the Go xmcp server satisfies the Python client's contract, not
just that each side's own unit tests pass in isolation.

Also scans a 401 upstream error path for token leakage through the real client.

Skipped entirely if the `go` toolchain is not available.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from shichimimi_agent.mcp.client import McpClientError, McpHttpClient

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH_PROXY_DIR = REPO_ROOT / "services" / "auth-proxy"

SENTINEL_TOKEN = "sentinel-x-bearer-do-not-leak"
SESSION_TOKEN = "sentinel-x-mcp-session-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:  # noqa: PERF203
            last_err = exc
            time.sleep(0.05)
    raise RuntimeError(f"server on {host}:{port} did not start in time: {last_err}")


class _StubXAPIHandler(http.server.BaseHTTPRequestHandler):
    """Mimics X API v2 search endpoint with a users expansion, plus a
    dedicated path that always returns 401 (for the token-leak sentinel
    scan on error paths)."""

    server_version = "StubXAPI/1.0"

    def log_message(self, *args: Any) -> None:  # silence default stderr logging
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/2/tweets/search/recent") and "TRIGGER_401" in self.path:
            body = json.dumps({"errors": [{"title": "Unauthorized"}]}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/2/tweets/search/recent"):
            body = json.dumps(
                {
                    "data": [
                        {
                            "id": "1001",
                            "text": "AI ops news: check https://example.com",
                            "author_id": "u1",
                            "created_at": "2026-07-05T00:00:00.000Z",
                            "public_metrics": {
                                "like_count": 5,
                                "retweet_count": 2,
                                "reply_count": 1,
                                "quote_count": 0,
                            },
                            "entities": {
                                "urls": [{"expanded_url": "https://example.com", "url": "https://t.co/x"}]
                            },
                        }
                    ],
                    "includes": {"users": [{"id": "u1", "username": "alice_ai"}]},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/2/unauthorized"):
            body = json.dumps({"errors": [{"title": "Unauthorized"}]}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


@unittest.skipUnless(shutil.which("go"), "go toolchain not available")
class GoXMcpIntegrationTest(unittest.TestCase):
    """Class-scoped: build the Go binary once, start stub + auth-proxy once,
    reuse across test methods to keep runtime modest."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.binary_path = Path(cls._tmpdir.name) / "auth-proxy-under-test"

        build = subprocess.run(
            ["go", "build", "-o", str(cls.binary_path), "./cmd/auth-proxy"],
            cwd=AUTH_PROXY_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if build.returncode != 0:
            raise RuntimeError(f"go build failed: {build.stdout}\n{build.stderr}")

        # Stub X API server (real X API is never contacted).
        cls.stub_port = _free_port()
        cls.stub_server = http.server.ThreadingHTTPServer(("127.0.0.1", cls.stub_port), _StubXAPIHandler)
        cls.stub_thread = threading.Thread(target=cls.stub_server.serve_forever, daemon=True)
        cls.stub_thread.start()

        # auth-proxy (Go), with xmcp mounted (X_BEARER_TOKEN set).
        cls.proxy_port = _free_port()
        env = dict(os.environ)
        env["AUTH_PROXY_ADDR"] = f"127.0.0.1:{cls.proxy_port}"
        env["X_BEARER_TOKEN"] = SENTINEL_TOKEN
        env["X_API_BASE_URL"] = f"http://127.0.0.1:{cls.stub_port}"
        # /mcp requires the same session Bearer as gitrelay (ADR-023); git
        # relay itself stays disabled here (no GitHub App credentials), which
        # is irrelevant to this contract test and keeps startup deterministic.
        env["AUTH_PROXY_SESSION_TOKEN"] = SESSION_TOKEN

        cls.proxy_process = subprocess.Popen(
            [str(cls.binary_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_port("127.0.0.1", cls.proxy_port, timeout=10.0)
        except Exception:
            cls.proxy_process.terminate()
            raise

        cls.base_url = f"http://127.0.0.1:{cls.proxy_port}"
        cls.client = McpHttpClient(base_url=cls.base_url, session_token=SESSION_TOKEN)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy_process.terminate()
        try:
            cls.proxy_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proxy_process.kill()
        cls.stub_server.shutdown()
        cls.stub_thread.join(timeout=5)
        cls._tmpdir.cleanup()

    # -- 2. real Python client end-to-end against the real Go server --

    def test_initialize_list_tools_and_search_posts_recent(self) -> None:
        init_result = self.client.initialize()
        self.assertEqual(init_result["protocolVersion"], "2025-03-26")

        tools = self.client.list_tools()
        names = {t["name"] for t in tools}
        self.assertEqual(
            names,
            {
                "x.search_posts_recent",
                "x.get_posts",
                "x.get_users",
                "x.get_users_by_username",
            },
        )

        result = self.client.call_tool("x.search_posts_recent", {"query": "AI OR IT", "max_results": 10})
        self.assertFalse(result.get("isError", False))
        payload = json.loads(result["content"][0]["text"])
        posts = payload["posts"]
        self.assertEqual(len(posts), 1)
        post = posts[0]

        self.assertEqual(post["id"], "1001")
        self.assertEqual(post["url"], "https://x.com/alice_ai/status/1001")
        self.assertEqual(post["author_handle"], "alice_ai")
        self.assertIn("text_redacted", post)
        self.assertNotIn("[REDACTED", post["text_redacted"])  # no secret-shaped text present
        self.assertEqual(
            post["engagement"],
            {"like_count": 5, "repost_count": 2, "reply_count": 1, "quote_count": 0},
        )
        self.assertIn("https://example.com", post["urls"])

    # -- 4. token-leak sentinel scan on error paths via the real client --

    def test_401_error_path_does_not_leak_bearer_token(self) -> None:
        # The stub returns a genuine 401 for this sentinel query; the
        # xmcp handler surfaces it as an isError tool result. Assert the
        # real bearer token never appears in that text, via the real
        # Python client (not just Go-side unit assertions).
        result = self.client.call_tool("x.search_posts_recent", {"query": "TRIGGER_401", "max_results": 10})
        self.assertTrue(result.get("isError", False))
        text = result["content"][0]["text"]
        self.assertNotIn(SENTINEL_TOKEN, text)
        self.assertIn("X API error", text)
        self.assertIn("401", text)

    def test_unregistered_upstream_path_error_does_not_leak_bearer_token(self) -> None:
        # x.get_users hits a stub path (/2/users) that falls through to a
        # plain 404 with no JSON body; confirms the generic error branch
        # also never leaks the token.
        result = self.client.call_tool("x.get_users", {"ids": ["u1"]})
        self.assertTrue(result.get("isError", False))
        text = result["content"][0]["text"]
        self.assertNotIn(SENTINEL_TOKEN, text)

    # -- 5. wrong session Bearer is rejected before JSON-RPC handling --

    def test_wrong_session_bearer_returns_401(self) -> None:
        # McpHttpClient wraps HTTPError as McpClientError; drive this with raw
        # urllib instead so the assertion is against the real 401 status the
        # xmcp handler returns for a bad session token (ADR-023 gate), not
        # just the client's error-wrapping behavior.
        request = urllib.request.Request(
            f"{self.base_url}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer wrong-session-token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
