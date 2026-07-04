from __future__ import annotations

from sevenmimi_agent.config.loader import AppConfig
from sevenmimi_agent.db.repository import Repository

from .backend import RunnerBackend, RunnerExecutionResult, RunnerTask, execute_runner_task


class LocalRunnerBackend(RunnerBackend):
    def __init__(self, *, config: AppConfig, repository: Repository) -> None:
        self.config = config
        self.repository = repository

    def run_task(self, task: RunnerTask) -> RunnerExecutionResult:
        return execute_runner_task(config=self.config, repository=self.repository, task=task)
