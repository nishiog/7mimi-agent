"""Runner-side environment wiring for the git Smart HTTP relay (ADR-020).

The agent-runner container never holds git credentials: it points bare git
at the relay via URL-scoped `http.<url>.extraheader` config, carrying only
the session bearer token. auth-proxy injects the real GitHub credentials.
"""

from __future__ import annotations


def build_git_relay_env(*, proxy_url: str, session_token: str) -> dict[str, str]:
    """Build the GIT_CONFIG_* env vars that route git over the relay.

    Uses git's env-based config mechanism (GIT_CONFIG_COUNT/KEY_n/VALUE_n)
    so no on-disk gitconfig or credential helper is needed, and disables the
    credential helper and terminal prompt so git never falls back to asking
    for credentials directly.
    """
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"http.{proxy_url.rstrip('/')}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {session_token}",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_TERMINAL_PROMPT": "0",
    }
