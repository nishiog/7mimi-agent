"""Diagnostic path for ADR-013: run Claude Code inside an agent-runner
container, pointed at claude-proxy, and let it perform a small autonomous
task in the session workspace.

The container gets only proxy coordinates and a session token — never
ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shichimimi_agent.runner.git_relay_env import build_git_relay_env

DEFAULT_PROMPT = (
    "You are running a smoke test. Create a file named hello.md in the current "
    "directory containing a haiku about proxies, then list the directory contents. "
    "Do nothing else."
)

DEFAULT_ALLOWED_TOOLS = "Write,Read,Bash(ls:*)"


@dataclass(frozen=True)
class ClaudeSmokeOptions:
    image: str = "7mimi-agent-runner:latest"
    docker_bin: str = "docker"
    network: str = "bridge"  # ADR-013: explicit opt-in; jobs default to none
    memory: str = "2g"
    pids_limit: int = 256
    max_turns: int = 6
    timeout_seconds: int = 300
    # ADR-016: claude-smoke is a cheap diagnostic and deliberately does not
    # go through config/model_selection.resolve_model; default is the
    # cheapest model to avoid unintended Opus-level cost.
    model: str = "claude-haiku-4-5"


@dataclass(frozen=True)
class ClaudeSmokeResult:
    exit_code: int
    stdout: str
    stderr: str
    workspace: Path


def build_docker_command(
    *,
    root: Path,
    session_id: str,
    role: str,
    workspace_rel: str,
    prompt: str,
    options: ClaudeSmokeOptions,
) -> list[str]:
    claude_proxy_url = os.environ.get("CLAUDE_PROXY_URL", "http://host.docker.internal:18080")
    session_token = os.environ.get("CLAUDE_PROXY_SESSION_TOKEN", "cp_sess_dev")
    env = {
        "SESSION_ID": session_id,
        "ROLE": role,
        # Claude Code standard env: base URL + bearer token + attribution headers.
        "ANTHROPIC_BASE_URL": claude_proxy_url,
        "ANTHROPIC_AUTH_TOKEN": session_token,
        "ANTHROPIC_CUSTOM_HEADERS": f"X-7mimi-Session-Id: {session_id}\nX-7mimi-Role: {role}",
        "ANTHROPIC_MODEL": options.model,
        # Keep Claude Code from writing config outside the workspace mount.
        "CLAUDE_CONFIG_DIR": f"/workspace/{workspace_rel}/.claude-config",
        "HOME": f"/workspace/{workspace_rel}",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    }

    # ADR-020: opt-in git relay wiring. The runner never holds git
    # credentials; when a proxy URL is configured it must also carry a
    # session token, otherwise fail fast rather than silently push without
    # auth.
    git_proxy_url = os.environ.get("GIT_PROXY_URL")
    if git_proxy_url:
        git_proxy_session_token = os.environ.get("GIT_PROXY_SESSION_TOKEN")
        if not git_proxy_session_token:
            raise ValueError(
                "GIT_PROXY_URL is set but GIT_PROXY_SESSION_TOKEN is missing; "
                "refusing to start the runner without a git relay session token."
            )
        env.update(build_git_relay_env(proxy_url=git_proxy_url, session_token=git_proxy_session_token))

    env_args: list[str] = []
    for key, value in env.items():
        env_args.extend(["-e", f"{key}={value}"])

    return [
        options.docker_bin,
        "run",
        "--rm",
        "--name",
        f"7mimi-claude-smoke-{session_id}",
        "--network",
        options.network,
        "--add-host",
        "host.docker.internal:host-gateway",
        "--memory",
        options.memory,
        "--pids-limit",
        str(options.pids_limit),
        "-v",
        f"{root}:/workspace",
        "-w",
        f"/workspace/{workspace_rel}",
        *env_args,
        options.image,
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--max-turns",
        str(options.max_turns),
        "--allowedTools",
        DEFAULT_ALLOWED_TOOLS,
        "--permission-mode",
        "acceptEdits",
    ]


def run_claude_smoke(
    *,
    root: Path,
    session_id: str,
    role: str,
    workspace: Path,
    prompt: str = DEFAULT_PROMPT,
    options: ClaudeSmokeOptions | None = None,
) -> ClaudeSmokeResult:
    options = options or ClaudeSmokeOptions()
    workspace_rel = workspace.resolve().relative_to(root.resolve()).as_posix()
    cmd = build_docker_command(
        root=root, session_id=session_id, role=role, workspace_rel=workspace_rel, prompt=prompt, options=options
    )
    completed = subprocess.run(
        cmd, cwd=root, text=True, capture_output=True, timeout=options.timeout_seconds
    )
    return ClaudeSmokeResult(
        exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr, workspace=workspace
    )


def summarize_result(result: ClaudeSmokeResult) -> dict[str, object]:
    summary: dict[str, object] = {
        "exit_code": result.exit_code,
        "workspace": str(result.workspace),
    }
    try:
        payload = json.loads(result.stdout)
        summary["claude_result"] = {
            "subtype": payload.get("subtype"),
            "num_turns": payload.get("num_turns"),
            "total_cost_usd": payload.get("total_cost_usd"),
            "result": (payload.get("result") or "")[:500],
        }
    except (json.JSONDecodeError, AttributeError):
        summary["raw_stdout"] = result.stdout[:1000]
    if result.stderr:
        summary["stderr"] = result.stderr[:1000]
    artifacts = [p.name for p in result.workspace.glob("*") if p.is_file()]
    summary["workspace_files"] = artifacts
    return summary
