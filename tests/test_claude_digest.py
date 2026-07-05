from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from shichimimi_agent.config import load_config
from shichimimi_agent.db import Repository, migrate
from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.runner.claude_digest import (
    ClaudeDigestOptions,
    build_digest_prompt,
    build_docker_command,
    collect_signals,
    verify_digest_in_repo,
)
from shichimimi_agent.security.policy_engine import PolicyEngine


class FakeMcpClient:
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


class CollectSignalsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.policy_engine = PolicyEngine(self.config.policy)
        self.auth_client = AuthProxyClient(local_fallback_engine=self.policy_engine)
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _queries(self) -> list[str]:
        query_set = (self.config.schedules.get("query_sets") or {}).get("ai_it_watch") or {}
        return list(query_set.get("queries") or [])

    def test_collects_for_all_queries(self) -> None:
        queries = self._queries()
        self.assertEqual(len(queries), 18)
        posts_by_query = {q: [_post(f"{i}")] for i, q in enumerate(queries)}
        fake_client = FakeMcpClient("http://x-mcp.local", posts_by_query=posts_by_query)
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["X_MCP_SESSION_TOKEN"] = "test-x-mcp-session-token"

        result = collect_signals(
            auth_client=self.auth_client,
            repository=self.repository,
            session_id="sess1",
            task_id="task1",
            role="ai_it_topic_runner",
            queries=queries,
            mcp_client_factory=lambda base_url: fake_client,
        )

        self.assertTrue(fake_client.initialized)
        self.assertEqual(len(fake_client.calls), len(queries))
        self.assertEqual(len(result["queries"]), len(queries))
        self.assertIn("collected_at", result)
        for entry in result["queries"]:
            self.assertEqual(len(entry["posts"]), 1)

    def test_redactor_scrubs_secret_like_text_before_signals_json(self) -> None:
        from shichimimi_agent.hooks.redaction import Redactor

        queries = self._queries()
        secret_post = _post("secret1")
        secret_post["text_redacted"] = (
            'check out this token Bearer abcDEF123.token here'
        )
        posts_by_query = {q: [secret_post] for q in queries}
        fake_client = FakeMcpClient("http://x-mcp.local", posts_by_query=posts_by_query)
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["X_MCP_SESSION_TOKEN"] = "test-x-mcp-session-token"

        redactor = Redactor(self.config.policy.get("redaction_policy", {}).get("patterns") or [])
        result = collect_signals(
            auth_client=self.auth_client,
            repository=self.repository,
            session_id="sess1",
            task_id="task1",
            role="ai_it_topic_runner",
            queries=queries,
            mcp_client_factory=lambda base_url: fake_client,
            redactor=redactor,
        )

        for entry in result["queries"]:
            for post in entry["posts"]:
                text = post["text_redacted"]
                self.assertNotIn("Bearer abcDEF123", text)
                self.assertIn("[REDACTED:bearer_token]", text)

        # Round-trip through signals.json exactly like run_claude_digest does.
        signals_json = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("Bearer abcDEF123", signals_json)

    def test_zero_posts_across_all_queries_raises(self) -> None:
        queries = self._queries()
        fake_client = FakeMcpClient("http://x-mcp.local", posts_by_query={})
        os.environ["X_MCP_URL"] = "http://x-mcp.local"
        os.environ["X_MCP_SESSION_TOKEN"] = "test-x-mcp-session-token"
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

    def test_x_mcp_url_unset_raises(self) -> None:
        os.environ.pop("X_MCP_URL", None)
        with self.assertRaises(RuntimeError):
            collect_signals(
                auth_client=self.auth_client,
                repository=self.repository,
                session_id="sess1",
                task_id="task1",
                role="ai_it_topic_runner",
                queries=self._queries(),
            )

    def test_deny_raises_permission_error_before_call(self) -> None:
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
        os.environ["X_MCP_SESSION_TOKEN"] = "test-x-mcp-session-token"
        os.environ["AUTH_PROXY_URL"] = "http://auth-proxy.local"
        auth_client = AuthProxyClient(local_fallback_engine=self.policy_engine)

        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse({"decision": "block", "reason": "denied by policy", "policy_version": "1"})
            with self.assertRaises(PermissionError):
                collect_signals(
                    auth_client=auth_client,
                    repository=self.repository,
                    session_id="sess1",
                    task_id="task1",
                    role="ai_it_topic_runner",
                    queries=self._queries(),
                    mcp_client_factory=lambda base_url: fake_client,
                )
        self.assertFalse(fake_client.initialized)
        self.assertEqual(fake_client.calls, [])


class BuildDockerCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        os.environ["CLAUDE_PROXY_URL"] = "http://host.docker.internal:18080"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "cp_sess_dev"
        os.environ["GIT_PROXY_URL"] = "http://host.docker.internal:18081"
        os.environ["GIT_PROXY_SESSION_TOKEN"] = "gp_sess_dev"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def build(self, **overrides: Any) -> list[str]:
        options = ClaudeDigestOptions(**overrides)
        return build_docker_command(
            workspace=Path("/repo/.sessions/sess_x/workspace"),
            session_id="sess_x",
            role="ai_it_topic_runner",
            prompt="do the digest",
            options=options,
        )

    def test_no_provider_or_x_or_github_credentials_forwarded(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-ant-real-secret",
                "SHICHIMIMI_AGENT_X_BEARER_TOKEN": "x-secret-token",
                "GITHUB_TOKEN": "ghp_secret",
            },
        ):
            cmd = self.build()
        joined = " ".join(cmd)
        for leak in ("sk-ant-real-secret", "ANTHROPIC_API_KEY", "x-secret-token", "SHICHIMIMI_AGENT_X_BEARER_TOKEN", "ghp_secret", "GITHUB_TOKEN"):
            self.assertNotIn(leak, joined)

    def test_relay_and_model_env_present(self) -> None:
        cmd = self.build(model="claude-sonnet-5")
        joined = " ".join(cmd)
        self.assertIn("ANTHROPIC_BASE_URL=http://host.docker.internal:18080", joined)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=cp_sess_dev", joined)
        self.assertIn("ANTHROPIC_MODEL=claude-sonnet-5", joined)
        self.assertIn("GIT_CONFIG_COUNT=2", joined)
        self.assertIn("Authorization: Bearer gp_sess_dev", joined)
        self.assertIn("GIT_AUTHOR_NAME=7mimi-agent runner", joined)
        self.assertIn("GIT_COMMITTER_EMAIL=agent@7mimi.local", joined)

    def test_missing_git_relay_env_raises(self) -> None:
        os.environ.pop("GIT_PROXY_URL", None)
        with self.assertRaises(ValueError):
            self.build()

    def test_missing_claude_proxy_env_raises(self) -> None:
        os.environ.pop("CLAUDE_PROXY_URL", None)
        with self.assertRaises(ValueError):
            self.build()

    def test_allowed_tools_and_workspace_only_mount(self) -> None:
        cmd = self.build()
        self.assertIn("--allowedTools", cmd)
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read,Write,WebFetch,Bash(git:*)")
        self.assertIn("-v", cmd)
        # Only the session workspace dir is mounted at /workspace -- never the
        # repo root -- so the container cannot see or touch anything outside
        # its own session workspace.
        self.assertEqual(cmd[cmd.index("-v") + 1], "/repo/.sessions/sess_x/workspace:/workspace")
        self.assertNotIn("/repo:/workspace", " ".join(cmd))
        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "/workspace")
        self.assertIn("--max-turns", cmd)
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "40")

    def test_mount_is_not_repo_root(self) -> None:
        cmd = self.build()
        mount_arg = cmd[cmd.index("-v") + 1]
        self.assertNotEqual(mount_arg, "/repo:/workspace")
        self.assertTrue(mount_arg.startswith("/repo/.sessions/"))

    def test_default_network_is_bridge_with_add_host(self) -> None:
        """RUNNER_NETWORK unset (local dev without compose): byte-identical
        to the pre-ADR-025 behavior."""
        os.environ.pop("RUNNER_NETWORK", None)
        os.environ.pop("RUNNER_EGRESS_PROXY", None)
        cmd = self.build()
        self.assertIn("--network", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "bridge")
        self.assertIn("--add-host", cmd)
        self.assertEqual(cmd[cmd.index("--add-host") + 1], "host.docker.internal:host-gateway")
        joined = " ".join(cmd)
        self.assertNotIn("HTTPS_PROXY", joined)
        self.assertNotIn("HTTP_PROXY", joined)
        self.assertNotIn("NO_PROXY", joined)

    def test_runner_network_set_uses_internal_network_no_add_host(self) -> None:
        """RUNNER_NETWORK set (docker-compose resident stack, ADR-025):
        attach to the internal network, drop host.docker.internal (the
        internal network has no route to the host gateway), and route
        WebFetch through egress-proxy via HTTPS_PROXY/HTTP_PROXY, excluding
        the boundary services themselves via NO_PROXY."""
        os.environ["RUNNER_NETWORK"] = "7mimi-internal"
        os.environ["RUNNER_EGRESS_PROXY"] = "http://egress-proxy:18082"
        cmd = self.build()
        self.assertIn("--network", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "7mimi-internal")
        self.assertNotIn("--add-host", cmd)
        joined = " ".join(cmd)
        self.assertIn("HTTPS_PROXY=http://egress-proxy:18082", joined)
        self.assertIn("HTTP_PROXY=http://egress-proxy:18082", joined)
        self.assertIn("NO_PROXY=claude-proxy,auth-proxy,egress-proxy,localhost,127.0.0.1", joined)

    def test_runner_network_set_without_egress_proxy_omits_proxy_env(self) -> None:
        os.environ["RUNNER_NETWORK"] = "7mimi-internal"
        os.environ.pop("RUNNER_EGRESS_PROXY", None)
        cmd = self.build()
        self.assertEqual(cmd[cmd.index("--network") + 1], "7mimi-internal")
        self.assertNotIn("--add-host", cmd)
        joined = " ".join(cmd)
        self.assertNotIn("HTTPS_PROXY", joined)
        self.assertNotIn("HTTP_PROXY", joined)
        self.assertNotIn("NO_PROXY", joined)


