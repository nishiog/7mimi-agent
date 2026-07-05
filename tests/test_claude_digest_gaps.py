"""ADR-028: direct-/mcp coverage for run_claude_digest and cli plumbing.

- mcp_session.issue_session error handling (HTTP errors, malformed JSON,
  missing fields in the response payload).
- build_direct_mcp_config's exact JSON shape.
- build_digest_prompt's cost guardrails plus prior invariants.
- run_claude_digest: mints a session token via issue_session, writes no
  signals.json, and passes DIRECT_MCP_ALLOWED_TOOLS + the mcp_config
  through to the docker command.
- cli.cmd_claude_digest argument plumbing (model/max-turns).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from shichimimi_agent.cli import cmd_claude_digest
from shichimimi_agent.config import load_config
from shichimimi_agent.db import Repository, migrate
from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.runner.claude_digest import (
    DEFAULT_ALLOWED_TOOLS,
    DIRECT_MCP_ALLOWED_TOOLS,
    ClaudeDigestOptions,
    ClaudeDigestResult,
    build_digest_prompt,
    build_direct_mcp_config,
    build_docker_command,
    run_claude_digest,
)
from shichimimi_agent.runner.mcp_session import IssuedSession, McpSessionError, issue_session
from shichimimi_agent.security.policy_engine import PolicyEngine
from shichimimi_agent.sessions.workspace import create_workspace


class IssueSessionErrorHandlingTest(unittest.TestCase):
    """mcp_session.issue_session: error paths not covered by the Go
    integration test (which only exercises the happy path + wrong bearer)."""

    def test_url_error_wraps_as_mcp_session_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(McpSessionError) as ctx:
                issue_session(auth_proxy_url="http://127.0.0.1:1", static_token="tok", role="ai_it_topic_runner")
        self.assertIn("connection refused", str(ctx.exception))

    def test_http_error_wraps_as_mcp_session_error_with_code(self) -> None:
        err = urllib.error.HTTPError("http://x", 500, "boom", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(McpSessionError) as ctx:
                issue_session(auth_proxy_url="http://127.0.0.1:1", static_token="tok", role="ai_it_topic_runner")
        self.assertIn("500", str(ctx.exception))

    def test_invalid_json_response_raises(self) -> None:
        class _Resp:
            def read(self_inner):
                return b"not json"

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_Resp()):
            with self.assertRaises(McpSessionError):
                issue_session(auth_proxy_url="http://127.0.0.1:1", static_token="tok", role="ai_it_topic_runner")

    def test_missing_token_field_raises(self) -> None:
        class _Resp:
            def read(self_inner):
                return json.dumps({"ttl_seconds": 2100}).encode("utf-8")

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_Resp()):
            with self.assertRaises(McpSessionError) as ctx:
                issue_session(auth_proxy_url="http://127.0.0.1:1", static_token="tok", role="ai_it_topic_runner")
        self.assertIn("unexpected payload", str(ctx.exception))

    def test_non_int_ttl_field_raises(self) -> None:
        class _Resp:
            def read(self_inner):
                return json.dumps({"token": "abc", "ttl_seconds": "2100"}).encode("utf-8")

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_Resp()):
            with self.assertRaises(McpSessionError):
                issue_session(auth_proxy_url="http://127.0.0.1:1", static_token="tok", role="ai_it_topic_runner")

    def test_happy_path_returns_issued_session(self) -> None:
        class _Resp:
            def read(self_inner):
                return json.dumps({"token": "sess-abc", "ttl_seconds": 2100}).encode("utf-8")

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        captured = {}

        def _fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Resp()

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            issued = issue_session(auth_proxy_url="http://auth-proxy:18081", static_token="static-tok", role="ai_it_topic_runner")

        self.assertEqual(issued, IssuedSession(token="sess-abc", ttl_seconds=2100))
        self.assertEqual(captured["url"], "http://auth-proxy:18081/session/issue")
        self.assertEqual(captured["headers"]["authorization"], "Bearer static-tok")
        self.assertEqual(captured["body"], {"role": "ai_it_topic_runner"})


class BuildDirectMcpConfigShapeTest(unittest.TestCase):
    """The Claude Code --mcp-config JSON must match the spike-proven schema
    exactly: mcpServers.<name>.{type: "http", url, headers.Authorization}."""

    def test_shape_matches_spike_proven_schema(self) -> None:
        self._env_backup = dict(os.environ)
        os.environ.pop("RUNNER_NETWORK", None)
        os.environ.pop("RUNNER_MCP_URL", None)
        try:
            config = build_direct_mcp_config(session_token="sess-tok-123")
        finally:
            os.environ.clear()
            os.environ.update(self._env_backup)

        self.assertEqual(
            config,
            {
                "mcpServers": {
                    "x7mimi": {
                        "type": "http",
                        "url": "http://host.docker.internal:18081/mcp",
                        "headers": {"Authorization": "Bearer sess-tok-123"},
                    }
                }
            },
        )
        # Must be valid, round-trippable JSON (this is literally what gets
        # written to .mcp.json).
        json.loads(json.dumps(config))

    def test_url_uses_auth_proxy_service_name_on_runner_network(self) -> None:
        self._env_backup = dict(os.environ)
        os.environ["RUNNER_NETWORK"] = "7mimi-internal"
        os.environ.pop("RUNNER_MCP_URL", None)
        try:
            config = build_direct_mcp_config(session_token="sess-tok-123")
        finally:
            os.environ.clear()
            os.environ.update(self._env_backup)
        self.assertEqual(config["mcpServers"]["x7mimi"]["url"], "http://auth-proxy:18081/mcp")

    def test_runner_mcp_url_override_wins(self) -> None:
        self._env_backup = dict(os.environ)
        os.environ["RUNNER_MCP_URL"] = "http://custom-host:9999/mcp"
        try:
            config = build_direct_mcp_config(session_token="sess-tok-123")
        finally:
            os.environ.clear()
            os.environ.update(self._env_backup)
        self.assertEqual(config["mcpServers"]["x7mimi"]["url"], "http://custom-host:9999/mcp")


class DockerCommandMcpConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmpdir.name)
        self._env_backup = dict(os.environ)
        os.environ["CLAUDE_PROXY_URL"] = "http://host.docker.internal:18080"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "cp-sess-secret"
        os.environ["GIT_PROXY_URL"] = "http://host.docker.internal:18081"
        os.environ["GIT_PROXY_SESSION_TOKEN"] = "gp-sess-secret"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_mcp_config_written_and_flags_present(self) -> None:
        mcp_config = build_direct_mcp_config(session_token="sess-tok-xyz")
        cmd = build_docker_command(
            workspace=self.workspace,
            session_id="sess1",
            role="ai_it_topic_runner",
            prompt="prompt text",
            options=ClaudeDigestOptions(),
            allowed_tools=DIRECT_MCP_ALLOWED_TOOLS,
            mcp_config=mcp_config,
        )
        self.assertIn("--mcp-config", cmd)
        idx = cmd.index("--mcp-config")
        self.assertEqual(cmd[idx + 1], "/workspace/.mcp.json")
        self.assertIn("--strict-mcp-config", cmd)

        written = json.loads((self.workspace / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(written, mcp_config)

        self.assertIn("--allowedTools", cmd)
        allowed_idx = cmd.index("--allowedTools")
        self.assertEqual(cmd[allowed_idx + 1], DIRECT_MCP_ALLOWED_TOOLS)
        # the minted session token must never leak into the docker command
        # args themselves (it's only written to .mcp.json inside the mounted
        # workspace, not passed as -e or CLI arg).
        self.assertNotIn("sess-tok-xyz", cmd)

    def test_no_mcp_config_omits_flags(self) -> None:
        cmd = build_docker_command(
            workspace=self.workspace,
            session_id="sess1",
            role="ai_it_topic_runner",
            prompt="prompt text",
            options=ClaudeDigestOptions(),
        )
        self.assertNotIn("--mcp-config", cmd)
        self.assertNotIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], DEFAULT_ALLOWED_TOOLS)
        self.assertFalse((self.workspace / ".mcp.json").exists())


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
        os.environ["X_MCP_URL"] = "http://auth-proxy:18081"
        os.environ["X_MCP_SESSION_TOKEN"] = "static-admin-token"
        os.environ["CLAUDE_PROXY_URL"] = "http://host.docker.internal:18080"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "cp_sess_dev_secret"
        os.environ["GIT_PROXY_URL"] = "http://host.docker.internal:18081"
        os.environ["GIT_PROXY_SESSION_TOKEN"] = "gp_sess_dev_secret"

        self.job = {"inputs": {"query_set": "ai_it_watch"}}

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        import shutil
        shutil.rmtree(self.root / ".sessions" / self._session_id_for_cleanup, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _fetch_documents(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute("SELECT * FROM documents").fetchall())
        finally:
            conn.close()

    def test_success_path_mints_token_invokes_docker_and_records_published_document(self) -> None:
        with mock.patch("shichimimi_agent.runner.claude_digest.issue_session") as issue_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest.subprocess.run") as run_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest._verify_published") as verify_mock:
            issue_mock.return_value = IssuedSession(token="minted-sess-tok", ttl_seconds=2100)
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
            )

        issue_mock.assert_called_once()
        _, kwargs = issue_mock.call_args
        self.assertEqual(kwargs["auth_proxy_url"], "http://auth-proxy:18081")
        self.assertEqual(kwargs["static_token"], "static-admin-token")
        self.assertEqual(kwargs["role"], "ai_it_topic_runner")

        # docker was invoked exactly once
        self.assertEqual(run_mock.call_count, 1)
        docker_cmd = run_mock.call_args.args[0]
        self.assertEqual(docker_cmd[0], "docker")
        allowed_idx = docker_cmd.index("--allowedTools")
        self.assertEqual(docker_cmd[allowed_idx + 1], DIRECT_MCP_ALLOWED_TOOLS)
        self.assertIn("--mcp-config", docker_cmd)

        # no signals.json anymore (ADR-028: direct /mcp is the sole flow);
        # instead the mcp config with the minted token is written.
        self.assertFalse((self.workspace_dir / "signals.json").exists())
        self.assertTrue((self.workspace_dir / ".mcp.json").exists())
        written = json.loads((self.workspace_dir / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(written["mcpServers"]["x7mimi"]["headers"]["Authorization"], "Bearer minted-sess-tok")

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.verified)
        self.assertEqual(result.commit_sha, "a" * 40)

        rows = self._fetch_documents()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "published")
        self.assertEqual(rows[0]["commit_sha"], "a" * 40)

    def test_verification_failure_yields_nonzero_exit_and_failed_document_status(self) -> None:
        with mock.patch("shichimimi_agent.runner.claude_digest.issue_session") as issue_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest.subprocess.run") as run_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest._verify_published") as verify_mock:
            issue_mock.return_value = IssuedSession(token="minted-sess-tok", ttl_seconds=2100)
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
        with mock.patch("shichimimi_agent.runner.claude_digest.issue_session") as issue_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest.subprocess.run") as run_mock, \
             mock.patch("shichimimi_agent.runner.claude_digest._verify_published") as verify_mock:
            issue_mock.return_value = IssuedSession(token="minted-sess-tok", ttl_seconds=2100)
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
            )

        verify_mock.assert_not_called()
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.verified)

    def test_missing_x_mcp_url_raises(self) -> None:
        del os.environ["X_MCP_URL"]
        with self.assertRaises(ValueError):
            run_claude_digest(
                config=self.config_obj,
                repository=self.repository,
                session_id="sess4",
                task_id="task4",
                workspace=self.workspace_dir,
                job=self.job,
                options=ClaudeDigestOptions(),
                auth_client=AuthProxyClient(local_fallback_engine=PolicyEngine(self.config_obj.policy)),
            )

    def test_missing_x_mcp_session_token_raises(self) -> None:
        del os.environ["X_MCP_SESSION_TOKEN"]
        with self.assertRaises(ValueError):
            run_claude_digest(
                config=self.config_obj,
                repository=self.repository,
                session_id="sess5",
                task_id="task5",
                workspace=self.workspace_dir,
                job=self.job,
                options=ClaudeDigestOptions(),
                auth_client=AuthProxyClient(local_fallback_engine=PolicyEngine(self.config_obj.policy)),
            )


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
        tools = DEFAULT_ALLOWED_TOOLS.split(",")
        self.assertNotIn("Bash", tools)
        self.assertNotIn("Bash(*)", tools)
        for tool in tools:
            if tool.startswith("Bash"):
                self.assertEqual(tool, "Bash(git:*)")


if __name__ == "__main__":
    unittest.main()
