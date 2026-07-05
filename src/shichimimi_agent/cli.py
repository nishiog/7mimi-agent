from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from shichimimi_agent.config import load_config, validate_config
from shichimimi_agent.db import Repository, default_db_path, migrate
from shichimimi_agent.runner import ContainerRunnerBackend, ContainerRunnerOptions, LocalRunnerBackend, RunnerTask, execute_runner_task
from shichimimi_agent.sessions.workspace import create_workspace


def _print_validation(result: Any) -> int:
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)
    if result.ok:
        print("config ok")
        return 0
    return 1


def _load_validated_config(root: str | None = None) -> Any:
    config = load_config(Path(root) if root else None)
    validation = validate_config(config)
    if not validation.ok:
        raise ValueError("config validation failed: " + "; ".join(validation.errors))
    return config


def cmd_config_validate(args: argparse.Namespace) -> int:
    config = load_config(Path(args.root) if args.root else None)
    return _print_validation(validate_config(config))


def cmd_db_init(args: argparse.Namespace) -> int:
    config = load_config(Path(args.root) if args.root else None)
    db_path = default_db_path(config.root)
    migrate(db_path)
    print(f"initialized database: {db_path}")
    return 0


def cmd_schedule_list(args: argparse.Namespace) -> int:
    config = load_config(Path(args.root) if args.root else None)
    for job in config.schedules.get("jobs") or []:
        print(f"{job.get('name')}\trole={job.get('role')}\tcron={job.get('cron')}\tenabled={job.get('enabled', True)}")
    return 0


def _find_job(config: Any, name: str) -> dict[str, Any]:
    for job in config.schedules.get("jobs") or []:
        if job.get("name") == name:
            return job
    raise KeyError(f"unknown job: {name}")


def _prepare_task(*, config: Any, repository: Repository, job_name: str, dry_run: bool, source: str) -> RunnerTask:
    job = _find_job(config, job_name)
    role = job["role"]
    session_id = repository.create_session(source=source, role=role, workspace_path="")
    workspace = create_workspace(config.root, session_id)
    repository.update_session_status(session_id, "running")
    task_id = repository.create_task(session_id=session_id, role=role, input_data={"job": job, "dry_run": dry_run})
    return RunnerTask(job_name=job_name, job=job, session_id=session_id, task_id=task_id, role=role, dry_run=dry_run)


