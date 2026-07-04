from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.security.policy_engine import PolicyDecision


@dataclass(frozen=True)
class PreToolUseInput:
    session_id: str
    task_id: str
    role: str
    tool_name: str
    arguments: dict[str, Any]


def run_pre_tool_use(authorizer: AuthProxyClient, payload: PreToolUseInput) -> PolicyDecision:
    try:
        return authorizer.authorize(
            session_id=payload.session_id,
            task_id=payload.task_id,
            role=payload.role,
            tool_name=payload.tool_name,
            arguments=payload.arguments,
        )
    except Exception as exc:  # fail-closed
        return PolicyDecision("block", f"auth authorization failed: {exc}")
