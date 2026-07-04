"""Minimal MCP HTTP client (JSON-RPC 2.0 over Streamable HTTP), stdlib only."""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class McpClientError(Exception):
    pass


@dataclass
class McpHttpClient:
    base_url: str
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        self._id_counter = itertools.count(1)

    def _endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/mcp"

    def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(message).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise McpClientError(f"MCP request failed: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise McpClientError(f"MCP request failed: {exc.reason}") from None
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _next_id(self) -> int:
        return next(self._id_counter)

    def initialize(self) -> dict[str, Any]:
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "shichimimi-agent-x-smoke", "version": "0.1.0"},
                },
            }
        )
        if response is None or "error" in response:
            raise McpClientError(f"initialize failed: {response}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response["result"]

    def list_tools(self) -> list[dict[str, Any]]:
        response = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"})
        if response is None or "error" in response:
            raise McpClientError(f"tools/list failed: {response}")
        return response["result"]["tools"]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        if response is None or "error" in response:
            raise McpClientError(f"tools/call failed: {response}")
        return response["result"]
