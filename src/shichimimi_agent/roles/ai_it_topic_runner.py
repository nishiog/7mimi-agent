from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from shichimimi_agent.config.loader import AppConfig
from shichimimi_agent.db.repository import Repository
from shichimimi_agent.documents.markdown import TopicDigestItem, render_ai_it_daily_digest
from shichimimi_agent.documents.repository_writer import DocumentRepositoryWriter
from shichimimi_agent.hooks.post_tool_use import run_post_tool_use
from shichimimi_agent.hooks.pre_tool_use import PreToolUseInput, run_pre_tool_use
from shichimimi_agent.config.model_selection import resolve_model
from shichimimi_agent.mcp.client import McpHttpClient
from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.proxies.claude_proxy_client import ClaudeProxyClient
from shichimimi_agent.research.signal_summarizer import summarize_signals
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
        mcp_client_factory: Callable[[str], McpHttpClient] | None = None,
        claude_client_factory: Callable[[str, str], ClaudeProxyClient] | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.policy_engine = policy_engine
        self.auth_client = auth_client or AuthProxyClient(local_fallback_engine=policy_engine)
        self.writer = DocumentRepositoryWriter(
            config.root,
            document_repositories=(config.policy.get("document_repositories") or {}),
        )
        self._mcp_client_factory = mcp_client_factory or (lambda base_url: McpHttpClient(base_url=base_url))
        self._claude_client_factory = claude_client_factory or (
            lambda session_id, role: ClaudeProxyClient(session_id=session_id, role=role)
        )

    def run_daily_digest(self, *, session_id: str, task_id: str, job: dict[str, Any], dry_run: bool = True) -> RunnerResult:
        inputs = job.get("inputs") or {}
        query_set_name = inputs.get("query_set", "ai_it_watch")
        query_set = (self.config.schedules.get("query_sets") or {}).get(query_set_name) or {}
        queries = list(query_set.get("queries") or [])

        items, reviewed_posts, fetched_urls = self._collect_topics(session_id=session_id, task_id=task_id, queries=queries)
        markdown = render_ai_it_daily_digest(queries=queries, items=items, reviewed_posts=reviewed_posts, fetched_urls=fetched_urls)

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

        if dry_run:
            write_result = self.writer.write_dry_run(relative_path=relative_path, content=markdown)
        else:
            write_result = self.writer.publish(
                repo=repo,
                relative_path=relative_path,
                content=markdown,
                commit_message=f"docs: daily AI/IT digest {date.isoformat()} (7mimi-agent)",
            )
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
            metadata={
                "dry_run": dry_run,
                "target_repo": repo,
                "target_path": relative_path,
                "pushed": write_result.pushed,
                "commit_sha": write_result.commit_sha,
            },
        )
        return RunnerResult(status="succeeded", path=str(write_result.path), title=f"Daily AI/IT Digest - {date.isoformat()}", source_refs=source_refs)

    def _collect_topics(
        self, *, session_id: str, task_id: str, queries: list[str]
    ) -> tuple[list[TopicDigestItem], int, int]:
        x_mcp_url = os.environ.get("X_MCP_URL")
        if x_mcp_url:
            return self._collect_real_topics(session_id=session_id, task_id=task_id, queries=queries, x_mcp_url=x_mcp_url)
        items = self._collect_mock_topics(session_id=session_id, task_id=task_id, queries=queries)
        return items, len(queries) * 3, len(items)

    def _collect_real_topics(
        self, *, session_id: str, task_id: str, queries: list[str], x_mcp_url: str
    ) -> tuple[list[TopicDigestItem], int, int]:
        client: McpHttpClient | None = None
        items: list[TopicDigestItem] = []
        reviewed_posts = 0
        for query in queries[:3]:
            # Authorization must happen before any MCP call, and before we know the
            # response size, so the pre-call post_tool_use record uses output_size=0.
            decision = run_pre_tool_use(
                self.auth_client,
                PreToolUseInput(
                    session_id=session_id,
                    task_id=task_id,
                    role=self.role,
                    tool_name="x.search_posts_recent",
                    arguments={"query": query, "max_results": 10},
                ),
            )
            if not decision.allowed:
                run_post_tool_use(
                    self.repository,
                    session_id=session_id,
                    task_id=task_id,
                    role=self.role,
                    tool_name="x.search_posts_recent",
                    decision=decision.decision,
                    success=0,
                    output_size=0,
                )
                raise PermissionError(decision.reason)

            if client is None:
                client = self._mcp_client_factory(x_mcp_url)
                client.initialize()

            result = client.call_tool("x.search_posts_recent", {"query": query, "max_results": 10})
            content = (result.get("content") or [{}])[0]
            text_payload = content.get("text", "")
            output_size = len(text_payload.encode("utf-8"))

            run_post_tool_use(
                self.repository,
                session_id=session_id,
                task_id=task_id,
                role=self.role,
                tool_name="x.search_posts_recent",
                decision=decision.decision,
                success=0 if result.get("isError") else 1,
                output_size=output_size,
            )

            if result.get("isError"):
                raise RuntimeError(f"x.search_posts_recent failed for query {query!r}: {text_payload}")

            posts = json.loads(text_payload or "{}").get("posts") or []
            reviewed_posts += len(posts)
            if not posts:
                continue

            best_post = None
            best_score = -1
            for post in posts:
                engagement = post.get("engagement") or {}
                score = int(engagement.get("like_count") or 0) + int(engagement.get("repost_count") or 0)
                if score > best_score:
                    best_score = score
                    best_post = post

            text_redacted = (best_post.get("text_redacted") or "").strip()
            collapsed = " ".join(text_redacted.split())
            what_happened = (collapsed[:200] if collapsed else "(no text)") + " (via X signal)"
            why_it_matters = "X で観測されたシグナル(自動収集、要ファクトチェック)"
            post_url = best_post.get("url") or ""
            urls = best_post.get("urls") or []
            # X posts are signals, never evidence: only a genuine external URL
            # from the post counts as evidence_url; otherwise leave it empty so
            # downstream rendering marks it as unverified rather than pointing
            # back at the X post itself.
            evidence_url = urls[0] if urls else ""

            summary = self._summarize_signals_if_enabled(
                session_id=session_id, task_id=task_id, query=query, posts=posts
            )
            if summary is not None:
                what_happened = summary.what_happened + " (via X signal, LLM要約)"
                why_it_matters = summary.why_it_matters

            items.append(
                TopicDigestItem(
                    topic=query,
                    what_happened=what_happened,
                    why_it_matters=why_it_matters,
                    evidence_url=evidence_url,
                    x_signal_url=post_url,
                    follow_up="収集シグナルの一次情報を確認する",
                )
            )

        if not items:
            raise RuntimeError("no X signals collected")

        fetched_urls = sum(1 for item in items if item.evidence_url)
        return items, reviewed_posts, fetched_urls

    def _summarize_signals_if_enabled(
        self, *, session_id: str, task_id: str, query: str, posts: list[dict[str, Any]]
    ):
        claude_proxy_url = os.environ.get("CLAUDE_PROXY_URL")
        claude_proxy_session_token = os.environ.get("CLAUDE_PROXY_SESSION_TOKEN")
        if not claude_proxy_url or not claude_proxy_session_token:
            return None

        decision = run_pre_tool_use(
            self.auth_client,
            PreToolUseInput(
                session_id=session_id,
                task_id=task_id,
                role=self.role,
                tool_name="claude.summarize_signals",
                arguments={"query": query, "post_count": len(posts)},
            ),
        )
        if not decision.allowed:
            run_post_tool_use(
                self.repository,
                session_id=session_id,
                task_id=task_id,
                role=self.role,
                tool_name="claude.summarize_signals",
                decision=decision.decision,
                success=0,
                output_size=0,
            )
            return None

        role_config = ((self.config.roles or {}).get("roles") or {}).get(self.role) or {}
        model = resolve_model(role_config, self.config.policy)
        client = self._claude_client_factory(session_id, self.role)
        summary = summarize_signals(client, model=model, query=query, posts=posts)

        output_size = 0
        if summary is not None:
            output_size = len(
                json.dumps(
                    {"what_happened": summary.what_happened, "why_it_matters": summary.why_it_matters},
                    ensure_ascii=False,
                ).encode("utf-8")
            )

        run_post_tool_use(
            self.repository,
            session_id=session_id,
            task_id=task_id,
            role=self.role,
            tool_name="claude.summarize_signals",
            decision=decision.decision,
            success=1 if summary is not None else 0,
            output_size=output_size,
        )
        return summary

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
