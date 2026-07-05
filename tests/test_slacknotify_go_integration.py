"""Cross-language contract parity test for /v1/slack/notify (ADR-026, Issue #19).

Builds the real Go auth-proxy binary, starts it with AUTH_PROXY_SESSION_TOKEN,
SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, and SLACK_API_BASE_URL pointed at a local
stub Slack Web API server, then drives it with the REAL Python client
(shichimimi_agent.proxies.slack_notify_client.SlackNotifyClient) end to end.
This proves the Go slacknotify handler satisfies the Python client's
contract, not just that each side's own unit tests pass in isolation.

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

from shichimimi_agent.proxies.slack_notify_client import SlackNotifyClient, SlackNotifyError

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH_PROXY_DIR = REPO_ROOT / "services" / "auth-proxy"

SESSION_TOKEN = "sentinel-slack-notify-session-token"
BOT_TOKEN = "sentinel-slack-bot-token"
CHANNEL_ID = "C0SENTINEL01"


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


class _StubSlackAPIHandler(http.server.BaseHTTPRequestHandler):
    """Mimics the Slack Web API's chat.postMessage: records every posted
    chunk (in arrival order) on the class, and always replies {"ok": true}.
    The real Slack API is never contacted."""

    server_version = "StubSlackAPI/1.0"
    received: list[str] = []
    lock = threading.Lock()

    def log_message(self, *args: Any) -> None:  # silence default stderr logging
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8"))
        with self.lock:
            self.__class__.received.append(payload["text"])
        resp = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _build_auth_proxy(binary_path: Path) -> None:
    build = subprocess.run(
        ["go", "build", "-o", str(binary_path), "./cmd/auth-proxy"],
        cwd=AUTH_PROXY_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if build.returncode != 0:
        raise RuntimeError(f"go build failed: {build.stdout}\n{build.stderr}")


@unittest.skipUnless(shutil.which("go"), "go toolchain not available")
class GoSlackNotifyIntegrationTest(unittest.TestCase):
    """Class-scoped: build the Go binary once, start stub Slack API +
    auth-proxy once (with SLACK_BOT_TOKEN/SLACK_CHANNEL_ID/
    AUTH_PROXY_SESSION_TOKEN set), reuse across test methods."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.binary_path = Path(cls._tmpdir.name) / "auth-proxy-under-test"
        _build_auth_proxy(cls.binary_path)

        _StubSlackAPIHandler.received = []
        cls.stub_port = _free_port()
        cls.stub_server = http.server.ThreadingHTTPServer(("127.0.0.1", cls.stub_port), _StubSlackAPIHandler)
        cls.stub_thread = threading.Thread(target=cls.stub_server.serve_forever, daemon=True)
        cls.stub_thread.start()

        cls.proxy_port = _free_port()
        env = dict(os.environ)
        env["AUTH_PROXY_ADDR"] = f"127.0.0.1:{cls.proxy_port}"
        env["AUTH_PROXY_SESSION_TOKEN"] = SESSION_TOKEN
        env["SLACK_BOT_TOKEN"] = BOT_TOKEN
        env["SLACK_CHANNEL_ID"] = CHANNEL_ID
        env["SLACK_API_BASE_URL"] = f"http://127.0.0.1:{cls.stub_port}"
        # Keep other mounts (xmcp, gitrelay) deterministically disabled: no
        # X_BEARER_TOKEN / GitHub App creds set, irrelevant to this test.

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

    def setUp(self) -> None:
        with _StubSlackAPIHandler.lock:
            _StubSlackAPIHandler.received.clear()

    # -- 1. real Python client end-to-end against the real Go server:
    #       long multi-line Japanese text chunked on line boundaries --

    def test_long_japanese_text_chunks_on_line_boundaries_and_reassembles(self) -> None:
        client = SlackNotifyClient(base_url=self.base_url, session_token=SESSION_TOKEN, timeout_seconds=60.0)

        # ~8000 chars of Japanese text across many distinct lines, well over
        # the 3500-char chunk boundary, with no single line long enough to
        # need a hard split (so "\n".join(chunks) must reproduce the input).
        lines = [
            f"第{i}回シグナル観測メモ: 日経平均・米国株・暗号資産・マクロ経済の要点整理です。確認済み事実と未確認シグナルを区別して記載しています。"
            for i in range(120)
        ]
        original_text = "\n".join(lines)
        self.assertGreater(len(original_text), 7500)

        chunk_count = client.notify(original_text)
        self.assertGreaterEqual(chunk_count, 3)

        received = list(_StubSlackAPIHandler.received)
        self.assertEqual(len(received), chunk_count)

        # Every chunk must respect the 3500-char line-boundary limit.
        for chunk in received:
            self.assertLessEqual(len(chunk), 3500)
            for line in chunk.split("\n"):
                self.assertIn(line, lines)

        # Order preserved, reassembly (join with the same "\n" separator used
        # internally between original lines) reproduces the original text.
        reassembled = "\n".join(received)
        self.assertEqual(reassembled, original_text)

    # -- 2. wrong session bearer token is rejected with 401, and the
    #       Slack API must never have been reached --

    def test_wrong_session_token_returns_401_and_never_calls_slack_api(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/v1/slack/notify",
            data=json.dumps({"text": "hi"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer wrong-token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(_StubSlackAPIHandler.received, [])

    def test_wrong_session_token_via_real_client_raises(self) -> None:
        client = SlackNotifyClient(base_url=self.base_url, session_token="wrong-token", timeout_seconds=10.0)
        with self.assertRaises(SlackNotifyError):
            client.notify("hi")
        self.assertEqual(_StubSlackAPIHandler.received, [])


@unittest.skipUnless(shutil.which("go"), "go toolchain not available")
class GoSlackNotifyUnmountedWhenUnconfiguredTest(unittest.TestCase):
    """A separate auth-proxy instance with SLACK_BOT_TOKEN unset: the route
    must not be mounted at all (404), confirming fail-closed default."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.binary_path = Path(cls._tmpdir.name) / "auth-proxy-under-test-unmounted"
        _build_auth_proxy(cls.binary_path)

        cls.proxy_port = _free_port()
        env = dict(os.environ)
        env["AUTH_PROXY_ADDR"] = f"127.0.0.1:{cls.proxy_port}"
        env["AUTH_PROXY_SESSION_TOKEN"] = SESSION_TOKEN
        env.pop("SLACK_BOT_TOKEN", None)
        env.pop("SLACK_CHANNEL_ID", None)

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

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy_process.terminate()
        try:
            cls.proxy_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proxy_process.kill()
        cls._tmpdir.cleanup()

    def test_route_not_mounted_returns_404(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/v1/slack/notify",
            data=json.dumps({"text": "hi"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {SESSION_TOKEN}"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
