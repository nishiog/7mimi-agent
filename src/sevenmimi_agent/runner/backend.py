from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sevenmimi_agent.config.loader import AppConfig
from sevenmimi_agent.db.repository import Repository
from sevenmimi_agent.security.policy_engine import PolicyEngine


@dataclass(frozen=True)
class RunnerTask:
    job_name: str
    job: dict[str, Any]
    session_id: str
    task_id: str
    role: str
    dry_run: bool


@dataclass(frozen=True)
class RunnerExecutionResult:
    status: str
    payload: dict[str, Any]


class RunnerBackend(Protocol):
    def run_task(self, task: RunnerTask) -> RunnerExecutionResult: ...


def execute_runner_task(*, config: AppConfig, repository: Repository, task: RunnerTask) -> RunnerExecutionResult:
    if task.role != "ai_it_topic_runner":
        raise NotImplementedError(f"runner currently supports ai_it_topic_runner only, got {task.role}")

    from sevenmimi_agent.roles.ai_it_topic_runner import AiItTopicRunner

    runner = AiItTopicRunner(config=config, repository=repository, policy_engine=PolicyEngine(config.policy))
    result = runner.run_daily_digest(session_id=task.session_id, task_id=task.task_id, job=task.job, dry_run=task.dry_run)
    payload = {"status": result.status, "path": result.path, "title": result.title, "source_refs": result.source_refs}
    return RunnerExecutionResult(status=result.status, payload=payload)
