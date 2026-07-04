from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from sevenmimi_agent.runner import ContainerRunnerBackend, ContainerRunnerOptions, RunnerTask


class Completed:
    returncode = 0
    stdout = json.dumps({"status": "succeeded", "path": ".data/dry-run/example.md"})
    stderr = ""


class ContainerRunnerTest(unittest.TestCase):
    def test_docker_command_does_not_forward_provider_or_external_credentials(self) -> None:
        task = RunnerTask(
            job_name="ai-it-x-daily-digest",
            job={"role": "ai_it_topic_runner"},
            session_id="sess_test",
            task_id="task_test",
            role="ai_it_topic_runner",
            dry_run=True,
        )
        backend = ContainerRunnerBackend(root=Path.cwd(), options=ContainerRunnerOptions(image="test-image", docker_bin="docker"))
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-ant-secret",
                "X_API_KEY": "x-secret",
                "JQUANTS_API_KEY": "jq-secret",
                "GITHUB_TOKEN": "gh-secret",
                "CLAUDE_PROXY_SESSION_TOKEN": "cp_sess_allowed",
            },
            clear=False,
        ):
            with patch("subprocess.run", return_value=Completed()) as run:
                result = backend.run_task(task)

        self.assertEqual(result.status, "succeeded")
        cmd = run.call_args.args[0]
        joined = " ".join(cmd)
        self.assertIn("CLAUDE_PROXY_SESSION_TOKEN=cp_sess_allowed", joined)
        self.assertIn("PYTHONPATH=/workspace/src", joined)
        self.assertNotIn("sk-ant-secret", joined)
        self.assertNotIn("x-secret", joined)
        self.assertNotIn("jq-secret", joined)
        self.assertNotIn("gh-secret", joined)
        self.assertIn("--network none", joined)


if __name__ == "__main__":
    unittest.main()
