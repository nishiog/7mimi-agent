"""Unit tests for KubernetesRunnerBackend (Issue #29 / k3s + ArgoCD).

Focus areas (see the class docstring in kubernetes_runner.py and the long
comment in deploy/k8s/kustomization.yaml for the design this backs):

- env allowlist: only SESSION_ID/ROLE/WORKSPACE_DIR/PYTHONPATH/proxy URLs
  (+ session tokens when present) ever reach the Job spec -- credentials
  like ANTHROPIC_API_KEY must never leak into it.
- the ServiceAccount token is re-read from disk on every API request
  (BoundServiceAccountToken rotation).
- Job manifest shape: backoffLimit=0, restartPolicy=Never,
  ttlSecondsAfterFinished, runner label, nodeSelector, PVC + ConfigMap
  mounts, and -- critically -- no ArgoCD app.kubernetes.io/instance
  tracking label (runner Jobs must stay outside ArgoCD's tracked set).
- completion polling: succeeded / failed / timeout, with RuntimeError on
  failure and on timeout.
- result collection reads `.sessions/<session_id>/result.json` from the
  shared PVC, never Pod logs.
- ConfigMap name resolution: explicit RUNNER_CONFIGMAP_NAME override, and
  the self-Pod-spec-read-back fallback.

The HTTP layer is exercised two ways: most tests mock `_api_request`
directly (the seam the implementation was deliberately split out for), and
a couple of tests mock `urllib.request.urlopen` underneath a stubbed
`_ssl_context` to prove the token is actually re-read per call at the
transport layer.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shichimimi_agent.runner import KubernetesRunnerBackend, KubernetesRunnerOptions, RunnerTask
from shichimimi_agent.runner.backend import RunnerExecutionResult


def _task(session_id: str = "sess_k8s_test", dry_run: bool = True) -> RunnerTask:
    return RunnerTask(
        job_name="ai-it-x-daily-digest",
        job={"role": "ai_it_topic_runner"},
        session_id=session_id,
        task_id="task_k8s_test",
        role="ai_it_topic_runner",
        dry_run=dry_run,
    )


def _options(tmp_root: Path, **overrides) -> KubernetesRunnerOptions:
    kwargs = dict(
        namespace="test-ns",
        configmap_name="test-configmap",
        pvc_name="test-pvc",
        node_hostname="test-node",
        image_pull_secret="test-pull-secret",
        runner_label="test-runner-label",
        poll_interval_seconds=0.0,
        timeout_seconds=5.0,
    )
    kwargs.update(overrides)
    return KubernetesRunnerOptions(**kwargs)


class EnvAllowlistTest(unittest.TestCase):
    """Same security invariant as ContainerRunnerBackend: no provider or
    external-tool credential ever reaches the Job spec, even when present
    in the scheduler process's own environment."""

    def test_job_manifest_env_excludes_credentials_and_includes_only_allowlisted_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp)))
            with mock.patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "sk-ant-secret",
                    "X_API_KEY": "x-secret",
                    "JQUANTS_API_KEY": "jq-secret",
                    "GITHUB_TOKEN": "gh-secret",
                    "CLAUDE_PROXY_SESSION_TOKEN": "cp_sess_allowed",
                },
                clear=False,
            ):
                manifest = backend._job_manifest(_task())

        env_list = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        env_keys = {item["name"] for item in env_list}
        self.assertEqual(
            env_keys,
            {
                "SESSION_ID",
                "ROLE",
                "WORKSPACE_DIR",
                "PYTHONPATH",
                "CLAUDE_PROXY_URL",
                "AUTH_PROXY_URL",
                # Issue #29 hardening pass: the container runs as a non-root
                # UID with no /etc/passwd entry (securityContext.runAsUser
                # below), so $HOME must be pinned explicitly or anything
                # calling expanduser("~")/getpwuid (git, some stdlib paths)
                # fails instead of just working.
                "HOME",
                "CLAUDE_PROXY_SESSION_TOKEN",
            },
        )
        serialized = json.dumps(manifest)
        for secret in ("sk-ant-secret", "x-secret", "jq-secret", "gh-secret"):
            self.assertNotIn(secret, serialized)
        self.assertIn("cp_sess_allowed", serialized)

    def test_optional_session_tokens_omitted_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp)))
            with mock.patch.dict("os.environ", {}, clear=False):
                for key in ("CLAUDE_PROXY_SESSION_TOKEN", "AUTH_PROXY_SESSION_TOKEN"):
                    os.environ.pop(key, None)
                manifest = backend._job_manifest(_task())
        env_keys = {item["name"] for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertNotIn("CLAUDE_PROXY_SESSION_TOKEN", env_keys)
        self.assertNotIn("AUTH_PROXY_SESSION_TOKEN", env_keys)


class JobManifestShapeTest(unittest.TestCase):
    def _manifest(self, tmp: str):
        backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp)))
        return backend, backend._job_manifest(_task())

    def test_backoff_limit_zero_and_restart_policy_never(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, manifest = self._manifest(tmp)
        self.assertEqual(manifest["spec"]["backoffLimit"], 0)
        self.assertEqual(manifest["spec"]["template"]["spec"]["restartPolicy"], "Never")

    def test_ttl_seconds_after_finished_set_from_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp), ttl_seconds_after_finished=42))
            manifest = backend._job_manifest(_task())
        self.assertEqual(manifest["spec"]["ttlSecondsAfterFinished"], 42)

    def test_runner_label_present_on_job_and_pod_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, manifest = self._manifest(tmp)
        self.assertEqual(manifest["metadata"]["labels"]["app.kubernetes.io/name"], "test-runner-label")
        self.assertEqual(
            manifest["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"],
            "test-runner-label",
        )

    def test_node_selector_pins_to_configured_hostname(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, manifest = self._manifest(tmp)
        self.assertEqual(
            manifest["spec"]["template"]["spec"]["nodeSelector"],
            {"kubernetes.io/hostname": "test-node"},
        )

    def test_pvc_and_configmap_mounts_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, manifest = self._manifest(tmp)
        pod_spec = manifest["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod_spec["volumes"]}
        self.assertEqual(volumes["config"]["configMap"]["name"], "test-configmap")
        self.assertEqual(volumes["data"]["persistentVolumeClaim"]["claimName"], "test-pvc")
        self.assertEqual(volumes["sessions"]["persistentVolumeClaim"]["claimName"], "test-pvc")

        mounts = {m["name"]: m for m in pod_spec["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["config"]["mountPath"], "/app/config")
        self.assertEqual(mounts["data"]["mountPath"], "/app/.data")
        self.assertEqual(mounts["sessions"]["mountPath"], "/app/.sessions")

    def test_image_pull_secret_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, manifest = self._manifest(tmp)
        self.assertEqual(
            manifest["spec"]["template"]["spec"]["imagePullSecrets"],
            [{"name": "test-pull-secret"}],
        )

    def test_no_argocd_instance_tracking_label_anywhere_in_manifest(self) -> None:
        """Runner Jobs are ephemeral, scheduler-created, per-task resources
        and must stay outside ArgoCD's tracked-resource set: an
        app.kubernetes.io/instance label anywhere on the Job would pull it
        into ArgoCD's sync/prune bookkeeping."""
        with tempfile.TemporaryDirectory() as tmp:
            _, manifest = self._manifest(tmp)
        self.assertNotIn("app.kubernetes.io/instance", json.dumps(manifest))

    def test_dry_run_flag_forwarded_to_runner_execute_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp)))
            manifest = backend._job_manifest(_task(dry_run=True))
        args = manifest["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--dry-run", args)

    def test_dry_run_false_omits_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp)))
            manifest = backend._job_manifest(_task(dry_run=False))
        args = manifest["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertNotIn("--dry-run", args)

    def test_job_name_translates_underscores_and_is_lowercase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp)))
            manifest = backend._job_manifest(_task(session_id="sess_ABC_123"))
        name = manifest["metadata"]["name"]
        self.assertEqual(name, name.lower())
        self.assertNotIn("_", name)
        self.assertLessEqual(len(name), 63)
        self.assertFalse(name.endswith("-"))

    def test_job_namespace_matches_resolved_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, manifest = self._manifest(tmp)
        self.assertEqual(manifest["metadata"]["namespace"], "test-ns")


