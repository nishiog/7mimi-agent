from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shichimimi_agent.config import load_config
from shichimimi_agent.db import Repository, migrate
from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.proxies.slack_notify_client import SlackNotifyClient, SlackNotifyError
from shichimimi_agent.runner.invest_digest import (
    DISCLAIMER_FOOTER,
    INVEST_ALLOWED_TOOLS,
    INVEST_DIRECT_MCP_ALLOWED_TOOLS,
    InvestDigestOptions,
    build_invest_digest_prompt,
    run_invest_digest,
)
from shichimimi_agent.runner.mcp_session import IssuedSession
from shichimimi_agent.security.policy_engine import PolicyEngine
from shichimimi_agent.sessions.workspace import create_workspace


class FakeSlackClient:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.raise_error = raise_error
        self.sent: list[str] = []

    def notify(self, text: str) -> int:
        if self.raise_error:
            raise SlackNotifyError("boom")
        self.sent.append(text)
        return 2


class BuildInvestDigestPromptTest(unittest.TestCase):
    def test_prompt_contains_invariants(self) -> None:
        prompt = build_invest_digest_prompt()
        self.assertIn("投資助言", prompt)
        self.assertIn("暗号資産", prompt)
        self.assertIn("digest.md", prompt)
        self.assertIn("mrkdwn", prompt)
        self.assertNotIn("git push", prompt)
        # ADR-028: direct /mcp collection is the sole flow -- no
        # pre-collected signals.json to reference; cost guardrails present.
        self.assertNotIn("signals.json", prompt)
        self.assertIn("最大 12 回", prompt)
        self.assertIn("max_results", prompt)
        self.assertIn("10 以下", prompt)
        self.assertIn("再試行", prompt)
        self.assertIn("tools/list", prompt)
        self.assertIn("prompt injection", prompt)


class RunInvestDigestEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.auth_client = AuthProxyClient(local_fallback_engine=PolicyEngine(self.config.policy))
        self.session_id = self.repository.create_session(source="test", role="investment_signal_runner", workspace_path="")
        self.task_id = self.repository.create_task(session_id=self.session_id, role="investment_signal_runner", input_data={})
        self.workspace = create_workspace(self.root, self.session_id)

        self._env_backup = dict(os.environ)
        os.environ["X_MCP_URL"] = "http://auth-proxy:18081"
        os.environ["X_MCP_SESSION_TOKEN"] = "static-admin-token"
        os.environ["CLAUDE_PROXY_URL"] = "http://claude-proxy.local"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "test-claude-proxy-session-token"
        os.environ.pop("GIT_PROXY_URL", None)
        os.environ.pop("GIT_PROXY_SESSION_TOKEN", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmpdir.cleanup()

    def _run_with_fake_docker(self, *, digest_content: str | None, returncode: int = 0, slack_client=None):
        job = {"role": "investment_signal_runner", "inputs": {"query_set": "invest_watch"}}

        def fake_run(cmd, cwd, text, capture_output, timeout):
            if digest_content is not None:
                (self.workspace / "digest.md").write_text(digest_content, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="")

        with mock.patch("shichimimi_agent.runner.invest_digest.subprocess.run", side_effect=fake_run), \
             mock.patch("shichimimi_agent.runner.invest_digest.issue_session") as issue_mock:
            issue_mock.return_value = IssuedSession(token="minted-invest-tok", ttl_seconds=2100)
            return run_invest_digest(
                config=self.config,
                repository=self.repository,
                session_id=self.session_id,
                task_id=self.task_id,
                workspace=self.workspace,
                job=job,
                options=InvestDigestOptions(),
                auth_client=self.auth_client,
                slack_client=slack_client or FakeSlackClient(),
            )

    def test_successful_run_appends_footer_and_publishes(self) -> None:
        slack_client = FakeSlackClient()
        result = self._run_with_fake_docker(
            digest_content="*日経平均* 本日の観測整理です。",
            slack_client=slack_client,
        )
        self.assertTrue(result.published)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(slack_client.sent), 1)
        self.assertTrue(slack_client.sent[0].endswith(DISCLAIMER_FOOTER))
        self.assertIn("投資助言", slack_client.sent[0])

    def test_missing_digest_file_fails(self) -> None:
        result = self._run_with_fake_docker(digest_content=None)
        self.assertFalse(result.published)
        self.assertNotEqual(result.exit_code, 0)

    def test_ascii_only_digest_fails(self) -> None:
        result = self._run_with_fake_docker(digest_content="ascii only content, no japanese")
        self.assertFalse(result.published)
        self.assertNotEqual(result.exit_code, 0)

    def test_docker_failure_skips_publish(self) -> None:
        result = self._run_with_fake_docker(digest_content="*日経平均*", returncode=1)
        self.assertFalse(result.published)
        self.assertEqual(result.exit_code, 1)

    def test_slack_notify_failure_marks_unpublished(self) -> None:
        slack_client = FakeSlackClient(raise_error=True)
        result = self._run_with_fake_docker(digest_content="*日経平均*", slack_client=slack_client)
        self.assertFalse(result.published)
        self.assertNotEqual(result.exit_code, 0)

    def test_deny_blocks_publish_and_never_calls_slack(self) -> None:
        """slack.post_digest denied: the publish step must be blocked and
        must never reach SlackNotifyClient."""
        from shichimimi_agent.security.policy_engine import PolicyDecision

        policy_config = self.config.policy
        fallback_client = AuthProxyClient(local_fallback_engine=PolicyEngine(policy_config))

        class DenySlackPostAuthClient:
            def authorize(self, *, session_id, task_id, role, tool_name, arguments=None):
                if tool_name == "slack.post_digest":
                    return PolicyDecision("block", "denied for test")
                return fallback_client.authorize(
                    session_id=session_id, task_id=task_id, role=role, tool_name=tool_name, arguments=arguments
                )

        slack_client = FakeSlackClient()
        job = {"role": "investment_signal_runner", "inputs": {"query_set": "invest_watch"}}

        def fake_run(cmd, cwd, text, capture_output, timeout):
            (self.workspace / "digest.md").write_text("*日経平均*", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch("shichimimi_agent.runner.invest_digest.subprocess.run", side_effect=fake_run), \
             mock.patch("shichimimi_agent.runner.invest_digest.issue_session") as issue_mock:
            issue_mock.return_value = IssuedSession(token="minted-invest-tok", ttl_seconds=2100)
            result = run_invest_digest(
                config=self.config,
                repository=self.repository,
                session_id=self.session_id,
                task_id=self.task_id,
                workspace=self.workspace,
                job=job,
                options=InvestDigestOptions(),
                auth_client=DenySlackPostAuthClient(),
                slack_client=slack_client,
            )
        self.assertFalse(result.published)
        self.assertEqual(slack_client.sent, [])

    def test_no_git_relay_env_required(self) -> None:
        # Sanity check that invest-digest never requires GIT_PROXY_* env,
        # unlike claude-digest (ADR-026: no git relay for this job).
        self.assertNotIn("GIT_PROXY_URL", os.environ)
        result = self._run_with_fake_docker(digest_content="*日経平均*")
        self.assertTrue(result.published)

    def test_mcp_config_minted_with_investment_signal_runner_role(self) -> None:
        with mock.patch("shichimimi_agent.runner.invest_digest.subprocess.run") as run_mock, \
             mock.patch("shichimimi_agent.runner.invest_digest.issue_session") as issue_mock:
            issue_mock.return_value = IssuedSession(token="minted-invest-tok", ttl_seconds=2100)

            def fake_run(cmd, **kwargs):
                (self.workspace / "digest.md").write_text("*日経平均*", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            run_mock.side_effect = fake_run
            job = {"role": "investment_signal_runner", "inputs": {"query_set": "invest_watch"}}
            run_invest_digest(
                config=self.config,
                repository=self.repository,
                session_id=self.session_id,
                task_id=self.task_id,
                workspace=self.workspace,
                job=job,
                options=InvestDigestOptions(),
                auth_client=self.auth_client,
                slack_client=FakeSlackClient(),
            )

        issue_mock.assert_called_once()
        _, kwargs = issue_mock.call_args
        self.assertEqual(kwargs["role"], "investment_signal_runner")

        self.assertFalse((self.workspace / "signals.json").exists())
        self.assertTrue((self.workspace / ".mcp.json").exists())
        written = json.loads((self.workspace / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(written["mcpServers"]["x7mimi"]["headers"]["Authorization"], "Bearer minted-invest-tok")

        docker_cmd = run_mock.call_args.args[0]
        allowed_idx = docker_cmd.index("--allowedTools")
        self.assertEqual(docker_cmd[allowed_idx + 1], INVEST_DIRECT_MCP_ALLOWED_TOOLS)
        self.assertIn("--mcp-config", docker_cmd)
        self.assertNotIn("GIT_CONFIG_COUNT", " ".join(docker_cmd))

    def test_missing_x_mcp_url_raises(self) -> None:
        del os.environ["X_MCP_URL"]
        job = {"role": "investment_signal_runner", "inputs": {"query_set": "invest_watch"}}
        with self.assertRaises(ValueError):
            run_invest_digest(
                config=self.config,
                repository=self.repository,
                session_id=self.session_id,
                task_id=self.task_id,
                workspace=self.workspace,
                job=job,
                options=InvestDigestOptions(),
                auth_client=self.auth_client,
                slack_client=FakeSlackClient(),
            )


class BuildDockerCommandInvestFlavorTest(unittest.TestCase):
    """Confirms the claude_digest.build_docker_command refactor (allowed_tools /
    include_git_relay params) produces the invest-digest shape: no git relay
    env, Read/Write/WebFetch only."""

    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        os.environ["CLAUDE_PROXY_URL"] = "http://claude-proxy.local"
        os.environ["CLAUDE_PROXY_SESSION_TOKEN"] = "test-token"
        os.environ.pop("GIT_PROXY_URL", None)
        os.environ.pop("GIT_PROXY_SESSION_TOKEN", None)
        os.environ.pop("RUNNER_NETWORK", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_no_git_relay_env_and_invest_allowed_tools(self) -> None:
        from shichimimi_agent.runner.claude_digest import ClaudeDigestOptions, build_docker_command

        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = build_docker_command(
                workspace=Path(tmpdir),
                session_id="sess1",
                role="investment_signal_runner",
                prompt="do the thing",
                options=ClaudeDigestOptions(),
                allowed_tools=INVEST_ALLOWED_TOOLS,
                include_git_relay=False,
            )
        joined = " ".join(cmd)
        self.assertIn("Read,Write,WebFetch", joined)
        self.assertNotIn("GIT_CONFIG_COUNT", joined)
        self.assertNotIn("GIT_AUTHOR_NAME", joined)

    def test_default_still_includes_git_relay(self) -> None:
        from shichimimi_agent.runner.claude_digest import ClaudeDigestOptions, build_docker_command

        os.environ["GIT_PROXY_URL"] = "http://git-proxy.local"
        os.environ["GIT_PROXY_SESSION_TOKEN"] = "test-git-token"
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = build_docker_command(
                workspace=Path(tmpdir),
                session_id="sess2",
                role="ai_it_topic_runner",
                prompt="do the thing",
                options=ClaudeDigestOptions(),
            )
        joined = " ".join(cmd)
        self.assertIn("GIT_CONFIG_COUNT", joined)


class SlackNotifyClientTest(unittest.TestCase):
    def test_notify_raises_on_transport_failure(self) -> None:
        client = SlackNotifyClient(base_url="http://127.0.0.1:1", session_token="tok", timeout_seconds=0.2)
        with self.assertRaises(SlackNotifyError):
            client.notify("hello")


if __name__ == "__main__":
    unittest.main()
