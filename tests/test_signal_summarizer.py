from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from shichimimi_agent.proxies.claude_proxy_client import ClaudeProxyClient
from shichimimi_agent.research.signal_summarizer import SignalSummary, summarize_signals


def _client() -> ClaudeProxyClient:
    return ClaudeProxyClient(
        base_url="http://claude-proxy.local",
        session_token="secret-session-token",
        session_id="sess1",
        role="ai_it_topic_runner",
    )


def _posts() -> list[dict[str, Any]]:
    return [
        {"author_handle": "alice", "text_redacted": "something happened", "engagement": {"like_count": 5}},
    ]


class SignalSummarizerTest(unittest.TestCase):
    def test_happy_path_returns_summary(self) -> None:
        body = json.dumps({"what_happened": "X happened.", "why_it_matters": "It matters."})
        response = {"content": [{"type": "text", "text": body}]}
        client = _client()
        with mock.patch.object(ClaudeProxyClient, "create_message", return_value=response) as create_message:
            result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
        self.assertEqual(result, SignalSummary(what_happened="X happened.", why_it_matters="It matters."))
        create_message.assert_called_once()

    def test_markdown_fenced_json_accepted(self) -> None:
        body = "```json\n" + json.dumps({"what_happened": "A.", "why_it_matters": "B."}) + "\n```"
        response = {"content": [{"type": "text", "text": body}]}
        client = _client()
        with mock.patch.object(ClaudeProxyClient, "create_message", return_value=response):
            result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
        self.assertEqual(result, SignalSummary(what_happened="A.", why_it_matters="B."))

    def test_malformed_json_returns_none(self) -> None:
        response = {"content": [{"type": "text", "text": "not json at all"}]}
        client = _client()
        with mock.patch.object(ClaudeProxyClient, "create_message", return_value=response):
            result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
        self.assertIsNone(result)

    def test_missing_key_returns_none(self) -> None:
        body = json.dumps({"what_happened": "only this"})
        response = {"content": [{"type": "text", "text": body}]}
        client = _client()
        with mock.patch.object(ClaudeProxyClient, "create_message", return_value=response):
            result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
        self.assertIsNone(result)

    def test_empty_string_value_returns_none(self) -> None:
        body = json.dumps({"what_happened": "", "why_it_matters": "B."})
        response = {"content": [{"type": "text", "text": body}]}
        client = _client()
        with mock.patch.object(ClaudeProxyClient, "create_message", return_value=response):
            result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
        self.assertIsNone(result)

    def test_http_error_returns_none(self) -> None:
        client = _client()
        with mock.patch.object(ClaudeProxyClient, "create_message", side_effect=OSError("HTTP Error 500: Internal Server Error")):
            result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
        self.assertIsNone(result)

    def test_no_content_returns_none(self) -> None:
        client = _client()
        with mock.patch.object(ClaudeProxyClient, "create_message", return_value={"content": []}):
            result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
        self.assertIsNone(result)

    def test_field_length_is_capped(self) -> None:
        long_text = "x" * 500
        body = json.dumps({"what_happened": long_text, "why_it_matters": long_text})
        response = {"content": [{"type": "text", "text": body}]}
        client = _client()
        with mock.patch.object(ClaudeProxyClient, "create_message", return_value=response):
            result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
        assert result is not None
        self.assertEqual(len(result.what_happened), 300)
        self.assertEqual(len(result.why_it_matters), 300)

    def test_session_token_never_appears_in_raised_or_returned_state(self) -> None:
        client = _client()

        def _raise(_payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("boom, unrelated failure")

        with mock.patch.object(ClaudeProxyClient, "create_message", side_effect=_raise):
            try:
                result = summarize_signals(client, model="claude-sonnet-5", query="q", posts=_posts())
            except Exception as exc:  # pragma: no cover - must never happen
                self.fail(f"summarize_signals raised: {exc}")
        self.assertIsNone(result)
        # The client's session token must never leak into what summarize_signals
        # returns or into any exception object it lets escape (it lets none escape).
        self.assertNotIn("secret-session-token", repr(result))


if __name__ == "__main__":
    unittest.main()
