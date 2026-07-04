from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shichimimi_agent.config import load_config
from shichimimi_agent.db import Repository, migrate
from shichimimi_agent.roles.ai_it_topic_runner import AiItTopicRunner
from shichimimi_agent.security.policy_engine import PolicyEngine


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class AiItTopicRunnerAuthWiringTest(unittest.TestCase):
    """Verifies AiItTopicRunner's default auth_client picks up AUTH_PROXY_URL from
    the environment (Refs #4) rather than always going straight to the local
    PolicyEngine, and that a deny decision from auth-proxy is enforced end to end."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.policy_engine = PolicyEngine(self.config.policy)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_default_auth_client_reads_auth_proxy_url_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"AUTH_PROXY_URL": "http://auth-proxy.local"}, clear=False):
            runner = AiItTopicRunner(config=self.config, repository=self.repository, policy_engine=self.policy_engine)

        self.assertTrue(runner.auth_client.remote_enabled)
        self.assertEqual(runner.auth_client.base_url, "http://auth-proxy.local")

    def test_no_env_uses_local_fallback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTH_PROXY_URL", None)
            runner = AiItTopicRunner(config=self.config, repository=self.repository, policy_engine=self.policy_engine)

        self.assertFalse(runner.auth_client.remote_enabled)
        self.assertIs(runner.auth_client.local_fallback_engine, self.policy_engine)

    def test_auth_proxy_deny_blocks_run_daily_digest(self) -> None:
        with mock.patch.dict(os.environ, {"AUTH_PROXY_URL": "http://auth-proxy.local"}, clear=False):
            runner = AiItTopicRunner(config=self.config, repository=self.repository, policy_engine=self.policy_engine)

        job = {
            "role": "ai_it_topic_runner",
            "inputs": {"query_set": "ai_it_watch"},
            "output": {"repo": "nishiog/ai-it-research-notes"},
        }

        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse(
                {"decision": "block", "reason": "denied by policy", "policy_version": "1"}
            )
            with self.assertRaises(PermissionError):
                runner.run_daily_digest(session_id="sess_test", task_id="task_test", job=job, dry_run=True)


if __name__ == "__main__":
    unittest.main()
