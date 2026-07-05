from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shichimimi_agent.config import load_config
from shichimimi_agent.db import Repository, migrate
from shichimimi_agent.proxies.auth_proxy_client import AuthProxyClient
from shichimimi_agent.runner.stock_research import run_stock_research
from shichimimi_agent.security.policy_engine import PolicyEngine


class FakeMcpClient:
    def __init__(
        self,
        base_url: str,
        *,
        jq_responses: dict[str, dict[str, Any]] | None = None,
        jq_errors: dict[str, str] | None = None,
        x_posts: list[dict[str, Any]] | None = None,
        x_error: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.jq_responses = jq_responses or {}
        self.jq_errors = jq_errors or {}
        self.x_posts = x_posts
        self.x_error = x_error
        self.initialized = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def initialize(self) -> dict[str, Any]:
        self.initialized = True
        return {"protocolVersion": "2025-03-26"}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name in self.jq_errors:
            return {"content": [{"type": "text", "text": self.jq_errors[name]}], "isError": True}
        if name in self.jq_responses:
            return {
                "content": [{"type": "text", "text": json.dumps(self.jq_responses[name])}],
                "isError": False,
            }
        if name == "x.search_posts_recent":
            if self.x_error is not None:
                return {"content": [{"type": "text", "text": self.x_error}], "isError": True}
            posts = self.x_posts if self.x_posts is not None else []
            return {"content": [{"type": "text", "text": json.dumps({"posts": posts})}], "isError": False}
        raise AssertionError(f"unexpected tool call: {name}")


class StockResearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.config = load_config(self.root)
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "app.sqlite"
        migrate(db_path)
        self.repository = Repository(db_path)
        self.policy_engine = PolicyEngine(self.config.policy)
        self.auth_client = AuthProxyClient(local_fallback_engine=self.policy_engine)
        self._env_backup = dict(os.environ)
        os.environ["X_MCP_URL"] = "http://localhost:18081"
        os.environ["X_MCP_SESSION_TOKEN"] = "test-token"
        self._output_tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        self._output_tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _jq_responses(self) -> dict[str, dict[str, Any]]:
        return {
            "jq.get_listed_info": {"info": [{"Code": "7203", "CompanyName": "Toyota"}]},
            "jq.get_daily_quotes": {"daily_quotes": [{"Date": "2026-07-01", "Close": 3000}]},
            "jq.get_statements": {"statements": [{"FiscalYear": "2025", "NetSales": 1000000}]},
        }

    def _run(self, client: FakeMcpClient, code: str = "7203"):
        session_id = self.repository.create_session(source="test", role="stock_researcher", workspace_path="")
        task_id = self.repository.create_task(session_id=session_id, role="stock_researcher", input_data={"code": code})
        return run_stock_research(
            config=self.config,
            repository=self.repository,
            session_id=session_id,
            task_id=task_id,
            code=code,
            auth_client=self.auth_client,
            mcp_client_factory=lambda base_url: client,
        )

    def test_writes_memo_with_all_sections_and_source(self) -> None:
        client = FakeMcpClient(
            "http://localhost:18081",
            jq_responses=self._jq_responses(),
            x_posts=[{"text_redacted": "7203 上がってる", "url": "https://x.com/a/status/1"}],
        )
        result = self._run(client)
        try:
            content = result.path.read_text(encoding="utf-8")
            self.assertIn("## 基本情報", content)
            self.assertIn("## 株価", content)
            self.assertIn("## 財務", content)
            self.assertIn("## Xシグナル(未確認)", content)
            self.assertIn("## 取得時刻・出典", content)
            self.assertIn("J-Quants", content)
            self.assertIn("Toyota", content)
            self.assertIn("7203 上がってる", content)
        finally:
            result.path.unlink(missing_ok=True)

    def test_deny_path_raises_permission_error(self) -> None:
        policy = dict(self.config.policy)
        role_policy = dict(policy["role_tool_policy"])
        role_policy["stock_researcher"] = {"allow": [], "deny": ["jq.*"]}
        policy["role_tool_policy"] = role_policy
        denying_engine = PolicyEngine(policy)
        denying_auth_client = AuthProxyClient(local_fallback_engine=denying_engine)

        client = FakeMcpClient("http://localhost:18081", jq_responses=self._jq_responses())
        session_id = self.repository.create_session(source="test", role="stock_researcher", workspace_path="")
        task_id = self.repository.create_task(session_id=session_id, role="stock_researcher", input_data={"code": "7203"})

        with self.assertRaises(PermissionError):
            run_stock_research(
                config=self.config,
                repository=self.repository,
                session_id=session_id,
                task_id=task_id,
                code="7203",
                auth_client=denying_auth_client,
                mcp_client_factory=lambda base_url: client,
            )

    def test_jq_tool_error_raises_runtime_error_without_leaking_status_only(self) -> None:
        client = FakeMcpClient(
            "http://localhost:18081",
            jq_errors={"jq.get_listed_info": "jquants API error (status=404)"},
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._run(client)
        self.assertIn("status=404", str(ctx.exception))

    def test_x_signal_section_only_when_search_succeeds(self) -> None:
        client = FakeMcpClient(
            "http://localhost:18081",
            jq_responses=self._jq_responses(),
            x_error="X API error (status=429)",
        )
        result = self._run(client)
        try:
            content = result.path.read_text(encoding="utf-8")
            self.assertIn("(収集なし、または未確認)", content)
        finally:
            result.path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
