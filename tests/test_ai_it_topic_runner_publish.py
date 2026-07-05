from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shichimimi_agent.config import load_config
from shichimimi_agent.db import Repository, migrate
from shichimimi_agent.documents.repository_writer import WriteResult
from shichimimi_agent.roles.ai_it_topic_runner import AiItTopicRunner
from shichimimi_agent.security.policy_engine import PolicyEngine


class StubWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.publish_calls: list[dict] = []
        self.write_dry_run_calls: list[dict] = []

    def publish(self, *, repo, relative_path, content, commit_message, remote_url=None):
        self.publish_calls.append(
            {"repo": repo, "relative_path": relative_path, "content": content, "commit_message": commit_message}
        )
        return WriteResult(path=self.root / relative_path, repo=repo, pushed=True, commit_sha="deadbeef")

    def write_dry_run(self, *, relative_path, content):
        self.write_dry_run_calls.append({"relative_path": relative_path, "content": content})
        return WriteResult(path=self.root / "dry-run" / relative_path, repo=None, pushed=False)


class AiItTopicRunnerPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.policy_engine = PolicyEngine(self.config.policy)
        self.job = {
            "role": "ai_it_topic_runner",
            "inputs": {"query_set": "ai_it_watch"},
            "output": {"repo": "nishiog/ai-it-research-notes"},
        }

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_dry_run_false_calls_publish_and_records_metadata(self) -> None:
        runner = AiItTopicRunner(config=self.config, repository=self.repository, policy_engine=self.policy_engine)
        stub = StubWriter(self.root)
        runner.writer = stub

        result = runner.run_daily_digest(session_id="sess1", task_id="task1", job=self.job, dry_run=False)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(stub.publish_calls), 1)
        self.assertEqual(len(stub.write_dry_run_calls), 0)
        self.assertIn("(7mimi-agent)", stub.publish_calls[0]["commit_message"])

        docs = self.repository.list_documents() if hasattr(self.repository, "list_documents") else None
        # Fall back to raw connection query if there is no convenience accessor.
        if docs is None:
            with self.repository._connect() as conn:  # type: ignore[attr-defined]
                row = conn.execute("SELECT status, metadata_json FROM documents ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = docs[-1]
        self.assertIsNotNone(row)

    def test_dry_run_default_regression_unchanged(self) -> None:
        runner = AiItTopicRunner(config=self.config, repository=self.repository, policy_engine=self.policy_engine)
        stub = StubWriter(self.root)
        runner.writer = stub

        result = runner.run_daily_digest(session_id="sess2", task_id="task2", job=self.job, dry_run=True)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(stub.write_dry_run_calls), 1)
        self.assertEqual(len(stub.publish_calls), 0)


if __name__ == "__main__":
    unittest.main()