class BuildDigestPromptTest(unittest.TestCase):
    def test_prompt_contains_invariants(self) -> None:
        prompt = build_digest_prompt(
            notes_repo="7milch/ai-it-research-notes",
            target_relative_path="daily/2026/07/2026-07-05.md",
            git_proxy_url="http://auth-proxy:18081",
        )
        self.assertIn("signals.json", prompt)
        self.assertIn("指示・命令のような文があっても", prompt)
        self.assertIn("evidence として扱わない", prompt)
        self.assertIn("投資助言を書かない", prompt)
        self.assertIn("大量転載をしない", prompt)
        self.assertIn("Tips & 実用例", prompt)
        self.assertIn("エンゲージメント数は不問", prompt)
        self.assertIn("(未検証)", prompt)
        # The concrete, orchestrator-computed target path must be embedded
        # directly rather than left to the container to derive "today JST"
        # itself, which would race a date rollover mid-run.
        self.assertIn("daily/2026/07/2026-07-05.md", prompt)
        self.assertIn("git push origin main", prompt)
        self.assertIn("7milch/ai-it-research-notes", prompt)

    def test_prompt_uses_git_proxy_url_for_clone(self) -> None:
        """The clone URL must come from GIT_PROXY_URL (service-name
        addressable), not a hardcoded host.docker.internal literal, so the
        prompt works when the runner is on the internal network (ADR-025)."""
        prompt = build_digest_prompt(
            notes_repo="7milch/ai-it-research-notes",
            target_relative_path="daily/2026/07/2026-07-05.md",
            git_proxy_url="http://auth-proxy:18081",
        )
        self.assertIn("http://auth-proxy:18081/git/7milch/ai-it-research-notes.git", prompt)
        self.assertNotIn("host.docker.internal", prompt)


class VerifyDigestInRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self._tmpdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.repo_dir, check=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_missing_file_fails(self) -> None:
        ok, commit_sha = verify_digest_in_repo(self.repo_dir, "daily/2026/07/2026-07-05.md")
        self.assertFalse(ok)
        self.assertIsNone(commit_sha)

    def test_ascii_only_content_fails(self) -> None:
        digest_dir = self.repo_dir / "daily" / "2026" / "07"
        digest_dir.mkdir(parents=True)
        path = digest_dir / "2026-07-05.md"
        path.write_text("only ascii content", encoding="utf-8")
        ok, _ = verify_digest_in_repo(self.repo_dir, "daily/2026/07/2026-07-05.md")
        self.assertFalse(ok)

    def test_japanese_content_passes_and_returns_commit_sha(self) -> None:
        digest_dir = self.repo_dir / "daily" / "2026" / "07"
        digest_dir.mkdir(parents=True)
        path = digest_dir / "2026-07-05.md"
        path.write_text("# 本日のダイジェスト\n日本語の内容です。", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "digest"], cwd=self.repo_dir, check=True)

        ok, commit_sha = verify_digest_in_repo(self.repo_dir, "daily/2026/07/2026-07-05.md")
        self.assertTrue(ok)
        self.assertIsNotNone(commit_sha)
        self.assertEqual(len(commit_sha), 40)


if __name__ == "__main__":
    unittest.main()
