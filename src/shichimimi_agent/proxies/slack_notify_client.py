"""Client for auth-proxy's Slack notify endpoint (ADR-026).

auth-proxy alone holds the SLACK_WEBHOOK_URL secret and performs the
line-boundary chunking. The orchestrator only needs the same session Bearer
token used for the git relay / x-mcp (AUTH_PROXY_SESSION_TOKEN), wired here
under distinct env-var-agnostic constructor params so the caller decides
where the values come from.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


class SlackNotifyError(RuntimeError):
    """Raised when auth-proxy's /v1/slack/notify call fails or is denied."""


@dataclass(frozen=True)
class SlackNotifyClient:
    base_url: str
    session_token: str
    # auth-proxy posts chunks sequentially with a 700ms delay; a 200KB digest
    # can take ~40s upstream, so keep the client timeout comfortably above it.
    timeout_seconds: float = 120.0

    def notify(self, text: str) -> int:
        """POST text to auth-proxy's /v1/slack/notify. Returns the chunk count.

        Raises SlackNotifyError on any non-200 response or transport failure.
        """
        payload = json.dumps({"text": text}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/v1/slack/notify",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.session_token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalize all transport/HTTP errors
            raise SlackNotifyError(f"slack notify request failed: {exc}") from exc
        return int(body.get("chunks", 0))