class RunnerJobHardeningTest(unittest.TestCase):
    """Issue #29 reviewer CONCERNS fixes (P2-2, P2-4): the runner Job's
    pod/container spec must not automount a k8s API token it never uses,
    and must run under a Pod Security "restricted"-equivalent
    securityContext."""

    def _manifest(self, tmp: str, **overrides):
        backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp), **overrides))
        return backend._job_manifest(_task())

    def test_automount_service_account_token_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(tmp)
        self.assertIs(manifest["spec"]["template"]["spec"]["automountServiceAccountToken"], False)

    def test_pod_security_context_is_restricted_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(tmp)
        pod_security = manifest["spec"]["template"]["spec"]["securityContext"]
        self.assertIs(pod_security["runAsNonRoot"], True)
        self.assertEqual(pod_security["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertIsInstance(pod_security["runAsUser"], int)
        self.assertIsInstance(pod_security["runAsGroup"], int)
        # fsGroup must match runAsGroup so the runner can write into
        # PVC subPath directories the scheduler (running as the same
        # UID/GID, see deploy/k8s/scheduler.yaml) already created.
        self.assertEqual(pod_security["fsGroup"], pod_security["runAsGroup"])

    def test_container_security_context_drops_all_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(tmp)
        container_security = manifest["spec"]["template"]["spec"]["containers"][0]["securityContext"]
        self.assertIs(container_security["allowPrivilegeEscalation"], False)
        self.assertEqual(container_security["capabilities"], {"drop": ["ALL"]})

    def test_run_as_user_and_group_configurable_and_default_to_10001(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(tmp)
        pod_security = manifest["spec"]["template"]["spec"]["securityContext"]
        self.assertEqual(pod_security["runAsUser"], 10001)
        self.assertEqual(pod_security["runAsGroup"], 10001)

        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(tmp, run_as_user=20002, run_as_group=20002)
        pod_security = manifest["spec"]["template"]["spec"]["securityContext"]
        self.assertEqual(pod_security["runAsUser"], 20002)
        self.assertEqual(pod_security["runAsGroup"], 20002)

    def test_run_as_user_and_group_default_from_runner_uid_gid_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"RUNNER_UID": "30003", "RUNNER_GID": "40004"}, clear=False):
                options = KubernetesRunnerOptions(namespace="test-ns", configmap_name="cm")
        self.assertEqual(options.run_as_user, 30003)
        self.assertEqual(options.run_as_group, 40004)


class ConfigMapResolutionTest(unittest.TestCase):
    def test_explicit_configmap_name_short_circuits_api_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(
                root=Path(tmp), options=_options(Path(tmp), configmap_name="explicit-cm")
            )
            with mock.patch.object(backend, "_api_request") as api_request:
                name = backend._resolve_configmap_name()
        self.assertEqual(name, "explicit-cm")
        api_request.assert_not_called()

    def test_self_pod_lookup_resolves_configmap_name_from_own_pod_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(
                root=Path(tmp),
                options=_options(
                    Path(tmp),
                    configmap_name=None,
                    self_pod_name="scheduler-abc123",
                    self_pod_namespace="test-ns",
                ),
            )
            pod = {
                "spec": {
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": "test-pvc"}},
                        {"name": "config", "configMap": {"name": "7mimi-agent-config-h5k9m2"}},
                    ]
                }
            }
            with mock.patch.object(backend, "_api_request", return_value=pod) as api_request:
                name = backend._resolve_configmap_name()
                name_again = backend._resolve_configmap_name()

        self.assertEqual(name, "7mimi-agent-config-h5k9m2")
        self.assertEqual(name_again, "7mimi-agent-config-h5k9m2")
        # Cached after first resolution -- only one API round trip.
        api_request.assert_called_once_with(
            "GET", "/api/v1/namespaces/test-ns/pods/scheduler-abc123"
        )

    def test_self_pod_lookup_raises_when_no_config_volume_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(
                root=Path(tmp),
                options=_options(
                    Path(tmp),
                    configmap_name=None,
                    self_pod_name="scheduler-abc123",
                    self_pod_namespace="test-ns",
                ),
            )
            pod = {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "x"}}]}}
            with mock.patch.object(backend, "_api_request", return_value=pod):
                with self.assertRaises(RuntimeError):
                    backend._resolve_configmap_name()

    def test_raises_when_neither_explicit_name_nor_downward_api_env_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(
                root=Path(tmp),
                options=_options(
                    Path(tmp), configmap_name=None, self_pod_name=None, self_pod_namespace=None
                ),
            )
            with mock.patch.object(backend, "_api_request") as api_request:
                with self.assertRaises(RuntimeError):
                    backend._resolve_configmap_name()
            api_request.assert_not_called()


