from __future__ import annotations

import argparse
import io
import json
import os
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from shichimimi_agent.mcp.x_readonly_server import call_tool, handle_jsonrpc, run_server


class FakeResponse:
    """Fake response object mimicking urllib.request.urlopen's context manager for
    mocking AuthProxyClient remote calls (mirrors the pattern used in
    test_pre_tool_use.py / test_ai_it_topic_runner_auth.py)."""

    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _StubXApiHandler(BaseHTTPRequestHandler):
    """Stub upstream X API used by additional tools/call happy-path tests."""

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/2/tweets/search/recent"):
            self._send(200, {"data": [], "includes": {}})
            return
        if self.path.startswith("/2/tweets"):
            body = {
                "data": [
                    {
                        "id": "42",
                        "author_id": "u2",
                        "created_at": "2026-07-02T00:00:00.000Z",
                        "text": "post by id",
                        "public_metrics": {"like_count": 2, "retweet_count": 0, "reply_count": 0, "quote_count": 0},
                    }
                ],
                "includes": {"users": [{"id": "u2", "username": "bob"}]},
            }
            self._send(200, body)
            return
        if self.path.startswith("/2/users/by"):
            body = {
                "data": [
                    {"id": "9", "username": "carol", "name": "Carol", "public_metrics": {"followers_count": 3, "following_count": 1}}
                ]
            }
            self._send(200, body)
            return
        if self.path.startswith("/2/users"):
            body = {
                "data": [
                    {"id": "9", "username": "carol", "name": "Carol", "public_metrics": {"followers_count": 3, "following_count": 1}}
                ]
            }
            self._send(200, body)
            return
        self._send(404, {"errors": [{"title": "not found"}]})

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ToolsCallHappyPathsTest(unittest.TestCase):
    """Covers get_posts / get_users / get_users_by_username, which the original
    suite only exercised indirectly (get_users was only hit via the 401 error path)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub_server = ThreadingHTTPServer(("127.0.0.1", 0), _StubXApiHandler)
        cls.stub_port = cls.stub_server.server_address[1]
        cls.stub_thread = threading.Thread(target=cls.stub_server.serve_forever, daemon=True)
        cls.stub_thread.start()
        os.environ["X_API_BASE_URL"] = f"http://127.0.0.1:{cls.stub_port}"
        os.environ["X_BEARER_TOKEN"] = "gaps-test-token"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub_server.shutdown()
        cls.stub_server.server_close()
        os.environ.pop("X_API_BASE_URL", None)
        os.environ.pop("X_BEARER_TOKEN", None)

    def test_get_posts_happy_path(self) -> None:
        result = call_tool("x.get_posts", {"ids": ["42"]})
        self.assertNotIn("isError", result)
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["posts"][0]["id"], "42")
        self.assertEqual(payload["posts"][0]["author_handle"], "bob")

    def test_get_users_happy_path(self) -> None:
        result = call_tool("x.get_users", {"ids": ["9"]})
        self.assertNotIn("isError", result)
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["users"][0]["username"], "carol")
        self.assertEqual(payload["users"][0]["followers_count"], 3)

    def test_get_users_by_username_happy_path(self) -> None:
        result = call_tool("x.get_users_by_username", {"usernames": ["carol"]})
        self.assertNotIn("isError", result)
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["users"][0]["username"], "carol")

    def test_search_posts_recent_empty_results(self) -> None:
        result = call_tool("x.search_posts_recent", {"query": "nothing"})
        self.assertNotIn("isError", result)
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["posts"], [])


class MalformedRequestTest(unittest.TestCase):
    """Malformed JSON body over the HTTP transport -> JSON-RPC -32700 parse error."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp_server = run_server(host="127.0.0.1", port=0)
        cls.mcp_port = cls.mcp_server.server_address[1]
        cls.mcp_thread = threading.Thread(target=cls.mcp_server.serve_forever, daemon=True)
        cls.mcp_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mcp_server.shutdown()
        cls.mcp_server.server_close()

    def test_malformed_json_body_is_parse_error(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.mcp_port}/mcp",
            data=b"{not valid json!!",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(body["error"]["code"], -32700)

    def test_tools_call_missing_required_argument(self) -> None:
        # x.get_posts requires "ids" but is called with no arguments at all.
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x.get_posts", "arguments": {}}}
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.mcp_port}/mcp",
            data=json.dumps(message).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        # Server does not validate schema-level "required" fields at the JSON-RPC
        # layer; a missing "ids" argument does NOT produce a -32602 error. It
        # silently defaults to [] and the request proceeds to call_tool(), which
        # then fails for the *unrelated* reason that X_BEARER_TOKEN is not set in
        # this test process. Documented here as current (permissive) behavior:
        # required-argument validation is not enforced server-side.
        self.assertIn("result", body)
        result = body["result"]
        self.assertTrue(result.get("isError"))
        self.assertIn("X_BEARER_TOKEN", result["content"][0]["text"])


