from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backend import RunnerBackend, RunnerExecutionResult, RunnerTask

# In-cluster ServiceAccount projection (k3s / any k8s). BoundServiceAccountToken
# (default since k8s 1.21+) rotates the token file periodically, so callers
# must re-read it per request rather than caching it.
_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_DEFAULT_API_SERVER = "https://kubernetes.default.svc"


@dataclass(frozen=True)
class KubernetesRunnerOptions:
    """Wiring for the in-cluster runner Job (Issue #29 / k3s + ArgoCD).

    Most defaults are read from environment variables set on the scheduler
    Deployment (deploy/k8s/scheduler.yaml) so the manifests stay the single
    source of truth for cluster-specific values (image tag, ConfigMap name,
    node pin).
    """

    image: str = field(default_factory=lambda: os.environ.get("RUNNER_IMAGE", "ghcr.io/7milch/7mimi-agent-agent-runner:latest"))
    # None => resolved from the ServiceAccount `namespace` file at call time.
    namespace: str | None = None
    # Explicit override. Normally left unset: kustomize cannot inject the
    # generator's hash-suffixed name into this at build time (verified --
    # see the long comment in deploy/k8s/kustomization.yaml), so by default
    # KubernetesRunnerBackend instead resolves the live ConfigMap name by
    # reading its own Pod's volume spec back from the k8s API (self_pod_name
    # / self_pod_namespace below, populated from POD_NAME / POD_NAMESPACE
    # Downward API env vars on the scheduler Deployment).
    configmap_name: str | None = field(default_factory=lambda: os.environ.get("RUNNER_CONFIGMAP_NAME") or None)
    configmap_volume_name: str = "config"
    self_pod_name: str | None = field(default_factory=lambda: os.environ.get("POD_NAME") or None)
    self_pod_namespace: str | None = field(default_factory=lambda: os.environ.get("POD_NAMESPACE") or None)
    pvc_name: str = field(default_factory=lambda: os.environ.get("RUNNER_PVC_NAME", "7mimi-agent-data"))
    # local-path StorageClass has no cross-node access; pin runner Jobs to the
    # same single node as the PV (deploy/k8s/scheduler.yaml pins the same way).
    node_hostname: str = field(default_factory=lambda: os.environ.get("RUNNER_NODE_HOSTNAME", "john-cooper-works"))
    image_pull_secret: str = field(default_factory=lambda: os.environ.get("RUNNER_IMAGE_PULL_SECRET", "ghcr-pull-secret"))
    # Restricted Pod Security profile (reviewer hardening pass, Issue #29).
    # Neither Dockerfile sets a non-root USER (doing so would break
    # docker-compose/local-dev, which bind-mounts the host repo and needs
    # write access as whatever UID owns those host files) -- instead this
    # UID/GID is forced purely via Pod securityContext, which k8s allows
    # regardless of the image's own default user. It MUST match
    # deploy/k8s/scheduler.yaml's `runAsUser`/`runAsGroup`/`fsGroup`: the
    # scheduler (which pre-creates `.sessions/<id>/workspace/**`) and the
    # runner Job (which writes into it) share one PVC, so both need to run
    # as the same UID/GID for the runner to actually have write access to
    # what the scheduler created.
    run_as_user: int = field(default_factory=lambda: int(os.environ.get("RUNNER_UID", "10001")))
    run_as_group: int = field(default_factory=lambda: int(os.environ.get("RUNNER_GID", "10001")))
    memory_limit: str = "2Gi"
    api_server: str = _DEFAULT_API_SERVER
    ca_cert_path: Path = _SA_DIR / "ca.crt"
    token_path: Path = _SA_DIR / "token"
    namespace_path: Path = _SA_DIR / "namespace"
    poll_interval_seconds: float = 5.0
    timeout_seconds: float = 900.0
    ttl_seconds_after_finished: int = 600
    runner_label: str = "7mimi-agent-runner"
    job_name_prefix: str = "7mimi-agent-runner"
    request_timeout_seconds: float = 30.0


