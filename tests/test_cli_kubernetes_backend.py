"""CLI wiring for the Kubernetes runner backend (Issue #29):

- `run-job --runner kubernetes` selects KubernetesRunnerBackend, and
  `RUNNER_BACKEND` env var supplies the default when --runner is omitted.
- `runner-execute` (cmd_runner_execute) writes the result payload to both
  stdout and `.sessions/<session_id>/result.json`, for success and failure,
  since KubernetesRunnerBackend collects results from that file rather than
  from Pod stdout (see kubernetes_runner.py / cli.py comments).
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shichimimi_agent.cli import build_parser, cmd_run_job, cmd_runner_execute
from shichimimi_agent.runner.backend import RunnerExecutionResult


class RunnerFlagParsingTest(unittest.TestCase):
    def _parse_run_job(self, extra: list[str]):
        parser = build_parser()
        return parser.parse_args(["run-job", "ai-it-x-daily-digest", *extra])

    def test_kubernetes_is_an_accepted_runner_choice(self) -> None:
        args = self._parse_run_job(["--runner", "kubernetes"])
        self.assertEqual(args.runner, "kubernetes")

    def test_invalid_runner_choice_still_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse_run_job(["--runner", "not-a-real-backend"])

    def test_runner_defaults_to_local_when_env_unset(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("RUNNER_BACKEND", None)
            args = self._parse_run_job([])
        self.assertEqual(args.runner, "local")

    def test_runner_backend_env_var_supplies_default(self) -> None:
        with mock.patch.dict("os.environ", {"RUNNER_BACKEND": "kubernetes"}, clear=False):
            args = self._parse_run_job([])
        self.assertEqual(args.runner, "kubernetes")

    def test_explicit_runner_flag_overrides_env_var(self) -> None:
        with mock.patch.dict("os.environ", {"RUNNER_BACKEND": "kubernetes"}, clear=False):
            args = self._parse_run_job(["--runner", "container"])
        self.assertEqual(args.runner, "container")


class RunJobKubernetesDispatchTest(unittest.TestCase):
    """Verifies --runner kubernetes actually reaches KubernetesRunnerBackend
    (and only that backend), by stubbing out everything cmd_run_job touches
    around backend selection."""

    def test_run_job_with_kubernetes_runner_dispatches_to_kubernetes_backend(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["run-job", "ai-it-x-daily-digest", "--runner", "kubernetes"])

        stub_config = SimpleNamespace(root=Path("/fake/root"))
        stub_task = mock.Mock(name="stub_task")
        backend_instance = mock.Mock(name="kubernetes_backend_instance")
        backend_instance.run_task.return_value = RunnerExecutionResult(status="succeeded", payload={"status": "succeeded"})

        with mock.patch("shichimimi_agent.cli._load_validated_config", return_value=stub_config), \
             mock.patch("shichimimi_agent.cli.default_db_path", return_value=Path("/fake/db.sqlite")), \
             mock.patch("shichimimi_agent.cli.migrate"), \
             mock.patch("shichimimi_agent.cli.Repository"), \
             mock.patch("shichimimi_agent.cli._prepare_task", return_value=stub_task), \
             mock.patch("shichimimi_agent.cli._finalize_task") as finalize_task, \
             mock.patch("shichimimi_agent.cli.KubernetesRunnerBackend", return_value=backend_instance) as k8s_backend_cls, \
             mock.patch("shichimimi_agent.cli.ContainerRunnerBackend") as container_backend_cls, \
             mock.patch("shichimimi_agent.cli.LocalRunnerBackend") as local_backend_cls, \
             contextlib.redirect_stdout(io.StringIO()):
            exit_code = cmd_run_job(args)

        self.assertEqual(exit_code, 0)
        k8s_backend_cls.assert_called_once_with(root=stub_config.root)
        backend_instance.run_task.assert_called_once_with(stub_task)
        container_backend_cls.assert_not_called()
        local_backend_cls.assert_not_called()
        finalize_task.assert_called_once()
        self.assertEqual(finalize_task.call_args.kwargs.get("status"), "succeeded")

    def test_run_job_with_local_runner_still_does_not_touch_kubernetes_backend(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["run-job", "ai-it-x-daily-digest", "--runner", "local"])
        stub_config = SimpleNamespace(root=Path("/fake/root"))
        stub_task = mock.Mock(name="stub_task")

        with mock.patch("shichimimi_agent.cli._load_validated_config", return_value=stub_config), \
             mock.patch("shichimimi_agent.cli.default_db_path", return_value=Path("/fake/db.sqlite")), \
             mock.patch("shichimimi_agent.cli.migrate"), \
             mock.patch("shichimimi_agent.cli.Repository"), \
             mock.patch("shichimimi_agent.cli._prepare_task", return_value=stub_task), \
             mock.patch("shichimimi_agent.cli._finalize_task"), \
             mock.patch("shichimimi_agent.cli.KubernetesRunnerBackend") as k8s_backend_cls, \
             mock.patch("shichimimi_agent.cli.LocalRunnerBackend") as local_backend_cls, \
             contextlib.redirect_stdout(io.StringIO()):
            local_backend_cls.return_value.run_task.return_value = RunnerExecutionResult(
                status="succeeded", payload={"status": "succeeded"}
            )
            cmd_run_job(args)

        k8s_backend_cls.assert_not_called()
        local_backend_cls.assert_called_once()


def _runner_execute_args(runner_root: Path, session_id: str, *, dry_run: bool = False):
    parser = build_parser()
    argv = [
        "runner-execute",
        "ai-it-x-daily-digest",
        "--session-id",
        session_id,
        "--task-id",
        "task_test",
        "--runner-root",
        str(runner_root),
    ]
    if dry_run:
        argv.append("--dry-run")
    return parser.parse_args(argv)


class CmdRunnerExecuteResultFileTest(unittest.TestCase):
    """cmd_runner_execute must write .sessions/<session_id>/result.json in
    addition to printing to stdout, for both success and failure, so that
    KubernetesRunnerBackend (which cannot capture Job Pod stdout) can
    collect the result from the shared PVC."""

    def test_success_writes_result_json_file_with_expected_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "sess_result_file_success_content"
            args = _runner_execute_args(root, session_id, dry_run=True)
            stub_config = SimpleNamespace(root=root)
            stub_result = RunnerExecutionResult(
                status="succeeded",
                payload={"status": "succeeded", "path": "daily/2026/07/2026-07-16.md", "source_refs": []},
            )

            stdout = io.StringIO()
            with mock.patch("shichimimi_agent.cli._load_validated_config", return_value=stub_config), \
                 mock.patch("shichimimi_agent.cli.default_db_path", return_value=root / "app.sqlite"), \
                 mock.patch("shichimimi_agent.cli.migrate"), \
                 mock.patch("shichimimi_agent.cli.Repository"), \
                 mock.patch("shichimimi_agent.cli._find_job", return_value={"role": "ai_it_topic_runner"}), \
                 mock.patch("shichimimi_agent.cli.execute_runner_task", return_value=stub_result), \
                 contextlib.redirect_stdout(stdout):
                exit_code = cmd_runner_execute(args)

            result_path = root / ".sessions" / session_id / "result.json"
            self.assertTrue(result_path.exists())
            file_payload = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(file_payload, stub_result.payload)
        stdout_payload = json.loads(stdout.getvalue())
        self.assertEqual(stdout_payload, stub_result.payload)

    def test_failure_writes_result_json_with_error_and_returns_exit_code_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "sess_result_file_failure"
            args = _runner_execute_args(root, session_id)
            stub_config = SimpleNamespace(root=root)

            stdout = io.StringIO()
            with mock.patch("shichimimi_agent.cli._load_validated_config", return_value=stub_config), \
                 mock.patch("shichimimi_agent.cli.default_db_path", return_value=root / "app.sqlite"), \
                 mock.patch("shichimimi_agent.cli.migrate"), \
                 mock.patch("shichimimi_agent.cli.Repository"), \
                 mock.patch("shichimimi_agent.cli._find_job", return_value={"role": "ai_it_topic_runner"}), \
                 mock.patch(
                     "shichimimi_agent.cli.execute_runner_task",
                     side_effect=RuntimeError("simulated runner failure"),
                 ), \
                 contextlib.redirect_stdout(stdout):
                exit_code = cmd_runner_execute(args)

            result_path = root / ".sessions" / session_id / "result.json"
            self.assertTrue(result_path.exists())
            file_payload = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(file_payload["status"], "failed")
        self.assertEqual(file_payload["error"]["type"], "RuntimeError")
        self.assertIn("simulated runner failure", file_payload["error"]["message"])
        stdout_payload = json.loads(stdout.getvalue())
        self.assertEqual(stdout_payload, file_payload)

    def test_result_file_written_under_correct_session_id_only(self) -> None:
        """Regression guard: the result file must be scoped to this task's
        own session id, not some other in-flight session sharing the PVC."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "sess_scope_check"
            other_session_dir = root / ".sessions" / "sess_unrelated"
            other_session_dir.mkdir(parents=True)

            args = _runner_execute_args(root, session_id, dry_run=True)
            stub_config = SimpleNamespace(root=root)
            stub_result = RunnerExecutionResult(status="succeeded", payload={"status": "succeeded"})

            with mock.patch("shichimimi_agent.cli._load_validated_config", return_value=stub_config), \
                 mock.patch("shichimimi_agent.cli.default_db_path", return_value=root / "app.sqlite"), \
                 mock.patch("shichimimi_agent.cli.migrate"), \
                 mock.patch("shichimimi_agent.cli.Repository"), \
                 mock.patch("shichimimi_agent.cli._find_job", return_value={"role": "ai_it_topic_runner"}), \
                 mock.patch("shichimimi_agent.cli.execute_runner_task", return_value=stub_result), \
                 contextlib.redirect_stdout(io.StringIO()):
                cmd_runner_execute(args)

            self.assertTrue((root / ".sessions" / session_id / "result.json").exists())
            self.assertFalse((other_session_dir / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