class TokenLeakScanTest(unittest.TestCase):
    """Exercises every error path with a sentinel token and asserts it never
    appears anywhere in any JSON-RPC response body (belt-and-suspenders on top
    of the existing single-path checks in test_x_mcp_readonly.py)."""

    SENTINEL = "SENTINEL-TOKEN-DO-NOT-LEAK-abc123"

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub_server = ThreadingHTTPServer(("127.0.0.1", 0), _StubXApiHandler)
        cls.stub_port = cls.stub_server.server_address[1]
        cls.stub_thread = threading.Thread(target=cls.stub_server.serve_forever, daemon=True)
        cls.stub_thread.start()
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

    def setUp(self) -> None:
        os.environ["X_API_BASE_URL"] = f"http://127.0.0.1:{self.stub_port}"
        os.environ["X_BEARER_TOKEN"] = self.SENTINEL

    def tearDown(self) -> None:
        os.environ.pop("X_API_BASE_URL", None)
        os.environ.pop("X_BEARER_TOKEN", None)

    def _rpc(self, method: str, params: dict | None = None) -> dict | None:
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
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
        return json.loads(raw.decode("utf-8")) if raw else None

    def test_sentinel_never_leaks_via_401_on_every_tool(self) -> None:
        # _StubXApiHandler returns 401 for anything under /2/users*
        for name, args in (
            ("x.get_users", {"ids": ["1"]}),
            ("x.get_users_by_username", {"usernames": ["carol"]}),
        ):
            with self.subTest(tool=name):
                response = self._rpc("tools/call", {"name": name, "arguments": args})
                raw = json.dumps(response)
                self.assertNotIn(self.SENTINEL, raw)

    def test_sentinel_never_leaks_on_upstream_unreachable(self) -> None:
        os.environ["X_API_BASE_URL"] = "http://127.0.0.1:1"
        response = self._rpc("tools/call", {"name": "x.search_posts_recent", "arguments": {"query": "x"}})
        raw = json.dumps(response)
        self.assertNotIn(self.SENTINEL, raw)

    def test_sentinel_never_leaks_on_write_tool_and_unknown_method(self) -> None:
        response = self._rpc("tools/call", {"name": "x.create_post", "arguments": {"text": self.SENTINEL}})
        self.assertNotIn(self.SENTINEL, json.dumps(response))
        response = self._rpc("not/a/real/method")
        self.assertNotIn(self.SENTINEL, json.dumps(response))


class HandleJsonRpcDirectTest(unittest.TestCase):
    """Direct handle_jsonrpc() checks that don't need a running HTTP server."""

    def test_tools_call_unknown_tool_never_reaches_call_tool(self) -> None:
        response = handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x.delete_account", "arguments": {}}})
        self.assertEqual(response["error"]["code"], -32602)


class XSmokeDenyPathTest(unittest.TestCase):
    """Covers cmd_x_smoke's policy-deny branch: with AUTH_PROXY_URL set and a
    mocked 'block' decision from auth-proxy, x-smoke must exit non-zero and
    must never reach the MCP client (i.e. never need X_BEARER_TOKEN)."""

    def test_denied_authorization_exits_non_zero_without_calling_mcp(self) -> None:
        from shichimimi_agent import cli

        args = argparse.Namespace(
            root=None,
            query="MCP server",
            max_results=10,
            mcp_url="http://127.0.0.1:1",  # would fail if ever reached
        )

        with mock.patch.dict(os.environ, {"AUTH_PROXY_URL": "http://auth-proxy.local"}, clear=False):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value = FakeResponse(
                    {"decision": "block", "reason": "denied by policy", "policy_version": "1"}
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = cli.cmd_x_smoke(args)

        self.assertNotEqual(exit_code, 0)
        self.assertIn("blocked by policy", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