class WaitForCompletionTest(unittest.TestCase):
    def test_succeeded_status_returns_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp)))
            responses = [
                {"status": {}},
                {"status": {"succeeded": 1}},
            ]
            with mock.patch.object(backend, "_api_request", side_effect=responses):
                with mock.patch("time.sleep") as sleep_mock:
                    backend._wait_for_completion(namespace="test-ns", job_name="job-1")
            sleep_mock.assert_called()

    def test_failed_status_raises_runtime_error_with_condition_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(root=Path(tmp), options=_options(Path(tmp)))
            response = {
                "status": {
                    "failed": 1,
                    "conditions": [{"type": "Failed", "reason": "BackoffLimitExceeded", "message": "boom"}],
                }
            }
            with mock.patch.object(backend, "_api_request", return_value=response):
                with self.assertRaises(RuntimeError) as ctx:
                    backend._wait_for_completion(namespace="test-ns", job_name="job-1")
        self.assertIn("boom", str(ctx.exception))

    def test_timeout_raises_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = KubernetesRunnerBackend(
                root=Path(tmp), options=_options(Path(tmp), timeout_seconds=1.0, poll_interval_seconds=0.0)
            )
            with mock.patch.object(backend, "_api_request", return_value={"status": {}}):
                with mock.patch("time.sleep"):
                    # monotonic(): first call sets the deadline, every call
                    # after must appear to be past it.
                    with mock.patch("time.monotonic", side_effect=[0.0, 10.0, 10.0]):
                        with self.assertRaises(RuntimeError) as ctx:
                            backend._wait_for_completion(namespace="test-ns", job_name="job-1")
        self.assertIn("did not complete within", str(ctx.exception))


