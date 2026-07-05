from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from shichimimi_agent.config import load_config
from shichimimi_agent.db import Repository, migrate
from shichimimi_agent.mcp.client import McpHttpClient
from shichimimi_agent.research.signal_summarizer import SignalSummary
from shichimimi_agent.roles.ai_it_topic_runner import AiItTopicRunner
from shichimimi_agent.security.policy_engine import PolicyEngine


class FakeMcpClient:
    """In-process stand-in for McpHttpClient, keyed by query -> posts payload."""

    def __init__(self, base_url: str, *, posts_by_query: dict[str, list[dict[str, Any]]] | None = None, error: str | None = None) -> None:
        self.base_url = base_url
        self.posts_by_query = posts_by_query or {}
        self.error = error
        self.initialized = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def initialize(self) -> dict[str, Any]:
        self.initialized = True
        return {"protocolVersion": "2025-03-26"}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if self.error is not None:
            return {"content": [{"type": "text", "text": self.error}], "isError": True}
        query = arguments["query"]
        posts = self.posts_by_query.get(query, [])
        text = json.dumps({"posts": posts})
        return {"content": [{"type": "text", "text": text}], "isError": False}


def _post(post_id: str, url: str, text: str, urls: list[str], likes: int, reposts: int) -> dict[str, Any]:
    return {
        "id": post_id,
        "url": url,
        "author_handle": "alice",
        "created_at": "2026-07-01T00:00:00Z",
        "text_redacted": text,
        "urls": urls,
        "topics": [],
        "engagement": {"like_count": likes, "repost_count": reposts},
        "collected_at": "2026-07-01T00:05:00Z",
    }


class AiItTopicRunnerRealCollectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.policy_engine = PolicyEngine(self.config.policy)
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _job(self) -> dict[str, Any]:
        return {
            "role": "ai_it_topic_runner",
            "inputs": {"query_set": "ai_it_watch"},
            "output": {"repo": "nishiog/ai-it-research-notes"},
        }

    def _queries(self) -> list[str]:
        query_set = (self.config.schedules.get("query_sets") or {}).get("ai_it_watch") or {}
        return list(query_set.get("queries") or [])

    def test_real_collection_builds_digest_from_stubbed_posts(self) -> None:
        queries = self._queries()
        self.assertTrue(queries)
        long_text = "x" * 250
        posts_by_query = {
            query: [
                _post(f"{i}-low", f"https://x.com/alice/status/{i}low", "low engagement post", [], likes=1, reposts=0),
                _post(
                    f"{i}-high",
                    f"https://x.com/alice/status/{i}high",
                    long_text,
                    [f"https://example.com/evidence/{i}"],
                    likes=100,
                    reposts=10,
                ),
            ]
            for i, query in enumerate(queries[:3])
        }
        fake_client = FakeMcpClient("http://x-mcp.local", posts_by_query=posts_by_query)

        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        runner = AiItTopicRunner(
            config=self.config,
            repository=self.repository,
            policy_engine=self.policy_engine,
            mcp_client_factory=lambda base_url: fake_client,
        )

        result = runner.run_daily_digest(session_id="sess_real", task_id="task_real", job=self._job(), dry_run=True)

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(fake_client.initialized)
        self.assertEqual(len(fake_client.calls), len(queries[:3]))

        items = {ref["topic"]: ref for ref in result.source_refs}
        for i, query in enumerate(queries[:3]):
            self.assertIn(query, items)
        markdown_path = Path(result.path)
        self.assertTrue(markdown_path.exists())
        content = markdown_path.read_text(encoding="utf-8")
        self.assertIn("(via X signal)", content)
        self.assertIn("https://example.com/evidence/0", content)
        # what_happened should be truncated to 200 chars of text + suffix, single line
        self.assertIn(long_text[:200], content)

    def test_zero_posts_for_all_queries_raises_runtime_error(self) -> None:
        fake_client = FakeMcpClient("http://x-mcp.local", posts_by_query={})
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        runner = AiItTopicRunner(
            config=self.config,
            repository=self.repository,
            policy_engine=self.policy_engine,
            mcp_client_factory=lambda base_url: fake_client,
        )
        with self.assertRaises(RuntimeError):
            runner.run_daily_digest(session_id="sess_real", task_id="task_real", job=self._job(), dry_run=True)

    def test_is_error_result_raises_runtime_error(self) -> None:
        fake_client = FakeMcpClient("http://x-mcp.local", error="rate limited")
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        runner = AiItTopicRunner(
            config=self.config,
            repository=self.repository,
            policy_engine=self.policy_engine,
            mcp_client_factory=lambda base_url: fake_client,
        )
        with self.assertRaises(RuntimeError):
            runner.run_daily_digest(session_id="sess_real", task_id="task_real", job=self._job(), dry_run=True)

    def test_x_mcp_url_unset_uses_mock_path(self) -> None:
        os.environ.pop("X_MCP_URL", None)
        runner = AiItTopicRunner(config=self.config, repository=self.repository, policy_engine=self.policy_engine)
        result = runner.run_daily_digest(session_id="sess_mock", task_id="task_mock", job=self._job(), dry_run=True)
        self.assertEqual(result.status, "succeeded")
        topics = {ref["topic"] for ref in result.source_refs}
        self.assertEqual(topics, {"MCP ecosystem", "Claude Code / coding agents", "AI security / prompt injection"})

    def test_authorization_deny_on_real_path_raises_before_mcp_call(self) -> None:
        from unittest import mock

        class FakeResponse:
            def __init__(self, body: dict) -> None:
                self._body = json.dumps(body).encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        fake_client = FakeMcpClient("http://x-mcp.local", posts_by_query={})
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["AUTH_PROXY_URL"] = "http://auth-proxy.local"
        runner = AiItTopicRunner(
            config=self.config,
            repository=self.repository,
            policy_engine=self.policy_engine,
            mcp_client_factory=lambda base_url: fake_client,
        )

        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse({"decision": "block", "reason": "denied by policy", "policy_version": "1"})
            with self.assertRaises(PermissionError):
                runner.run_daily_digest(session_id="sess_deny", task_id="task_deny", job=self._job(), dry_run=True)

        self.assertFalse(fake_client.initialized)
        self.assertEqual(fake_client.calls, [])

    def test_authorization_deny_is_persisted_as_audit_event(self) -> None:
        from unittest import mock

        class FakeResponse:
            def __init__(self, body: dict) -> None:
                self._body = json.dumps(body).encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        fake_client = FakeMcpClient("http://x-mcp.local", posts_by_query={})
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["AUTH_PROXY_URL"] = "http://auth-proxy.local"
        runner = AiItTopicRunner(
            config=self.config,
            repository=self.repository,
            policy_engine=self.policy_engine,
            mcp_client_factory=lambda base_url: fake_client,
        )

        # tool_events has FK constraints on sessions/tasks; create real rows the
        # way the CLI entrypoint does, instead of passing arbitrary ids.
        job = self._job()
        session_id = self.repository.create_session(source="test", role="ai_it_topic_runner", workspace_path="/tmp/ws")
        task_id = self.repository.create_task(session_id=session_id, role="ai_it_topic_runner", input_data=job)

        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse({"decision": "block", "reason": "denied by policy", "policy_version": "1"})
            with self.assertRaises(PermissionError):
                runner.run_daily_digest(session_id=session_id, task_id=task_id, job=job, dry_run=True)

        conn = self.repository._connect()
        try:
            rows = conn.execute(
                "SELECT tool_name, decision, success FROM tool_events WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 1)
        tool_name, decision, success = rows[0]
        self.assertEqual(tool_name, "x.search_posts_recent")
        self.assertIn(decision, ("block", "deny"))
        self.assertEqual(success, 0)


class FakeClaudeClient:
    def __init__(self, session_id: str, role: str) -> None:
        self.session_id = session_id
        self.role = role


class AiItTopicRunnerLlmSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.policy_engine = PolicyEngine(self.config.policy)
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _job(self) -> dict[str, Any]:
        return {
            "role": "ai_it_topic_runner",
            "inputs": {"query_set": "ai_it_watch"},
            "output": {"repo": "nishiog/ai-it-research-notes"},
        }

    def _queries(self) -> list[str]:
        query_set = (self.config.schedules.get("query_sets") or {}).get("ai_it_watch") or {}
        return list(query_set.get("queries") or [])

    def _fake_mcp_client(self) -> FakeMcpClient:
        queries = self._queries()
        posts_by_query = {
            query: [
                _post(f"{i}-high", f"https://x.com/alice/status/{i}high", "some observed text", ["https://example.com/e"], likes=10, reposts=1),
            ]
            for i, query in enumerate(queries[:3])
        }
        return FakeMcpClient("http://x-mcp.local", posts_by_query=posts_by_query)

    def test_env_unset_llm_client_factory_never_called(self) -> None:
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ.pop("CLAUDE_PROXY_URL", None)
        os.environ.pop("CLAUDE_PROXY_SESSION_TOKEN", None)
        fake_mcp = self._fake_mcp_client()
        factory_calls: list[tuple[str, str]] = []

        def claude_client_factory(session_id: str, role: str) -> FakeClaudeClient:
            factory_calls.append((session_id, role))
            return FakeClaudeClient(session_id, role)

        runner = AiItTopicRunner(
            config=self.config,
            repository=self.repository,
            policy_engine=self.policy_engine,
            mcp_client_factory=lambda base_url: fake_mcp,
            claude_client_factory=claude_client_factory,
        )
        result = runner.run_daily_digest(session_id="sess_llm_off", task_id="task_llm_off", job=self._job(), dry_run=True)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(factory_calls, [])
        content = Path(result.path).read_text(encoding="utf-8")
        self.assertIn("(via X signal)", content)
        self.assertNotIn("LLM要約", content)

    def test_llm_summary_applied_to_digest_item(self) -> None:
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["CLAUDE_PROXY_URL"] = "http://claude-proxy.local"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "token-abc"
        fake_mcp = self._fake_mcp_client()

        def claude_client_factory(session_id: str, role: str) -> FakeClaudeClient:
            return FakeClaudeClient(session_id, role)

        runner = AiItTopicRunner(
            config=self.config,
            repository=self.repository,
            policy_engine=self.policy_engine,
            mcp_client_factory=lambda base_url: fake_mcp,
            claude_client_factory=claude_client_factory,
        )
        with mock.patch(
            "shichimimi_agent.roles.ai_it_topic_runner.summarize_signals",
            return_value=SignalSummary(what_happened="LLM summarized fact.", why_it_matters="LLM importance."),
        ) as summarize_mock:
            result = runner.run_daily_digest(session_id="sess_llm_on", task_id="task_llm_on", job=self._job(), dry_run=True)

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(summarize_mock.called)
        content = Path(result.path).read_text(encoding="utf-8")
        self.assertIn("LLM summarized fact. (via X signal, LLM要約)", content)
        self.assertIn("LLM importance.", content)

    def test_llm_deny_falls_back_to_deterministic_and_records_event(self) -> None:
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["CLAUDE_PROXY_URL"] = "http://claude-proxy.local"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "token-abc"
        os.environ["AUTH_PROXY_URL"] = "http://auth-proxy.local"
        fake_mcp = self._fake_mcp_client()

        class FakeResponse:
            def __init__(self, body: dict) -> None:
                self._body = json.dumps(body).encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def urlopen_side_effect(request, timeout=None):
            payload = json.loads(request.data.decode("utf-8"))
            if payload.get("tool_name") == "claude.summarize_signals":
                return FakeResponse({"decision": "block", "reason": "denied", "policy_version": "1"})
            return FakeResponse({"decision": "allow", "reason": "ok", "policy_version": "1"})

        claude_factory_calls: list[Any] = []

        def claude_client_factory(session_id: str, role: str) -> FakeClaudeClient:
            claude_factory_calls.append((session_id, role))
            return FakeClaudeClient(session_id, role)

        runner = AiItTopicRunner(
            config=self.config,
            repository=self.repository,
            policy_engine=self.policy_engine,
            mcp_client_factory=lambda base_url: fake_mcp,
            claude_client_factory=claude_client_factory,
        )

        job = self._job()
        session_id = self.repository.create_session(source="test", role="ai_it_topic_runner", workspace_path="/tmp/ws")
        task_id = self.repository.create_task(session_id=session_id, role="ai_it_topic_runner", input_data=job)

        with mock.patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            result = runner.run_daily_digest(session_id=session_id, task_id=task_id, job=job, dry_run=True)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(claude_factory_calls, [])
        content = Path(result.path).read_text(encoding="utf-8")
        self.assertIn("(via X signal)", content)
        self.assertNotIn("LLM要約", content)

        conn = self.repository._connect()
        try:
            rows = conn.execute(
                "SELECT tool_name, decision, success FROM tool_events WHERE session_id = ? AND tool_name = 'claude.summarize_signals'",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(len(rows) >= 1)
        for _, decision, success in rows:
            self.assertIn(decision, ("block", "deny"))
            self.assertEqual(success, 0)


if __name__ == "__main__":
    unittest.main()
