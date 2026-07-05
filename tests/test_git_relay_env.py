from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from shichimimi_agent.runner.claude_smoke import ClaudeSmokeOptions, build_docker_command
from shichimimi_agent.runner.git_relay_env import build_git_relay_env


class BuildGitRelayEnvTest(unittest.TestCase):
    def test_exact_keys_and_values(self) -> None:
        env = build_git_relay_env(proxy_url="http://127.0.0.1:18081/", session_token="sess_tok_123")
        self.assertEqual(
            env,
            {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "http.http://127.0.0.1:18081/.extraheader",
                "GIT_CONFIG_VALUE_0": "Authorization: Bearer sess_tok_123",
                "GIT_CONFIG_KEY_1": "credential.helper",
                "GIT_CONFIG_VALUE_1": "",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )

    def test_proxy_url_without_trailing_slash(self) -> None:
        env = build_git_relay_env(proxy_url="http://127.0.0.1:18081", session_token="tok")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.http://127.0.0.1:18081/.extraheader")


class ClaudeSmokeGitRelayOptInTest(unittest.TestCase):
    def build(self) -> list[str]:
        return build_docker_command(
            root=Path("/repo"),
            session_id="sess_x",
            role="ai_it_topic_runner",
            workspace_rel=".sessions/sess_x/workspace",
            prompt="do something small",
            options=ClaudeSmokeOptions(),
        )

    def test_git_relay_env_absent_when_git_proxy_url_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GIT_PROXY_URL", None)
            os.environ.pop("GIT_PROXY_SESSION_TOKEN", None)
            cmd = self.build()
        joined = " ".join(cmd)
        self.assertNotIn("GIT_CONFIG_COUNT", joined)
        self.assertNotIn("GIT_TERMINAL_PROMPT", joined)

    def test_git_relay_env_present_when_git_proxy_url_and_token_set(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"GIT_PROXY_URL": "http://127.0.0.1:18081/", "GIT_PROXY_SESSION_TOKEN": "relay_tok"},
        ):
            cmd = self.build()
        joined = " ".join(cmd)
        self.assertIn("GIT_CONFIG_COUNT=2", joined)
        self.assertIn("GIT_CONFIG_KEY_0=http.http://127.0.0.1:18081/.extraheader", joined)
        self.assertIn("GIT_CONFIG_VALUE_0=Authorization: Bearer relay_tok", joined)
        self.assertIn("GIT_CONFIG_KEY_1=credential.helper", joined)
        self.assertIn("GIT_TERMINAL_PROMPT=0", joined)

    def test_git_proxy_url_without_token_raises(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_PROXY_URL": "http://127.0.0.1:18081/"}):
            os.environ.pop("GIT_PROXY_SESSION_TOKEN", None)
            with self.assertRaises(ValueError):
                self.build()

    def test_anthropic_api_key_still_absent_regression(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-ant-real-secret",
                "GIT_PROXY_URL": "http://127.0.0.1:18081/",
                "GIT_PROXY_SESSION_TOKEN": "relay_tok",
            },
        ):
            cmd = self.build()
        joined = " ".join(cmd)
        self.assertNotIn("sk-ant-real-secret", joined)
        self.assertNotIn("ANTHROPIC_API_KEY", joined)


if __name__ == "__main__":
    unittest.main()
