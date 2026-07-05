from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from shichimimi_agent.cli import build_parser, cmd_run_job
from shichimimi_agent.documents.repository_writer import DocumentRepositoryWriter
from shichimimi_agent.security.path_policy import is_path_allowed

DOCUMENT_REPOSITORIES = {
    "ai_it_research_notes": {
        "repo": "nishiog/ai-it-research-notes",
        "allowed_paths": ["daily/**", "weekly/**", "topics/**", "queue/**"],
        "denied_paths": [".github/**", ".env", ".env.*", "secrets/**", "config/**"],
    }
}


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class CliPublishFlagSemanticsTest(unittest.TestCase):
    """Verifies run-job --publish / --dry-run dispatch semantics per issue #8 spec."""

    def _parse(self, extra: list[str]):
        parser = build_parser()
        return parser.parse_args(["run-job", "ai-it-x-daily-digest", *extra])

    def test_no_flags_defaults_to_dry_run(self) -> None:
        args = self._parse([])
        effective_dry_run = not args.publish or args.dry_run
        self.assertTrue(effective_dry_run)

    def test_publish_alone_disables_dry_run(self) -> None:
        args = self._parse(["--publish"])
        effective_dry_run = not args.publish or args.dry_run
        self.assertFalse(effective_dry_run)

    def test_publish_and_dry_run_together_keeps_dry_run(self) -> None:
        args = self._parse(["--publish", "--dry-run"])
        effective_dry_run = not args.publish or args.dry_run
        self.assertTrue(effective_dry_run)

    def test_runner_execute_subcommand_has_no_publish_flag(self) -> None:
        # Container path (runner-execute) must not gain an implicit publish
        # capability: there is no --publish flag threaded to it at all.
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "runner-execute",
                    "ai-it-x-daily-digest",
                    "--session-id",
                    "s1",
                    "--task-id",
                    "t1",
                    "--publish",
                ]
            )


    def test_publish_with_container_runner_is_rejected_before_any_backend_work(self) -> None:
        args = self._parse(["--publish", "--runner", "container"])

        with mock.patch("shichimimi_agent.cli._load_validated_config") as load_config, \
             mock.patch("shichimimi_agent.cli.ContainerRunnerBackend") as container_backend, \
             mock.patch("shichimimi_agent.cli.LocalRunnerBackend") as local_backend:
            exit_code = cmd_run_job(args)

        self.assertNotEqual(exit_code, 0)
        load_config.assert_not_called()
        container_backend.assert_not_called()
        local_backend.assert_not_called()


class PathPolicyAllowedCasesTest(unittest.TestCase):
    def test_all_declared_allowed_globs_are_accepted(self) -> None:
        allowed = ["daily/**", "weekly/**", "topics/**", "queue/**"]
        denied = [".github/**", ".env", ".env.*", "secrets/**", "config/**"]
        for path in [
            "daily/2026/07/2026-07-05.md",
            "weekly/2026-W27.md",
            "topics/llm-agents.md",
            "queue/pending.md",
        ]:
            with self.subTest(path=path):
                decision = is_path_allowed(path, allowed=allowed, denied=denied)
                self.assertTrue(decision.allowed, decision.reason)

    def test_plain_denied_paths_rejected(self) -> None:
        allowed = ["daily/**"]
        denied = [".github/**", "secrets/**"]
        for path in [".github/workflows/evil.yml", "secrets/token.txt"]:
            with self.subTest(path=path):
                decision = is_path_allowed(path, allowed=allowed, denied=denied)
                self.assertFalse(decision.allowed)

    def test_dot_dot_traversal_is_rejected(self) -> None:
        """
        is_path_allowed() normalizes with posixpath.normpath() before matching,
        so a path like "daily/../.github/evil.yml" collapses to
        ".github/evil.yml" and is correctly rejected by the denied ".github/**"
        pattern instead of being allowed via the "daily/**" prefix.
        """
        allowed = ["daily/**", "weekly/**", "topics/**", "queue/**"]
        denied = [".github/**", ".env", ".env.*", "secrets/**", "config/**"]
        decision = is_path_allowed("daily/../.github/evil.yml", allowed=allowed, denied=denied)
        self.assertFalse(decision.allowed)

    def test_BUG_absolute_path_traversal_also_not_caught_by_denied_but_at_least_not_allowed(self) -> None:
        allowed = ["daily/**"]
        denied = [".github/**"]
        decision = is_path_allowed("/etc/passwd", allowed=allowed, denied=denied)
        self.assertFalse(decision.allowed)


class RepositoryWriterGitLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        self.bare_repo = self.tmp_path / "remote.git"
        self.bare_repo.mkdir()
        _run("init", "--bare", str(self.bare_repo), cwd=self.tmp_path)

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

        import os

        self._home_dir = tempfile.TemporaryDirectory()
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

    def test_second_publish_reuses_existing_checkout_via_pull_ff_only(self) -> None:
        checkout_dir = self.root / ".data" / "notes-repo" / "ai-it-research-notes"

        self.writer.publish(
            repo="nishiog/ai-it-research-notes",
            relative_path="daily/2026/07/2026-07-05.md",
            content="# Digest 1\n",
            commit_message="docs: day 1",
            remote_url=str(self.bare_repo),
        )
        self.assertTrue((checkout_dir / ".git").exists())
        first_git_dir_ctime = (checkout_dir / ".git").stat().st_ino

        # Second publish call, different content -> should pull --ff-only
        # against the existing checkout rather than re-cloning.
        second = self.writer.publish(
            repo="nishiog/ai-it-research-notes",
            relative_path="daily/2026/07/2026-07-06.md",
            content="# Digest 2\n",
            commit_message="docs: day 2",
            remote_url=str(self.bare_repo),
        )
        self.assertTrue(second.pushed)
        # Same checkout directory / .git inode reused (not re-cloned).
        self.assertEqual((checkout_dir / ".git").stat().st_ino, first_git_dir_ctime)

        # Both files present in the checkout.
        self.assertTrue((checkout_dir / "daily/2026/07/2026-07-05.md").exists())
        self.assertTrue((checkout_dir / "daily/2026/07/2026-07-06.md").exists())

    def test_publish_with_dot_dot_traversal_raises_permission_error_and_writes_nothing(self) -> None:
        with self.assertRaises(PermissionError):
            self.writer.publish(
                repo="nishiog/ai-it-research-notes",
                relative_path="daily/../.github/evil.yml",
                content="malicious",
                commit_message="evil",
                remote_url=str(self.bare_repo),
            )

        checkout_dir = self.root / ".data" / "notes-repo" / "ai-it-research-notes"
        # No checkout should have been created (path rejected before clone/write),
        # and in particular no file should land under .github/.
        github_dir = checkout_dir / ".github"
        self.assertFalse(github_dir.exists())

    def test_push_failure_after_remote_removed_raises_scrubbed_runtime_error(self) -> None:
        # First publish succeeds and creates the checkout + remote tracking.
        self.writer.publish(
            repo="nishiog/ai-it-research-notes",
            relative_path="daily/2026/07/2026-07-05.md",
            content="# Digest\n",
            commit_message="docs: day 1",
            remote_url=str(self.bare_repo),
        )

        # Simulate credential-laden remote URL so we can also assert scrubbing,
        # and remove the bare repo out from under the checkout to force a push
        # failure on the next publish call.
        checkout_dir = self.root / ".data" / "notes-repo" / "ai-it-research-notes"
        _run(
            "remote",
            "set-url",
            "origin",
            "https://x-access-token:supersecret@127.0.0.1/does-not-exist.git",
            cwd=checkout_dir,
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.writer.publish(
                repo="nishiog/ai-it-research-notes",
                relative_path="daily/2026/07/2026-07-06.md",
                content="# Digest 2\n",
                commit_message="docs: day 2",
                remote_url=str(self.bare_repo),
            )
        message = str(ctx.exception)
        self.assertNotIn("supersecret", message)


class ResolvedPathContainmentTest(unittest.TestCase):
    """Direct test of the resolve()/is_relative_to() defense-in-depth check in
    publish(), independent of the path_policy allow/deny logic (which is
    monkeypatched here to simulate it wrongly allowing an escaping path)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        self.bare_repo = self.tmp_path / "remote.git"
        self.bare_repo.mkdir()
        _run("init", "--bare", str(self.bare_repo), cwd=self.tmp_path)

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

        import os

        self._home_dir = tempfile.TemporaryDirectory()
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

    def test_escaping_path_is_rejected_even_if_policy_wrongly_allows_it(self) -> None:
        from shichimimi_agent.documents import repository_writer as repository_writer_module
        from shichimimi_agent.security.path_policy import PathDecision

        with mock.patch.object(
            repository_writer_module,
            "is_path_allowed",
            return_value=PathDecision(True, "allowed (simulated bug)"),
        ):
            with self.assertRaises(PermissionError):
                self.writer.publish(
                    repo="nishiog/ai-it-research-notes",
                    relative_path="../../etc/evil.md",
                    content="malicious",
                    commit_message="evil",
                    remote_url=str(self.bare_repo),
                )

        escaped_path = self.root / ".data" / "etc" / "evil.md"
        self.assertFalse(escaped_path.exists())


if __name__ == "__main__":
    unittest.main()
