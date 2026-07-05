from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from shichimimi_agent.documents.repository_writer import DocumentRepositoryWriter

DOCUMENT_REPOSITORIES = {
    "ai_it_research_notes": {
        "repo": "nishiog/ai-it-research-notes",
        "allowed_paths": ["daily/**", "weekly/**", "topics/**", "queue/**"],
        "denied_paths": [".github/**", ".env", ".env.*", "secrets/**", "config/**"],
    }
}


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class DocumentRepositoryWriterPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        self.bare_repo = self.tmp_path / "remote.git"
        self.bare_repo.mkdir()
        _run("init", "--bare", str(self.bare_repo), cwd=self.tmp_path)

        # Seed the bare repo with an initial commit so pull/push works predictably.
        seed_dir = self.tmp_path / "seed"
        _run("clone", str(self.bare_repo), str(seed_dir), cwd=self.tmp_path)
        _run("config", "user.email", "seed@example.com", cwd=seed_dir)
        _run("config", "user.name", "Seed", cwd=seed_dir)
        (seed_dir / "README.md").write_text("seed\n", encoding="utf-8")
        _run("add", "README.md", cwd=seed_dir)
        _run("commit", "-m", "seed", cwd=seed_dir)
        _run("push", cwd=seed_dir)

        self.root = self.tmp_path / "workspace"
        self.root.mkdir()
        self.writer = DocumentRepositoryWriter(self.root, document_repositories=DOCUMENT_REPOSITORIES)

        # Configure git identity for clones the writer makes, by setting global-ish
        # env-independent config right after clone via a wrapper isn't trivial, so
        # instead pre-seed HOME-scoped git config for the test process.
        self._home_dir = tempfile.TemporaryDirectory()
        import os

        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._home_dir.name
        _run("config", "--global", "user.email", "writer@example.com", cwd=self.tmp_path)
        _run("config", "--global", "user.name", "Writer", cwd=self.tmp_path)
        _run("config", "--global", "init.defaultBranch", "main", cwd=self.tmp_path)

    def tearDown(self) -> None:
        import os

        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._home_dir.cleanup()
        self._tmpdir.cleanup()

    def _clone_and_read(self, relative_path: str) -> tuple[str, str]:
        check_dir = self.tmp_path / "verify"
        if check_dir.exists():
            import shutil

            shutil.rmtree(check_dir)
        _run("clone", str(self.bare_repo), str(check_dir), cwd=self.tmp_path)
        content = (check_dir / relative_path).read_text(encoding="utf-8")
        log = _run("log", "-1", "--pretty=%B", cwd=check_dir).stdout
        return content, log

    def test_publish_clones_writes_commits_and_pushes(self) -> None:
        result = self.writer.publish(
            repo="nishiog/ai-it-research-notes",
            relative_path="daily/2026/07/2026-07-05.md",
            content="# Digest\n",
            commit_message="docs: daily AI/IT digest 2026-07-05 (7mimi-agent)",
            remote_url=str(self.bare_repo),
        )

        self.assertTrue(result.pushed)
        self.assertIsNotNone(result.commit_sha)
        self.assertEqual(result.repo, "nishiog/ai-it-research-notes")

        content, log = self._clone_and_read("daily/2026/07/2026-07-05.md")
        self.assertEqual(content, "# Digest\n")
        self.assertIn("docs: daily AI/IT digest 2026-07-05 (7mimi-agent)", log)
        self.assertIn("Generated-by: 7mimi-agent", log)

    def test_second_publish_with_identical_content_does_not_push(self) -> None:
        kwargs = dict(
            repo="nishiog/ai-it-research-notes",
            relative_path="daily/2026/07/2026-07-05.md",
            content="# Digest\n",
            commit_message="docs: daily AI/IT digest 2026-07-05 (7mimi-agent)",
            remote_url=str(self.bare_repo),
        )
        first = self.writer.publish(**kwargs)
        self.assertTrue(first.pushed)

        second = self.writer.publish(**kwargs)
        self.assertFalse(second.pushed)
        self.assertIsNone(second.commit_sha)

    def test_denied_path_raises_permission_error_and_writes_nothing(self) -> None:
        with self.assertRaises(PermissionError):
            self.writer.publish(
                repo="nishiog/ai-it-research-notes",
                relative_path=".github/workflows/evil.yml",
                content="malicious",
                commit_message="evil",
                remote_url=str(self.bare_repo),
            )

        checkout_dir = self.root / ".data" / "notes-repo" / "ai-it-research-notes"
        self.assertFalse(checkout_dir.exists())

    def test_git_error_scrubs_embedded_credentials(self) -> None:
        writer = DocumentRepositoryWriter(self.root, document_repositories=DOCUMENT_REPOSITORIES)

        with self.assertRaises(RuntimeError) as ctx:
            writer.publish(
                repo="nishiog/ai-it-research-notes",
                relative_path="daily/2026/07/2026-07-05.md",
                content="# Digest\n",
                commit_message="docs",
                remote_url="https://x-access-token:secret@github.com/does/not-exist.git",
            )

        message = str(ctx.exception)
        self.assertIn("https://***@", message)
        self.assertNotIn("secret", message)


if __name__ == "__main__":
    unittest.main()
