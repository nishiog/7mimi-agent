from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from shichimimi_agent.cli import cmd_claude_digest
from shichimimi_agent.config import load_config
from shichimimi_agent.db import Repository, migrate
from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.runner.claude_digest import (
    ClaudeDigestOptions,
    build_digest_prompt,
    collect_signals,
    run_claude_digest,
)
from shichimimi_agent.security.policy_engine import PolicyEngine
from shichimimi_agent.sessions.workspace import create_workspace


def _post(post_id: str) -> dict[str, Any]:
    return {
        "id": post_id,
        "url": f"https://x.com/alice/status/{post_id}",
        "author_handle": "alice",
        "created_at": "2026-07-01T00:00:00Z",
        "text_redacted": "some observed text",
        "urls": [],
        "topics": [],
        "engagement": {"like_count": 1, "repost_count": 0},
        "collected_at": "2026-07-01T00:05:00Z",
    }


class FakeMcpClient:
    def __init__(self, base_url: str, *, posts_by_query=None, error_for_query=None) -> None:
        self.base_url = base_url
        self.posts_by_query = posts_by_query or {}
        self.error_for_query = error_for_query or {}
        self.initialized = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def initialize(self) -> dict[str, Any]:
        self.initialized = True
        return {"protocolVersion": "2025-03-26"}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        query = arguments["query"]
        if query in self.error_for_query:
            return {"content": [{"type": "text", "text": self.error_for_query[query]}], "isError": True}
        posts = self.posts_by_query.get(query, [])
        text = json.dumps({"posts": posts})
        return {"content": [{"type": "text", "text": text}], "isError": False}


