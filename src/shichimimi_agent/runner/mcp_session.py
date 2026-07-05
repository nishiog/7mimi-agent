"""ADR-028: mint role-bound session tokens from auth-proxy's POST
/session/issue for the digest jobs' direct /mcp connection (the sole
collection flow for both ai-it and invest; the old pre-collection path
was removed).

The orchestrator (the only holder of the static AUTH_PROXY_SESSION_TOKEN /
X_MCP_SESSION_TOKEN admin credential) calls issue_session to mint a
short-lived, role-scoped token that the runner container then uses solely
for its own /mcp Streamable HTTP MCP connection. stdlib only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class McpSessionError(Exception):
    pass


@dataclass(frozen=True)
class IssuedSession:
    token: str
    ttl_seconds: int


def issue_session(*, auth_proxy_url: str, static_token: str, role: str, timeout_seconds: float = 10.0) -> IssuedSession:
    """POST {auth_proxy_url}/session/issue with the static admin bearer,
    returning the minted (token, ttl_seconds)."""
    endpoint = f"{auth_proxy_url.rstrip('/')}/session/issue"
    body = json.dumps({"role": role}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {static_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise McpSessionError(f"session/issue failed: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise McpSessionError(f"session/issue failed: {exc.reason}") from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise McpSessionError(f"session/issue returned invalid JSON: {exc}") from None

    token = payload.get("token")
    ttl_seconds = payload.get("ttl_seconds")
    if not token or not isinstance(ttl_seconds, int):
        raise McpSessionError(f"session/issue returned unexpected payload: {payload}")

    return IssuedSession(token=token, ttl_seconds=ttl_seconds)