class KubernetesRunnerBackend(RunnerBackend):
    """Runs a task as a batch/v1 Job on the in-cluster k3s API (Issue #29).

    Talks to the API server with stdlib `urllib` only (no `kubernetes`
    package, per repo convention). Job completion is observed by polling
    `.status` (no watch), and the result payload is read back from the
    shared PVC (`.sessions/<session_id>/result.json`, written by
    `runner-execute` -- see cli.py `cmd_runner_execute`) rather than from Pod
    logs, since logs are not a stable machine-readable channel.

    `root` must be the same path the scheduler and runner Job both mount the
    shared PVC at (`/app` in-cluster, matching the `find_project_root()`
    single-root invariant), so that reading `.sessions/<session_id>/result.json`
    here sees the file the Job pod wrote to the same PVC.
    """

    def __init__(self, *, root: Path, options: KubernetesRunnerOptions | None = None) -> None:
        self.root = root.resolve()
        self.options = options or KubernetesRunnerOptions()
        self._namespace_cache: str | None = None
        self._configmap_name_cache: str | None = None

    # -- k8s API plumbing, kept as thin wrappers so tests can mock just this layer --

    def _read_token(self) -> str:
        return self.options.token_path.read_text(encoding="utf-8").strip()

    def _namespace(self) -> str:
        if self.options.namespace:
            return self.options.namespace
        if self._namespace_cache is None:
            self._namespace_cache = self.options.namespace_path.read_text(encoding="utf-8").strip()
        return self._namespace_cache

    def _ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.options.ca_cert_path))

    def _api_request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.options.api_server}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._read_token()}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, context=self._ssl_context(), timeout=self.options.request_timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"kubernetes API {method} {path} failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"kubernetes API {method} {path} unreachable: {exc.reason}") from exc
        if not raw:
            return {}
        return json.loads(raw)

    # -- ConfigMap name resolution --

    def _resolve_configmap_name(self) -> str:
        if self.options.configmap_name:
            return self.options.configmap_name
        if self._configmap_name_cache is not None:
            return self._configmap_name_cache
        if not self.options.self_pod_name or not self.options.self_pod_namespace:
            raise RuntimeError(
                "cannot resolve the runner ConfigMap name: neither RUNNER_CONFIGMAP_NAME nor "
                "POD_NAME/POD_NAMESPACE (Downward API) are set on this process"
            )
        pod = self._api_request(
            "GET",
            f"/api/v1/namespaces/{self.options.self_pod_namespace}/pods/{self.options.self_pod_name}",
        )
        volumes = ((pod.get("spec") or {}).get("volumes")) or []
        for volume in volumes:
            if volume.get("name") == self.options.configmap_volume_name:
                config_map = volume.get("configMap") or {}
                name = config_map.get("name")
                if name:
                    self._configmap_name_cache = name
                    return name
        raise RuntimeError(
            f"pod {self.options.self_pod_namespace}/{self.options.self_pod_name} has no "
            f"'{self.options.configmap_volume_name}' configMap volume; cannot mount config into the runner Job"
        )

    # -- job construction --

    def _allowed_env(self, task: RunnerTask) -> list[dict[str, str]]:
        # Same allowlist as ContainerRunnerBackend (container_runner.py):
        # proxy endpoints are safe to forward, tokens only if explicitly set.
        allowed_env = {
            "SESSION_ID": task.session_id,
            "ROLE": task.role,
            "WORKSPACE_DIR": f"/app/.sessions/{task.session_id}/workspace",
            "PYTHONPATH": "/app/src",
            "CLAUDE_PROXY_URL": os.environ.get("CLAUDE_PROXY_URL", "http://claude-proxy:18080"),
            "AUTH_PROXY_URL": os.environ.get("AUTH_PROXY_URL", "http://auth-proxy:18081"),
            # The container runs as a non-root UID with no /etc/passwd entry
            # (securityContext.runAsUser below, no matching image USER); an
            # unset $HOME makes anything that calls os.path.expanduser("~")
            # or getpwuid (git, some Python stdlib paths) raise/fail instead
            # of just working, so pin it to a dir that's always writable.
            "HOME": "/tmp",
        }
        for optional in ["CLAUDE_PROXY_SESSION_TOKEN", "AUTH_PROXY_SESSION_TOKEN"]:
            if os.environ.get(optional):
                allowed_env[optional] = os.environ[optional]
        return [{"name": key, "value": value} for key, value in allowed_env.items()]

    def _job_name(self, task: RunnerTask) -> str:
        # Job/Pod names must be lowercase RFC1123 labels; session ids are
        # already lowercase alnum + underscores (util/ids.py), so only the
        # underscore needs translating.
        suffix = task.session_id.replace("_", "-").lower()
        return f"{self.options.job_name_prefix}-{suffix}"[:63].rstrip("-")

    def _job_manifest(self, task: RunnerTask) -> dict[str, Any]:
        configmap_name = self._resolve_configmap_name()
        args = [
            "-m",
            "shichimimi_agent",
            "runner-execute",
            task.job_name,
            "--session-id",
            task.session_id,
            "--task-id",
            task.task_id,
            "--runner-root",
            "/app",
        ]
        if task.dry_run:
            args.append("--dry-run")

        job_labels = {
            "app.kubernetes.io/name": self.options.runner_label,
            "app.kubernetes.io/part-of": "7mimi-agent",
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": self._job_name(task),
                "namespace": self._namespace(),
                # No app.kubernetes.io/instance label: runner Jobs are
                # scheduler-owned, ephemeral, per-task resources and must
                # stay outside ArgoCD's tracked-resource set (spec item 2).
                "labels": {**job_labels, "shichimimi.io/session-id": task.session_id},
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": self.options.ttl_seconds_after_finished,
                "template": {
                    "metadata": {"labels": job_labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "nodeSelector": {"kubernetes.io/hostname": self.options.node_hostname},
                        "imagePullSecrets": [{"name": self.options.image_pull_secret}],
                        # The runner never calls the k8s API (only the
                        # scheduler does, via its own 7mimi-scheduler SA),
                        # so it has no legitimate use for a token -- defense
                        # in depth against a compromised runner reaching the
                        # API server at all.
                        "automountServiceAccountToken": False,
                        # Pod Security "restricted" equivalent (Issue #29
                        # hardening pass). run_as_user/run_as_group must
                        # match deploy/k8s/scheduler.yaml's securityContext
                        # -- see the long comment on KubernetesRunnerOptions.
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": self.options.run_as_user,
                            "runAsGroup": self.options.run_as_group,
                            "fsGroup": self.options.run_as_group,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "runner",
                                "image": self.options.image,
                                "command": ["python"],
                                "args": args,
                                "env": self._allowed_env(task),
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "limits": {"memory": self.options.memory_limit},
                                    "requests": {"memory": self.options.memory_limit},
                                },
                                "volumeMounts": [
                                    {"name": "config", "mountPath": "/app/config"},
                                    {"name": "data", "mountPath": "/app/.data", "subPath": "data"},
                                    {"name": "sessions", "mountPath": "/app/.sessions", "subPath": "sessions"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": configmap_name}},
                            {"name": "data", "persistentVolumeClaim": {"claimName": self.options.pvc_name}},
                            {"name": "sessions", "persistentVolumeClaim": {"claimName": self.options.pvc_name}},
                        ],
                    },
                },
            },
        }

    # -- execution --

    def run_task(self, task: RunnerTask) -> RunnerExecutionResult:
        manifest = self._job_manifest(task)
        namespace = manifest["metadata"]["namespace"]
        job_name = manifest["metadata"]["name"]
        self._api_request("POST", f"/apis/batch/v1/namespaces/{namespace}/jobs", body=manifest)
        try:
            self._wait_for_completion(namespace=namespace, job_name=job_name)
        except RuntimeError as exc:
            raise RuntimeError(self._augment_with_result_error(task, str(exc))) from exc
        return self._collect_result(task)

    def _augment_with_result_error(self, task: RunnerTask, job_level_message: str) -> str:
        # The Job/Pod-level failure reason (OOMKilled, ImagePullBackOff,
        # timeout, ...) doesn't know *why* runner-execute itself failed.
        # cmd_runner_execute (cli.py) writes .sessions/<id>/result.json on
        # both success and failure, so if it got far enough to run and
        # raise, fold that detail into the message here.
        result_path = self.root / ".sessions" / task.session_id / "result.json"
        if not result_path.exists():
            return job_level_message
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return job_level_message
        error = payload.get("error") if isinstance(payload, dict) else None
        if not error:
            return job_level_message
        error_type = error.get("type", "Error") if isinstance(error, dict) else "Error"
        error_message = error.get("message", "") if isinstance(error, dict) else str(error)
        return f"{job_level_message}; runner-execute reported {error_type}: {error_message}"

    def _wait_for_completion(self, *, namespace: str, job_name: str) -> None:
        deadline = time.monotonic() + self.options.timeout_seconds
        while True:
            job = self._api_request("GET", f"/apis/batch/v1/namespaces/{namespace}/jobs/{job_name}")
            status = job.get("status") or {}
            if int(status.get("succeeded") or 0) >= 1:
                return
            if int(status.get("failed") or 0) >= 1:
                conditions = status.get("conditions") or []
                reason = next(
                    (c.get("message") or c.get("reason") for c in conditions if c.get("type") == "Failed"),
                    "runner Job reported failed status",
                )
                raise RuntimeError(f"runner Job {namespace}/{job_name} failed: {reason}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"runner Job {namespace}/{job_name} did not complete within {self.options.timeout_seconds}s")
            time.sleep(self.options.poll_interval_seconds)

    def _collect_result(self, task: RunnerTask) -> RunnerExecutionResult:
        result_path = self.root / ".sessions" / task.session_id / "result.json"
        if not result_path.exists():
            raise RuntimeError(
                f"runner Job for session {task.session_id} completed but {result_path} was not written"
            )
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        return RunnerExecutionResult(status=payload.get("status", "unknown"), payload=payload)
