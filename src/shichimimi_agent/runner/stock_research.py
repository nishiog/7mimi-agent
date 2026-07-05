"""ADR-027: `research stock <code>` manual command.

Deterministic (no LLM) stock research memo generation for a single stock
code. J-Quants tools (jq.get_listed_info / jq.get_daily_quotes /
jq.get_statements) are called through auth-proxy's /mcp endpoint under
PreToolUse authorization (role stock_researcher); their responses are
structured evidence and are rendered as-is. An optional X signal query is
made best-effort: an isError result or MCP failure is skipped silently
(X is signal, never evidence, and is not required for the memo to be
useful).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shichimimi_agent.config.loader import AppConfig
from shichimimi_agent.db.repository import Repository
from shichimimi_agent.hooks.post_tool_use import run_post_tool_use
from shichimimi_agent.hooks.pre_tool_use import PreToolUseInput, run_pre_tool_use
from shichimimi_agent.mcp.client import McpHttpClient
from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.security.policy_engine import PolicyEngine
from shichimimi_agent.util.time import now_jst

ROLE = "stock_researcher"

JQ_TOOLS = ("jq.get_listed_info", "jq.get_daily_quotes", "jq.get_statements")


@dataclass(frozen=True)
class StockResearchResult:
    code: str
    path: Path
    markdown: str
    document_id: str


def _call_jq_tool(
    *,
    client: McpHttpClient,
    auth_client: AuthProxyClient,
    repository: Repository,
    session_id: str,
    task_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    decision = run_pre_tool_use(
        auth_client,
        PreToolUseInput(
            session_id=session_id,
            task_id=task_id,
            role=ROLE,
            tool_name=tool_name,
            arguments=arguments,
        ),
    )
    if not decision.allowed:
        run_post_tool_use(
            repository,
            session_id=session_id,
            task_id=task_id,
            role=ROLE,
            tool_name=tool_name,
            decision=decision.decision,
            success=0,
            output_size=0,
        )
        raise PermissionError(decision.reason)

    result = client.call_tool(tool_name, arguments)
    content = (result.get("content") or [{}])[0]
    text_payload = content.get("text", "")
    output_size = len(text_payload.encode("utf-8"))

    run_post_tool_use(
        repository,
        session_id=session_id,
        task_id=task_id,
        role=ROLE,
        tool_name=tool_name,
        decision=decision.decision,
        success=0 if result.get("isError") else 1,
        output_size=output_size,
    )

    if result.get("isError"):
        # J-Quants error text carries only the upstream HTTP status (never
        # the idToken/refresh token, ADR-027) -- safe to surface verbatim.
        raise RuntimeError(f"{tool_name} failed: {text_payload}")

    try:
        return json.loads(text_payload or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{tool_name} returned invalid JSON") from exc


def _try_collect_x_signal(
    *,
    client: McpHttpClient,
    auth_client: AuthProxyClient,
    repository: Repository,
    session_id: str,
    task_id: str,
    code: str,
) -> list[dict[str, Any]]:
    """Best-effort X signal collection for the given stock code.

    Returns an empty list (never raises) when X is not configured, the
    authorization is denied, the MCP call fails, or the tool returns
    isError -- X signals are optional context, never required evidence.
    """
    query = f"{code} 株"
    tool_name = "x.search_posts_recent"
    try:
        decision = run_pre_tool_use(
            auth_client,
            PreToolUseInput(
                session_id=session_id,
                task_id=task_id,
                role=ROLE,
                tool_name=tool_name,
                arguments={"query": query, "max_results": 10},
            ),
        )
        if not decision.allowed:
            run_post_tool_use(
                repository,
                session_id=session_id,
                task_id=task_id,
                role=ROLE,
                tool_name=tool_name,
                decision=decision.decision,
                success=0,
                output_size=0,
            )
            return []

        result = client.call_tool(tool_name, {"query": query, "max_results": 10})
        content = (result.get("content") or [{}])[0]
        text_payload = content.get("text", "")
        output_size = len(text_payload.encode("utf-8"))

        run_post_tool_use(
            repository,
            session_id=session_id,
            task_id=task_id,
            role=ROLE,
            tool_name=tool_name,
            decision=decision.decision,
            success=0 if result.get("isError") else 1,
            output_size=output_size,
        )

        if result.get("isError"):
            return []
        return json.loads(text_payload or "{}").get("posts") or []
    except Exception:
        return []


def render_stock_research_memo(
    *,
    code: str,
    listed_info: dict[str, Any],
    daily_quotes: dict[str, Any],
    statements: dict[str, Any],
    x_posts: list[dict[str, Any]],
    fetched_at: str,
) -> str:
    lines: list[str] = [
        "---",
        f"title: Stock Research Memo - {code}",
        f"code: {code}",
        "generated_by: 7mimi-agent",
        "role: stock_researcher",
        "source_policy: x_is_signal_not_evidence",
        "---",
        "",
        f"# Stock Research Memo - {code}",
        "",
        "## 基本情報",
        "",
    ]
    info_rows = listed_info.get("info") or []
    if info_rows:
        lines.append("```json")
        lines.append(json.dumps(info_rows[0], ensure_ascii=False, indent=2))
        lines.append("```")
    else:
        lines.append("(データなし)")

    lines.extend(["", "## 株価", ""])
    quote_rows = daily_quotes.get("daily_quotes") or []
    if quote_rows:
        recent = quote_rows[-5:]
        lines.append("```json")
        lines.append(json.dumps(recent, ensure_ascii=False, indent=2))
        lines.append("```")
    else:
        lines.append("(データなし)")

    lines.extend(["", "## 財務", ""])
    statement_rows = statements.get("statements") or []
    if statement_rows:
        lines.append("```json")
        lines.append(json.dumps(statement_rows, ensure_ascii=False, indent=2))
        lines.append("```")
    else:
        lines.append("(データなし)")

    lines.extend(["", "## Xシグナル(未確認)", ""])
    if x_posts:
        lines.append("X ポストは signal であり、evidence として扱わないこと。")
        lines.append("")
        for post in x_posts[:5]:
            text = (post.get("text_redacted") or "").strip()
            url = post.get("url") or ""
            lines.append(f"- {text} ({url})")
    else:
        lines.append("(収集なし、または未確認)")

    lines.extend([
        "",
        "## 取得時刻・出典",
        "",
        f"- 取得時刻: {fetched_at}",
        "- 出典: J-Quants API (jq.get_listed_info / jq.get_daily_quotes / jq.get_statements)",
        "",
    ])
    return "\n".join(lines)


def run_stock_research(
    *,
    config: AppConfig,
    repository: Repository,
    session_id: str,
    task_id: str,
    code: str,
    auth_client: AuthProxyClient | None = None,
    mcp_client_factory: Callable[[str], McpHttpClient] | None = None,
) -> StockResearchResult:
    auth_client = auth_client or AuthProxyClient(local_fallback_engine=PolicyEngine(config.policy))

    mcp_url = os.environ.get("X_MCP_URL")
    if not mcp_url:
        raise RuntimeError("X_MCP_URL is not set; cannot reach the J-Quants /mcp endpoint")
    session_token = os.environ.get("X_MCP_SESSION_TOKEN")

    factory = mcp_client_factory or (lambda base_url: McpHttpClient(base_url=base_url, session_token=session_token))
    client = factory(mcp_url)
    client.initialize()

    listed_info = _call_jq_tool(
        client=client,
        auth_client=auth_client,
        repository=repository,
        session_id=session_id,
        task_id=task_id,
        tool_name="jq.get_listed_info",
        arguments={"code": code},
    )
    daily_quotes = _call_jq_tool(
        client=client,
        auth_client=auth_client,
        repository=repository,
        session_id=session_id,
        task_id=task_id,
        tool_name="jq.get_daily_quotes",
        arguments={"code": code},
    )
    statements = _call_jq_tool(
        client=client,
        auth_client=auth_client,
        repository=repository,
        session_id=session_id,
        task_id=task_id,
        tool_name="jq.get_statements",
        arguments={"code": code},
    )

    x_posts = _try_collect_x_signal(
        client=client,
        auth_client=auth_client,
        repository=repository,
        session_id=session_id,
        task_id=task_id,
        code=code,
    )

    fetched_at = now_jst().isoformat(timespec="seconds")
    markdown = render_stock_research_memo(
        code=code,
        listed_info=listed_info,
        daily_quotes=daily_quotes,
        statements=statements,
        x_posts=x_posts,
        fetched_at=fetched_at,
    )

    date = now_jst().date().isoformat()
    relative_path = Path(".data/generated/stocks") / f"{code}-{date}.md"
    output_path = config.root / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    document_id = repository.record_document(
        repo=None,
        path=str(relative_path),
        title=f"Stock Research Memo - {code}",
        doc_type="stock_research_memo",
        status="draft",
        source_refs=[{"type": "jquants", "code": code}],
        metadata={"code": code, "fetched_at": fetched_at},
    )

    return StockResearchResult(code=code, path=output_path, markdown=markdown, document_id=document_id)
