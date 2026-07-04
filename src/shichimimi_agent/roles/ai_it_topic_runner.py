from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shichimimi_agent.config.loader import AppConfig
from shichimimi_agent.db.repository import Repository
from shichimimi_agent.documents.markdown import TopicDigestItem, render_ai_it_daily_digest
from shichimimi_agent.documents.repository_writer import DocumentRepositoryWriter
from shichimimi_agent.hooks.post_tool_use import run_post_tool_use
from shichimimi_agent.hooks.pre_tool_use import PreToolUseInput, run_pre_tool_use
from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.security.policy_engine import PolicyEngine
from shichimimi_agent.util.time import now_jst


@dataclass(frozen=True)
class RunnerResult:
    status: str
    path: str
    title: str
    source_refs: list[dict[str, Any]]


class AiItTopicRunner:
    role = "ai_it_topic_runner"

    def __init__(
        self,
        *,
        config: AppConfig,
        repository: Repository,
        policy_engine: PolicyEngine,
        auth_client: AuthProxyClient | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.policy_engine = policy_engine
        self.auth_client = auth_client or AuthProxyClient(local_fallback_engine=policy_engine)
        self.writer = DocumentRepositoryWriter(config.root)

    def run_daily_digest(self, *, session_id: str, task_id: str, job: dict[str, Any], dry_run: bool = True) -> RunnerResult:
        inputs = job.get("inputs") or {}
        query_set_name = inputs.get("query_set", "ai_it_watch")
        query_set = (self.config.schedules.get("query_sets") or {}).get(query_set_name) or {}
        queries = list(query_set.get("queries") or [])

        # MVP: use deterministic mock collection, but still pass through policy/hook boundary.
        items = self._collect_mock_topics(session_id=session_id, task_id=task_id, queries=queries)
        markdown = render_ai_it_daily_digest(queries=queries, items=items, reviewed_posts=len(queries) * 3, fetched_urls=len(items))

        date = now_jst().date()
        relative_path = f"daily/{date:%Y}/{date:%m}/{date.isoformat()}.md"
        repo = (job.get("output") or {}).get("repo", "nishiog/ai-it-research-notes")

        decision = run_pre_tool_use(
            self.auth_client,
            PreToolUseInput(
                session_id=session_id,
                task_id=task_id,
                role=self.role,
                tool_name="document.commit_and_push_markdown_repo",
                arguments={"repo": repo, "path": relative_path},
            ),
        )
        if not decision.allowed:
            raise PermissionError(decision.reason)

        # For initial implementation, dry-run writes locally instead of pushing.
        write_result = self.writer.write_dry_run(relative_path=relative_path, content=markdown)
        run_post_tool_use(
            self.repository,
            session_id=session_id,
            task_id=task_id,
            role=self.role,
            tool_name="document.commit_and_push_markdown_repo",
            decision=decision.decision,
            success=1,
            output_size=len(markdown.encode("utf-8")),
        )

        source_refs = [{"type": "url", "url": item.evidence_url, "topic": item.topic} for item in items]
        self.repository.record_document(
            repo=repo if not dry_run else None,
            path=str(write_result.path.relative_to(self.config.root)),
            title=f"Daily AI/IT Digest - {date.isoformat()}",
            doc_type="ai_it_daily_digest",
            status="draft" if dry_run else "published",
            source_refs=source_refs,
            metadata={"dry_run": dry_run, "target_repo": repo, "target_path": relative_path},
        )
        return RunnerResult(status="succeeded", path=str(write_result.path), title=f"Daily AI/IT Digest - {date.isoformat()}", source_refs=source_refs)

    def _collect_mock_topics(self, *, session_id: str, task_id: str, queries: list[str]) -> list[TopicDigestItem]:
        for query in queries[:3]:
            decision = run_pre_tool_use(
                self.auth_client,
                PreToolUseInput(
                    session_id=session_id,
                    task_id=task_id,
                    role=self.role,
                    tool_name="x.search_posts_recent",
                    arguments={"query": query, "max_results": 50},
                ),
            )
            run_post_tool_use(
                self.repository,
                session_id=session_id,
                task_id=task_id,
                role=self.role,
                tool_name="x.search_posts_recent",
                decision=decision.decision,
                success=1 if decision.allowed else 0,
                output_size=0,
            )
            if not decision.allowed:
                raise PermissionError(decision.reason)

        return [
            TopicDigestItem(
                topic="MCP ecosystem",
                what_happened="X signals indicate continued discussion around MCP servers and agent tool integrations.",
                why_it_matters="MCP is becoming a common interface between agents and external tools.",
                evidence_url="https://modelcontextprotocol.io/",
                x_signal_url="https://x.com/search?q=%22MCP%20server%22",
                follow_up="Track new MCP server patterns and security guidance.",
            ),
            TopicDigestItem(
                topic="Claude Code / coding agents",
                what_happened="Developers are discussing agentic coding workflows and Claude Code usage patterns.",
                why_it_matters="Coding agents affect developer workflow, review practices, and repository automation.",
                evidence_url="https://docs.anthropic.com/en/docs/claude-code/overview",
                x_signal_url="https://x.com/search?q=%22Claude%20Code%22",
                follow_up="Compare local runner, container runner, and proxy boundary designs.",
            ),
            TopicDigestItem(
                topic="AI security / prompt injection",
                what_happened="Prompt injection and tool authorization remain recurring concerns for autonomous agents.",
                why_it_matters="Agent systems need deterministic controls outside the LLM.",
                evidence_url="https://owasp.org/www-project-top-10-for-large-language-model-applications/",
                x_signal_url="https://x.com/search?q=%22prompt%20injection%22%20%22AI%20security%22",
                follow_up="Collect concrete regression fixtures for prompt injection tests.",
            ),
        ]
