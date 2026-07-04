from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .backend import RunnerBackend, RunnerExecutionResult, RunnerTask


@dataclass(frozen=True)
class ContainerRunnerOptions:
    image: str = "7mimi-agent-runner:latest"
    docker_bin: str = "docker"
    network: str = "none"
    memory: str = "2g"
    pids_limit: int = 256


class ContainerRunnerBackend(RunnerBackend):
    def __init__(self, *, root: Path, options: ContainerRunnerOptions | None = None) -> None:
        self.root = root.resolve()
        self.options = options or ContainerRunnerOptions()

    def run_task(self, task: RunnerTask) -> RunnerExecutionResult:
        env_args: list[str] = []
        allowed_env = {
            "SESSION_ID": task.session_id,
            "ROLE": task.role,
            "WORKSPACE_DIR": f"/workspace/.sessions/{task.session_id}/workspace",
            "PYTHONPATH": "/workspace/src",
            # Proxy endpoints are safe to forward. Tokens are forwarded only if explicitly present.
            "CLAUDE_PROXY_URL": os.environ.get("CLAUDE_PROXY_URL", "http://host.docker.internal:18080"),
            "AUTH_PROXY_URL": os.environ.get("AUTH_PROXY_URL", "http://host.docker.internal:18081"),
        }
        for optional in ["CLAUDE_PROXY_SESSION_TOKEN", "AUTH_PROXY_SESSION_TOKEN"]:
            if os.environ.get(optional):
                allowed_env[optional] = os.environ[optional]
        for key, value in allowed_env.items():
            env_args.extend(["-e", f"{key}={value}"])

        cmd = [
            self.options.docker_bin,
            "run",
            "--rm",
            "--name",
            f"7mimi-agent-{task.session_id}",
            "--network",
            self.options.network,
            "--memory",
            self.options.memory,
            "--pids-limit",
            str(self.options.pids_limit),
            "-v",
            f"{self.root}:/workspace",
            "-w",
            "/workspace",
            *env_args,
            self.options.image,
            "python",
            "-m",
            "sevenmimi_agent",
            "runner-execute",
            task.job_name,
            "--session-id",
            task.session_id,
            "--task-id",
            task.task_id,
            "--runner-root",
            "/workspace",
        ]
        if task.dry_run:
            cmd.append("--dry-run")

        completed = subprocess.run(cmd, cwd=self.root, text=True, capture_output=True)
        if completed.returncode != 0:
            if completed.stdout:
                print(completed.stdout, file=sys.stdout)
            if completed.stderr:
                print(completed.stderr, file=sys.stderr)
            raise RuntimeError(f"container runner failed with exit code {completed.returncode}")

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"container runner returned non-JSON output: {completed.stdout[:500]}") from exc
        return RunnerExecutionResult(status=payload.get("status", "unknown"), payload=payload)