def _finalize_task(repository: Repository, task: RunnerTask, *, status: str, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
    if error is None:
        repository.finish_task(task.task_id, status=status, output=payload)
        repository.update_session_status(task.session_id, "stopped")
    else:
        repository.finish_task(task.task_id, status="failed", error={"type": type(error).__name__, "message": str(error)})
        repository.update_session_status(task.session_id, "failed")


def cmd_run_job(args: argparse.Namespace) -> int:
    if args.publish and args.runner == "container":
        print("error: publish is only supported with the local runner (ADR-018)", file=sys.stderr)
        return 1
    try:
        config = _load_validated_config(args.root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    migrate(default_db_path(config.root))
    repository = Repository.for_root(config.root)
    effective_dry_run = not args.publish or args.dry_run
    task = _prepare_task(config=config, repository=repository, job_name=args.name, dry_run=effective_dry_run, source="cli")

    if args.runner == "local":
        backend = LocalRunnerBackend(config=config, repository=repository)
    else:
        backend = ContainerRunnerBackend(
            root=config.root,
            options=ContainerRunnerOptions(image=args.image, network=args.network, memory=args.memory, pids_limit=args.pids_limit),
        )

    try:
        result = backend.run_task(task)
        _finalize_task(repository, task, status="succeeded", payload=result.payload)
        print(json.dumps(result.payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        _finalize_task(repository, task, status="failed", error=exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_runner_execute(args: argparse.Namespace) -> int:
    """Execute an already-created task inside agent-runner container."""
    try:
        config = _load_validated_config(args.runner_root or args.root)
    except ValueError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    migrate(default_db_path(config.root))
    repository = Repository.for_root(config.root)
    job = _find_job(config, args.name)
    task = RunnerTask(job_name=args.name, job=job, session_id=args.session_id, task_id=args.task_id, role=job["role"], dry_run=args.dry_run)
    try:
        result = execute_runner_task(config=config, repository=repository, task=task)
        print(json.dumps(result.payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1


def cmd_claude_smoke(args: argparse.Namespace) -> int:
    """ADR-013 diagnostic: Claude Code inside agent-runner via claude-proxy."""
    from shichimimi_agent.runner.claude_smoke import (
        DEFAULT_PROMPT,
        ClaudeSmokeOptions,
        run_claude_smoke,
        summarize_result,
    )

    try:
        config = _load_validated_config(args.root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    migrate(default_db_path(config.root))
    repository = Repository.for_root(config.root)
    role = "ai_it_topic_runner"
    session_id = repository.create_session(source="claude-smoke", role=role, workspace_path="")
    workspace = create_workspace(config.root, session_id)
    repository.update_session_status(session_id, "running")

    result = run_claude_smoke(
        root=config.root,
        session_id=session_id,
        role=role,
        workspace=workspace,
        prompt=args.prompt or DEFAULT_PROMPT,
        options=ClaudeSmokeOptions(image=args.image, network=args.network, model=args.model),
    )
    summary = summarize_result(result)
    repository.update_session_status(session_id, "stopped" if result.exit_code == 0 else "failed")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.exit_code == 0 else 1


def cmd_research_stock(args: argparse.Namespace) -> int:
    print("stock research runner is not implemented yet; planned for Phase D5")
    print(json.dumps({"ticker": args.ticker, "dry_run": args.dry_run, "status": "not_implemented"}, ensure_ascii=False, indent=2))
    return 0


def cmd_mcp_x_readonly(args: argparse.Namespace) -> int:
    """ADR-015: run the x-mcp-readonly MCP protocol server."""
    from shichimimi_agent.mcp.x_readonly_server import serve_forever

    serve_forever(host=args.host, port=args.port)
    return 0


def cmd_x_smoke(args: argparse.Namespace) -> int:
    """Connection-test CLI for x-mcp-readonly: authorize then call x.search_posts_recent."""
    import os

    from shichimimi_agent.hooks.post_tool_use import run_post_tool_use
    from shichimimi_agent.hooks.pre_tool_use import PreToolUseInput, run_pre_tool_use
    from shichimimi_agent.mcp.client import McpClientError, McpHttpClient
    from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
    from shichimimi_agent.security.policy_engine import PolicyEngine

    try:
        config = _load_validated_config(args.root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    migrate(default_db_path(config.root))
    repository = Repository.for_root(config.root)

    role = "ai_it_topic_runner"
    session_id = repository.create_session(source="x-smoke", role=role, workspace_path="")
    create_workspace(config.root, session_id)
    repository.update_session_status(session_id, "running")
    task_id = repository.create_task(session_id=session_id, role=role, input_data={"query": args.query, "max_results": args.max_results})

    auth_client = AuthProxyClient(local_fallback_engine=PolicyEngine(config.policy))
    tool_name = "x.search_posts_recent"
    arguments = {"query": args.query, "max_results": args.max_results}
    decision = run_pre_tool_use(
        auth_client,
        PreToolUseInput(session_id=session_id, task_id=task_id, role=role, tool_name=tool_name, arguments=arguments),
    )
    run_post_tool_use(
        repository,
        session_id=session_id,
        task_id=task_id,
        role=role,
        tool_name=tool_name,
        decision=decision.decision,
        success=1 if decision.allowed else 0,
        output_size=0,
    )
    if not decision.allowed:
        repository.finish_task(task_id, status="failed", error={"type": "PermissionError", "message": decision.reason})
        repository.update_session_status(session_id, "failed")
        print(f"error: blocked by policy: {decision.reason}", file=sys.stderr)
        return 1

    mcp_url = args.mcp_url or os.environ.get("X_MCP_URL", "http://127.0.0.1:18082")
    client = McpHttpClient(base_url=mcp_url)
    try:
        client.initialize()
        result = client.call_tool(tool_name, arguments)
    except McpClientError as exc:
        repository.finish_task(task_id, status="failed", error={"type": "McpClientError", "message": str(exc)})
        repository.update_session_status(session_id, "failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    content_items = result.get("content") or []
    text = content_items[0]["text"] if content_items else "{}"
    if result.get("isError"):
        repository.finish_task(task_id, status="failed", error={"type": "McpToolError", "message": text})
        repository.update_session_status(session_id, "failed")
        print(f"error: {text}", file=sys.stderr)
        return 1

    payload = json.loads(text)
    repository.finish_task(task_id, status="succeeded", output=payload)
    repository.update_session_status(session_id, "stopped")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shichimimi-agent")
    parser.add_argument("--root", help="project root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    config_parser = sub.add_parser("config")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    validate = config_sub.add_parser("validate")
    validate.set_defaults(func=cmd_config_validate)

    db_parser = sub.add_parser("db")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    init = db_sub.add_parser("init")
    init.set_defaults(func=cmd_db_init)
    migrate_cmd = db_sub.add_parser("migrate")
    migrate_cmd.set_defaults(func=cmd_db_init)

    schedule = sub.add_parser("schedule")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    list_cmd = schedule_sub.add_parser("list")
    list_cmd.set_defaults(func=cmd_schedule_list)

    run_job = sub.add_parser("run-job")
    run_job.add_argument("name")
    run_job.add_argument("--dry-run", action="store_true", default=False)
    run_job.add_argument(
        "--publish",
        action="store_true",
        default=False,
        help="Actually commit and push to the notes repo (default is dry-run)",
    )
    run_job.add_argument("--runner", choices=["local", "container"], default="local")
    run_job.add_argument("--image", default="7mimi-agent-runner:latest")
    run_job.add_argument("--network", default="none")
    run_job.add_argument("--memory", default="2g")
    run_job.add_argument("--pids-limit", type=int, default=256)
    run_job.set_defaults(func=cmd_run_job)

    runner_execute = sub.add_parser("runner-execute")
    runner_execute.add_argument("name")
    runner_execute.add_argument("--session-id", required=True)
    runner_execute.add_argument("--task-id", required=True)
    runner_execute.add_argument("--runner-root", default=None)
    runner_execute.add_argument("--dry-run", action="store_true", default=False)
    runner_execute.set_defaults(func=cmd_runner_execute)

    claude_smoke = sub.add_parser("claude-smoke")
    claude_smoke.add_argument("--prompt", default=None)
    claude_smoke.add_argument("--image", default="7mimi-agent-runner:latest")
    claude_smoke.add_argument("--network", default="bridge")
    claude_smoke.add_argument("--model", default="claude-haiku-4-5")
    claude_smoke.set_defaults(func=cmd_claude_smoke)

    mcp_x_readonly = sub.add_parser("mcp-x-readonly")
    mcp_x_readonly.add_argument("--host", default="127.0.0.1")
    mcp_x_readonly.add_argument("--port", type=int, default=18082)
    mcp_x_readonly.set_defaults(func=cmd_mcp_x_readonly)

    x_smoke = sub.add_parser("x-smoke")
    x_smoke.add_argument("--query", default="MCP server")
    x_smoke.add_argument("--max-results", type=int, default=10)
    x_smoke.add_argument("--mcp-url", default=None)
    x_smoke.set_defaults(func=cmd_x_smoke)

    stock = sub.add_parser("research-stock")
    stock.add_argument("ticker")
    stock.add_argument("--dry-run", action="store_true", default=False)
    stock.set_defaults(func=cmd_research_stock)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