class CollectResultTest(unittest.TestCase):
    """Result comes from the shared-PVC result.json file, never Pod logs."""

    def test_reads_result_json_written_by_runner_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _task(session_id="sess_collect_ok")
            session_dir = root / ".sessions" / task.session_id
            session_dir.mkdir(parents=True)
            payload = {"status": "succeeded", "path": "daily/2026/07/2026-07-16.md"}
            (session_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")

            backend = KubernetesRunnerBackend(root=root, options=_options(root))
            result = backend._collect_result(task)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload, payload)

    def test_missing_result_file_raises_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _task(session_id="sess_collect_missing")
            backend = KubernetesRunnerBackend(root=root, options=_options(root))
            with self.assertRaises(RuntimeError) as ctx:
                backend._collect_result(task)
        self.assertIn(task.session_id, str(ctx.exception))


class RunTaskIntegrationTest(unittest.TestCase):
    """End-to-end run_task() with the k8s API layer mocked at _api_request."""

    def test_run_task_creates_job_polls_and_collects_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _task(session_id="sess_run_task_ok")
            session_dir = root / ".sessions" / task.session_id
            session_dir.mkdir(parents=True)
            payload = {"status": "succeeded", "path": "daily/x.md"}
            (session_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")

            backend = KubernetesRunnerBackend(root=root, options=_options(root))
            api_responses = [
                {},  # POST create job
                {"status": {"succeeded": 1}},  # GET status poll
            ]
            with mock.patch.object(backend, "_api_request", side_effect=api_responses) as api_request:
                result = backend.run_task(task)

        self.assertIsInstance(result, RunnerExecutionResult)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload, payload)

        create_call = api_request.call_args_list[0]
        self.assertEqual(create_call.args[0], "POST")
        self.assertEqual(create_call.args[1], "/apis/batch/v1/namespaces/test-ns/jobs")
        submitted_manifest = create_call.kwargs["body"]
        self.assertEqual(submitted_manifest["kind"], "Job")

    def test_run_task_propagates_failure_without_reading_result_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _task(session_id="sess_run_task_fail")
            backend = KubernetesRunnerBackend(root=root, options=_options(root))
            api_responses = [
                {},  # POST create job
                {"status": {"failed": 1, "conditions": []}},  # GET status poll
            ]
            with mock.patch.object(backend, "_api_request", side_effect=api_responses):
                with self.assertRaises(RuntimeError):
                    backend.run_task(task)
        # No result.json was ever written for this session -- confirms the
        # failure path doesn't fall through to (silently wrong) result
        # collection.
        self.assertFalse((root / ".sessions" / task.session_id / "result.json").exists())

    def test_run_task_failure_message_includes_result_json_error_detail_when_present(self) -> None:
        """Issue #29 reviewer CONCERNS fix (P3-5): if runner-execute got far
        enough to write .sessions/<id>/result.json before the Job was
        marked failed (e.g. it ran and raised inside cmd_runner_execute),
        fold that error type/message into the RuntimeError instead of just
        the generic Job-level failure reason."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _task(session_id="sess_run_task_fail_with_detail")
            session_dir = root / ".sessions" / task.session_id
            session_dir.mkdir(parents=True)
            (session_dir / "result.json").write_text(
                json.dumps({"status": "failed", "error": {"type": "McpClientError", "message": "x-mcp timed out"}}),
                encoding="utf-8",
            )

            backend = KubernetesRunnerBackend(root=root, options=_options(root))
            api_responses = [
                {},  # POST create job
                {
                    "status": {
                        "failed": 1,
                        "conditions": [{"type": "Failed", "reason": "BackoffLimitExceeded", "message": "boom"}],
                    }
                },
            ]
            with mock.patch.object(backend, "_api_request", side_effect=api_responses):
                with self.assertRaises(RuntimeError) as ctx:
                    backend.run_task(task)

        message = str(ctx.exception)
        self.assertIn("boom", message)
        self.assertIn("McpClientError", message)
        self.assertIn("x-mcp timed out", message)

    def test_run_task_failure_message_unchanged_when_result_json_has_no_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _task(session_id="sess_run_task_fail_no_error_field")
            session_dir = root / ".sessions" / task.session_id
            session_dir.mkdir(parents=True)
            # e.g. a stale result.json from a previous, unrelated success.
            (session_dir / "result.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")

            backend = KubernetesRunnerBackend(root=root, options=_options(root))
            api_responses = [
                {},
                {"status": {"failed": 1, "conditions": [{"type": "Failed", "message": "boom"}]}},
            ]
            with mock.patch.object(backend, "_api_request", side_effect=api_responses):
                with self.assertRaises(RuntimeError) as ctx:
                    backend.run_task(task)
        message = str(ctx.exception)
        self.assertIn("boom", message)
        self.assertNotIn("runner-execute reported", message)


class TokenRotationAtTransportLayerTest(unittest.TestCase):
    """Proves the SA token is re-read from disk on every _api_request call
    (BoundServiceAccountToken rotation), by exercising the real
    _api_request/urllib path with only urlopen (and the ssl context
    construction) stubbed out."""

    def test_api_request_sends_freshly_read_token_bearer_header_each_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            token_path = tmp_path / "token"
            token_path.write_text("token-v1", encoding="utf-8")

            backend = KubernetesRunnerBackend(
                root=tmp_path,
                options=_options(tmp_path, token_path=token_path),
            )

            captured_requests = []

            def fake_urlopen(request, context=None, timeout=None):
                captured_requests.append(request)
                response = mock.MagicMock()
                response.read.return_value = b"{}"
                response.__enter__.return_value = response
                response.__exit__.return_value = False
                return response

            with mock.patch.object(backend, "_ssl_context", return_value=None), mock.patch(
                "urllib.request.urlopen", side_effect=fake_urlopen
            ):
                backend._api_request("GET", "/api/v1/namespaces/test-ns/pods/x")
                token_path.write_text("token-v2-rotated", encoding="utf-8")
                backend._api_request("GET", "/api/v1/namespaces/test-ns/pods/x")

        self.assertEqual(len(captured_requests), 2)
        self.assertEqual(captured_requests[0].get_header("Authorization"), "Bearer token-v1")
        self.assertEqual(captured_requests[1].get_header("Authorization"), "Bearer token-v2-rotated")

    def test_http_error_response_raises_runtime_error_with_status_and_body(self) -> None:
        import io
        import urllib.error

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            token_path = tmp_path / "token"
            token_path.write_text("token-v1", encoding="utf-8")
            backend = KubernetesRunnerBackend(
                root=tmp_path, options=_options(tmp_path, token_path=token_path)
            )

            def raise_http_error(request, context=None, timeout=None):
                raise urllib.error.HTTPError(
                    url="https://kubernetes.default.svc/api",
                    code=403,
                    msg="Forbidden",
                    hdrs=None,
                    fp=io.BytesIO(b"forbidden detail"),
                )

            with mock.patch.object(backend, "_ssl_context", return_value=None), mock.patch(
                "urllib.request.urlopen", side_effect=raise_http_error
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    backend._api_request("GET", "/api/v1/namespaces/test-ns/pods/x")
        self.assertIn("403", str(ctx.exception))
        self.assertIn("forbidden detail", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
