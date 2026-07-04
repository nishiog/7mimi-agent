from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from shichimimi_agent.config import load_config
from shichimimi_agent.hooks.pre_tool_use import PreToolUseInput, run_pre_tool_use
from shichimimi_agent.proxies import AuthProxyClient
from shichimimi_agent.security import PolicyEngine


def _payload() -> PreToolUseInput:
    return PreToolUseInput(
        session_id="sess_dev",
        task_id="task_dev",
        role="ai_it_topic_runner",
        tool_name="x.search_posts_recent",
        arguments={"query": '"Claude Code"'},
    )


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class RunPreToolUseTest(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.engine = PolicyEngine(load_config(root).policy)

    def test_remote_authorize_allows_and_hits_expected_endpoint(self) -> None:
        client = AuthProxyClient(base_url="http://auth-proxy.local", local_fallback_engine=self.engine)

        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse(
                {"decision": "allow", "reason": "ok", "policy_version": "1"}
            )
            decision = run_pre_tool_use(client, _payload())

        self.assertTrue(decision.allowed)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "http://auth-proxy.local/v1/tool/authorize")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["session_id"], "sess_dev")
        self.assertEqual(body["task_id"], "task_dev")
        self.assertEqual(body["role"], "ai_it_topic_runner")
        self.assertEqual(body["tool_name"], "x.search_posts_recent")

    def test_remote_unreachable_is_fail_closed(self) -> None:
        client = AuthProxyClient(
            base_url="http://127.0.0.1:1", local_fallback_engine=self.engine, timeout_seconds=0.2
        )
        decision = run_pre_tool_use(client, _payload())
        self.assertFalse(decision.allowed)

    def test_no_base_url_uses_local_fallback_engine(self) -> None:
        client = AuthProxyClient(base_url="", local_fallback_engine=self.engine)
        decision = run_pre_tool_use(client, _payload())
        self.assertTrue(decision.allowed)

    def test_authorizer_raising_directly_is_fail_closed(self) -> None:
        """Hook's own outer try/except must catch exceptions from any authorizer,
        not just ones AuthProxyClient already wraps internally."""

        class ExplodingAuthorizer:
            def authorize(self, **kwargs: object) -> None:
                raise RuntimeError("boom")

        decision = run_pre_tool_use(ExplodingAuthorizer(), _payload())  # type: ignore[arg-type]
        self.assertFalse(decision.allowed)
        self.assertIn("boom", decision.reason)

if __name__ == "__main__":
    unittest.main()