class PartialQueryFailureTest(unittest.TestCase):
    """Documents the resilient behavior of collect_signals when one query
    among many fails with isError: the failing query is skipped (recorded
    in failed_queries) and collection continues, per the orchestrator's
    revised ADR-021 "zero posts across all queries" failure criterion."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.auth_client = AuthProxyClient(local_fallback_engine=PolicyEngine(self.config.policy))
        self._env_backup = dict(os.environ)
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["X_MCP_SESSION_TOKEN"] = "test-x-mcp-session-token"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _queries(self) -> list[str]:
        query_set = (self.config.schedules.get("query_sets") or {}).get("ai_it_watch") or {}
        return list(query_set.get("queries") or [])

    def test_single_query_error_is_skipped_and_other_queries_still_collected(self) -> None:
        all_queries = self._queries()
        self.assertGreaterEqual(len(all_queries), 3)
        queries = all_queries[:3]
        # First and third queries succeed with posts; second query errors.
        posts_by_query = {queries[0]: [_post("1")], queries[2]: [_post("3")]}
        error_for_query = {queries[1]: "upstream 500"}
        fake_client = FakeMcpClient(
            "http://x-mcp.local", posts_by_query=posts_by_query, error_for_query=error_for_query
        )

        result = collect_signals(
            auth_client=self.auth_client,
            repository=self.repository,
            session_id="sess1",
            task_id="task1",
            role="ai_it_topic_runner",
            queries=queries,
            mcp_client_factory=lambda base_url: fake_client,
        )

        # Every query was attempted, including those after the failing one.
        self.assertEqual(len(fake_client.calls), len(queries))
        self.assertEqual(result["failed_queries"], [queries[1]])
        collected_queries = {entry["query"] for entry in result["queries"]}
        self.assertEqual(collected_queries, {queries[0], queries[2]})
        self.assertNotIn(queries[1], collected_queries)

    def test_all_queries_failing_raises_runtime_error(self) -> None:
        queries = self._queries()
        error_for_query = {q: "upstream 500" for q in queries}
        fake_client = FakeMcpClient("http://x-mcp.local", posts_by_query={}, error_for_query=error_for_query)

        with self.assertRaises(RuntimeError):
            collect_signals(
                auth_client=self.auth_client,
                repository=self.repository,
                session_id="sess1",
                task_id="task1",
                role="ai_it_topic_runner",
                queries=queries,
                mcp_client_factory=lambda base_url: fake_client,
            )


class RunClaudeDigestEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config_obj = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.db_path = db_path

        # run_claude_digest requires the workspace to live under config.root
        # (it computes a path relative to it for the container mount), so use
        # the real repo's .sessions/ dir like production code does, and clean
        # it up in tearDown.
        self._session_id_for_cleanup = "test-claude-digest-gaps-" + next(tempfile._get_candidate_names())
        self.workspace_dir = create_workspace(self.root, self._session_id_for_cleanup)

        self._env_backup = dict(os.environ)
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["X_MCP_SESSION_TOKEN"] = "test-x-mcp-session-token"
        os.environ["CLAUDE_PROXY_URL"] = "http://host.docker.internal:18080"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "cp_sess_dev_secret"
        os.environ["GIT_PROXY_URL"] = "http://host.docker.internal:18081"
        os.environ["GIT_PROXY_SESSION_TOKEN"] = "gp_sess_dev_secret"

        query_set = (self.config_obj.schedules.get("query_sets") or {}).get("ai_it_watch") or {}
        self.queries = list(query_set.get("queries") or [])
        self.job = {"inputs": {"query_set": "ai_it_watch"}}

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        import shutil
        shutil.rmtree(self.root / ".sessions" / self._session_id_for_cleanup, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _fake_client_factory(self):
        posts_by_query = {q: [_post(f"{i}")] for i, q in enumerate(self.queries)}
        client = FakeMcpClient("http://x-mcp.local", posts_by_query=posts_by_query)
        return client, (lambda base_url: client)

    def _fetch_documents(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute("SELECT * FROM documents").fetchall())
        finally:
            conn.close()

    def test_success_path_writes_signals_invokes_docker_and_records_published_document(self) -> None:
        _client, factory = self._fake_client_factory()

        with mock.patch("shichimimi_agent.runner.claude_digest.subprocess.run") as run_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest._verify_published") as verify_mock:
            run_mock.return_value = subprocess.CompletedProcess(args=["docker"], returncode=0, stdout="{}", stderr="")
            verify_mock.return_value = (True, "a" * 40)

            result = run_claude_digest(
                config=self.config_obj,
                repository=self.repository,
                session_id="sess1",
                task_id="task1",
                workspace=self.workspace_dir,
                job=self.job,
                options=ClaudeDigestOptions(),
                auth_client=AuthProxyClient(local_fallback_engine=PolicyEngine(self.config_obj.policy)),
                mcp_client_factory=factory,
            )

        # docker was invoked exactly once
        self.assertEqual(run_mock.call_count, 1)
        docker_cmd = run_mock.call_args.args[0]
        self.assertEqual(docker_cmd[0], "docker")

        # signals.json was written into the workspace with expected shape
        signals_path = self.workspace_dir / "signals.json"
        self.assertTrue(signals_path.exists())
        signals = json.loads(signals_path.read_text(encoding="utf-8"))
        self.assertIn("collected_at", signals)
        self.assertEqual(len(signals["queries"]), len(self.queries))
        for entry in signals["queries"]:
            self.assertIn("query", entry)
            self.assertIn("posts", entry)
            for post in entry["posts"]:
                for field in ("id", "url", "author_handle", "created_at", "text_redacted", "engagement", "collected_at"):
                    self.assertIn(field, post)

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.verified)
        self.assertEqual(result.commit_sha, "a" * 40)

        rows = self._fetch_documents()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "published")
        self.assertEqual(rows[0]["commit_sha"], "a" * 40)

    def test_verification_failure_yields_nonzero_exit_and_failed_document_status(self) -> None:
        _client, factory = self._fake_client_factory()

        with mock.patch("shichimimi_agent.runner.claude_digest.subprocess.run") as run_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest._verify_published") as verify_mock:
            run_mock.return_value = subprocess.CompletedProcess(args=["docker"], returncode=0, stdout="{}", stderr="")
            verify_mock.return_value = (False, None)

            result = run_claude_digest(
                config=self.config_obj,
                repository=self.repository,
                session_id="sess2",
                task_id="task2",
                workspace=self.workspace_dir,
                job=self.job,
                options=ClaudeDigestOptions(),
                auth_client=AuthProxyClient(local_fallback_engine=PolicyEngine(self.config_obj.policy)),
                mcp_client_factory=factory,
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(result.verified)
        self.assertIsNone(result.verified_path)
        self.assertIsNone(result.commit_sha)

        rows = self._fetch_documents()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIsNone(rows[0]["repo"]) if "repo" in rows[0].keys() else None

    def test_docker_run_nonzero_exit_skips_verification_and_fails(self) -> None:
        _client, factory = self._fake_client_factory()

        with mock.patch("shichimimi_agent.runner.claude_digest.subprocess.run") as run_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest._verify_published") as verify_mock:
            run_mock.return_value = subprocess.CompletedProcess(args=["docker"], returncode=1, stdout="", stderr="boom")

            result = run_claude_digest(
                config=self.config_obj,
                repository=self.repository,
                session_id="sess3",
                task_id="task3",
                workspace=self.workspace_dir,
                job=self.job,
                options=ClaudeDigestOptions(),
                auth_client=AuthProxyClient(local_fallback_engine=PolicyEngine(self.config_obj.policy)),
                mcp_client_factory=factory,
            )

        verify_mock.assert_not_called()
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.verified)


class PromptDoesNotLeakSecretsTest(unittest.TestCase):
    def test_prompt_contains_no_session_tokens_or_env_secrets(self) -> None:
        secret_env = {
            "CLAUDE_PROXY_SESSION_TOKEN": "cp_sess_super_secret",
            "GIT_PROXY_SESSION_TOKEN": "gp_sess_super_secret",
            "ANTHROPIC_API_KEY": "sk-ant-real-secret",
            "SHICHIMIMI_AGENT_X_BEARER_TOKEN": "x-secret-token",
            "GITHUB_TOKEN": "ghp_secret",
        }
        with mock.patch.dict(os.environ, secret_env):
            prompt = build_digest_prompt(
                notes_repo="7milch/ai-it-research-notes",
                target_relative_path="daily/2026/07/2026-07-05.md",
                git_proxy_url="http://auth-proxy:18081",
            )
        for value in secret_env.values():
            self.assertNotIn(value, prompt)
        for key in secret_env:
            self.assertNotIn(key, prompt)


class CliArgPlumbingTest(unittest.TestCase):
    def setUp(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self._tmpdir = tempfile.TemporaryDirectory()
        # Isolated root with its own config/ and .data/, so this test never
        # touches the real repo's database.
        self.root = Path(self._tmpdir.name) / "root"
        self.root.mkdir()
        import shutil
        shutil.copytree(repo_root / "config", self.root / "config")
        self._env_backup = dict(os.environ)
        os.environ["CLAUDE_PROXY_URL"] = "http://host.docker.internal:18080"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "cp_sess_dev"
        os.environ["GIT_PROXY_URL"] = "http://host.docker.internal:18081"
        os.environ["GIT_PROXY_SESSION_TOKEN"] = "gp_sess_dev"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _args(self, **overrides: Any) -> argparse.Namespace:
        defaults = dict(root=str(self.root), job="ai-it-x-daily-digest", model=None, max_turns=40)
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def _run_with_captured_options(self, args: argparse.Namespace):
        captured: dict[str, Any] = {}

        def fake_run_claude_digest(**kwargs: Any):
            captured["options"] = kwargs["options"]
            from shichimimi_agent.runner.claude_digest import ClaudeDigestResult
            return ClaudeDigestResult(
                exit_code=0, stdout="", stderr="", workspace=kwargs["workspace"],
                verified=True, verified_path="daily/2026/07/2026-07-05.md", commit_sha="a" * 40,
            )

        with mock.patch("shichimimi_agent.runner.claude_digest.run_claude_digest", side_effect=fake_run_claude_digest):
            exit_code = cmd_claude_digest(args)
        return exit_code, captured["options"]

    def test_explicit_model_overrides_resolve_model_default(self) -> None:
        args = self._args(model="claude-opus-9")
        exit_code, options = self._run_with_captured_options(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(options.model, "claude-opus-9")

    def test_no_model_falls_back_to_resolve_model(self) -> None:
        from shichimimi_agent.config import load_config
        from shichimimi_agent.config.model_selection import resolve_model

        config = load_config(self.root)
        job_role = "ai_it_topic_runner"
        role_config = ((config.roles or {}).get("roles") or {}).get(job_role) or {}
        expected_model = resolve_model(role_config, config.policy)

        args = self._args(model=None)
        exit_code, options = self._run_with_captured_options(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(options.model, expected_model)

    def test_max_turns_is_plumbed_through(self) -> None:
        args = self._args(max_turns=7)
        _exit_code, options = self._run_with_captured_options(args)
        self.assertEqual(options.max_turns, 7)


class AllowedToolsExactnessTest(unittest.TestCase):
    def test_allowed_tools_has_no_bare_bash_wildcard(self) -> None:
        from shichimimi_agent.runner.claude_digest import DEFAULT_ALLOWED_TOOLS

        tools = DEFAULT_ALLOWED_TOOLS.split(",")
        self.assertNotIn("Bash", tools)
        self.assertNotIn("Bash(*)", tools)
        for tool in tools:
            if tool.startswith("Bash"):
                self.assertEqual(tool, "Bash(git:*)")


if __name__ == "__main__":
    unittest.main()
