"""Structural checks on docker-compose.yml (Issue #16 / ADR-024, Issue #17 /
ADR-025).

These are lightweight PyYAML parsing checks, not a `docker compose config`
invocation (Docker isn't guaranteed to be available in CI), to keep the
resident-stack compose file honest about its expected shape:
- exactly the four resident services (claude-proxy, auth-proxy,
  egress-proxy, scheduler)
- no plaintext secrets committed (values are all ${VAR} env references)
- scheduler mounts the host Docker socket
- proxy ports are published on the host (egress-proxy is not)
- the `internal` network is Docker-internal and scheduler/agent-runner are
  confined to it; claude-proxy/auth-proxy/egress-proxy also reach `egress`
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

# A bare ${VAR}, ${VAR:-default} (default may itself be another ${VAR}
# reference, a literal, or empty), or ${VAR:?error message} required-var
# reference.
ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(:-.*|:\?.*)?\}$")

# Env/volume keys that must fail loudly (compose interpolation error) instead
# of silently degrading to an empty string when unset, because an empty value
# would make the corresponding proxy feature (gitrelay/x-mcp) silently
# disabled or would send an empty Bearer token.
REQUIRED_VAR_KEYS = {
    "ANTHROPIC_API_KEY",
    "AUTH_PROXY_SESSION_TOKEN",
    "X_BEARER_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_HOST_PATH",
}


class ComposeConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(COMPOSE_PATH.exists(), f"missing {COMPOSE_PATH}")
        with COMPOSE_PATH.open("r", encoding="utf-8") as fh:
            self.compose = yaml.safe_load(fh)

    def test_expected_services_present(self) -> None:
        services = self.compose.get("services") or {}
        self.assertEqual(
            set(services.keys()),
            {"claude-proxy", "auth-proxy", "egress-proxy", "scheduler"},
        )

    def test_restart_policy_and_build_context(self) -> None:
        services = self.compose["services"]
        for name, build_ctx in (
            ("claude-proxy", "services/claude-proxy"),
            ("auth-proxy", "services/auth-proxy"),
            ("egress-proxy", "services/egress-proxy"),
        ):
            svc = services[name]
            self.assertEqual(svc["restart"], "unless-stopped")
            self.assertIn("healthcheck", svc)
            build = svc["build"]
            context = build["context"] if isinstance(build, dict) else build
            self.assertEqual(context, build_ctx)

        scheduler = services["scheduler"]
        self.assertEqual(scheduler["restart"], "unless-stopped")
        self.assertEqual(scheduler["build"]["dockerfile"], "Dockerfile.scheduler")

    def test_ports_published_on_host(self) -> None:
        services = self.compose["services"]
        self.assertIn("18080:18080", services["claude-proxy"]["ports"])
        self.assertIn("18081:18081", services["auth-proxy"]["ports"])
        self.assertNotIn("ports", services["scheduler"])

    def test_egress_proxy_has_no_published_ports(self) -> None:
        """egress-proxy is only reachable from the internal network — it
        must never be published on the host (ADR-025)."""
        self.assertNotIn("ports", self.compose["services"]["egress-proxy"])

    def test_internal_network_is_docker_internal(self) -> None:
        networks = self.compose.get("networks") or {}
        self.assertIn("internal", networks)
        internal = networks["internal"]
        self.assertTrue(internal.get("internal"), internal)
        # A stable, explicit network name so RUNNER_NETWORK (used by the
        # scheduler to attach sibling agent-runner containers) does not
        # depend on Compose's project-name-derived default.
        self.assertEqual(internal.get("name"), "7mimi-internal")

    def test_egress_network_is_not_internal(self) -> None:
        networks = self.compose.get("networks") or {}
        self.assertIn("egress", networks)
        egress = networks["egress"] or {}
        self.assertFalse(egress.get("internal"))

    def test_proxies_attach_to_both_networks(self) -> None:
        services = self.compose["services"]
        for name in ("claude-proxy", "auth-proxy", "egress-proxy"):
            self.assertEqual(set(services[name]["networks"]), {"internal", "egress"})

    def test_scheduler_attaches_to_internal_network_only(self) -> None:
        scheduler = self.compose["services"]["scheduler"]
        self.assertEqual(scheduler["networks"], ["internal"])

    def test_scheduler_mounts_docker_socket(self) -> None:
        volumes = self.compose["services"]["scheduler"]["volumes"]
        self.assertTrue(
            any(v.startswith("/var/run/docker.sock:") for v in volumes),
            volumes,
        )

    def test_scheduler_mounts_repo_at_identical_path(self) -> None:
        volumes = self.compose["services"]["scheduler"]["volumes"]
        repo_mounts = [v for v in volumes if not v.startswith("/var/run/docker.sock")]
        self.assertEqual(len(repo_mounts), 1)
        mount = repo_mounts[0]
        # The mount is "<host>:<container>" but the host/container path here
        # is itself a `${VAR:-default}` expression containing its own `:`, so
        # a naive split(":", 1) breaks. Both sides are identical by
        # construction (see docker-compose.yml comment) once repeated
        # exactly, so split the string exactly in half instead.
        self.assertEqual(len(mount) % 2, 1, mount)
        half = len(mount) // 2
        host_path, sep, container_path = mount[:half], mount[half], mount[half + 1:]
        self.assertEqual(sep, ":")
        self.assertEqual(host_path, container_path)

    def test_no_plaintext_secrets(self) -> None:
        """Every environment value must be an ${VAR} reference, a plain
        literal endpoint URL (http://host.docker.internal:...), or empty —
        never a literal secret string."""
        allowed_literal_prefixes = ("http://", "cp_sess_dev", "7mimi-internal", "")
        services = self.compose["services"]
        for name, svc in services.items():
            env = svc.get("environment")
            if not env:
                continue
            items = env.items() if isinstance(env, dict) else (
                tuple(e.split("=", 1)) if "=" in e else (e, "") for e in env
            )
            for key, value in items:
                value = "" if value is None else str(value)
                if value.startswith(allowed_literal_prefixes):
                    continue
                self.assertRegex(
                    value,
                    ENV_REF_RE,
                    msg=f"{name}.environment.{key} looks like a literal secret: {value!r}",
                )

    def test_scheduler_depends_on_both_proxies_healthy(self) -> None:
        depends_on = self.compose["services"]["scheduler"]["depends_on"]
        self.assertEqual(set(depends_on.keys()), {"claude-proxy", "auth-proxy", "egress-proxy"})
        for dep in depends_on.values():
            self.assertEqual(dep["condition"], "service_healthy")

    def test_scheduler_has_runner_network_and_egress_proxy_env(self) -> None:
        scheduler_env = self.compose["services"]["scheduler"]["environment"]
        self.assertEqual(scheduler_env["RUNNER_NETWORK"], "7mimi-internal")
        self.assertEqual(scheduler_env["RUNNER_EGRESS_PROXY"], "http://egress-proxy:18082")
        # Proxy URLs used by orchestration code running inside the scheduler
        # container (both runner-facing and its own clone-back check) must
        # use service-name addressing now that scheduler has no
        # host.docker.internal route (internal network has no extra_hosts).
        self.assertEqual(scheduler_env["CLAUDE_PROXY_URL"], "http://claude-proxy:18080")
        self.assertEqual(scheduler_env["GIT_PROXY_URL"], "http://auth-proxy:18081")
        self.assertEqual(scheduler_env["GIT_PROXY_URL_HOST"], "http://auth-proxy:18081")
        self.assertEqual(scheduler_env["X_MCP_URL"], "http://auth-proxy:18081")

    def test_scheduler_has_no_extra_hosts(self) -> None:
        """host.docker.internal is unreachable/unnecessary once the
        scheduler is service-name-addressed on the internal network."""
        self.assertNotIn("extra_hosts", self.compose["services"]["scheduler"])

    def test_github_app_pem_mounted_read_only(self) -> None:
        volumes = self.compose["services"]["auth-proxy"]["volumes"]
        pem_mounts = [v for v in volumes if v.endswith("github-app-key.pem:ro")]
        self.assertEqual(len(pem_mounts), 1, volumes)

    def test_scheduler_working_dir_matches_repo_mount(self) -> None:
        scheduler = self.compose["services"]["scheduler"]
        volumes = scheduler["volumes"]
        repo_mounts = [v for v in volumes if not v.startswith("/var/run/docker.sock")]
        self.assertEqual(len(repo_mounts), 1, volumes)
        mount = repo_mounts[0]
        half = len(mount) // 2
        container_path = mount[half + 1:]
        self.assertEqual(scheduler["working_dir"], container_path)

    def test_x_mcp_session_token_matches_auth_proxy_session_token(self) -> None:
        scheduler_env = self.compose["services"]["scheduler"]["environment"]
        expected = "${AUTH_PROXY_SESSION_TOKEN:?AUTH_PROXY_SESSION_TOKEN is required}"
        self.assertEqual(scheduler_env["X_MCP_SESSION_TOKEN"], expected)
        self.assertEqual(scheduler_env["GIT_PROXY_SESSION_TOKEN"], expected)

    def test_scheduler_has_no_published_ports(self) -> None:
        self.assertNotIn("ports", self.compose["services"]["scheduler"])

    def test_scheduler_has_slack_notify_env(self) -> None:
        """ADR-026: invest-x-daily-digest publishes via auth-proxy's
        /v1/slack/notify using the same session Bearer as gitrelay/x-mcp."""
        scheduler_env = self.compose["services"]["scheduler"]["environment"]
        self.assertEqual(scheduler_env["SLACK_NOTIFY_URL"], "http://auth-proxy:18081")
        expected = "${AUTH_PROXY_SESSION_TOKEN:?AUTH_PROXY_SESSION_TOKEN is required}"
        self.assertEqual(scheduler_env["SLACK_NOTIFY_SESSION_TOKEN"], expected)

    def test_auth_proxy_slack_bot_token_and_channel_are_optional(self) -> None:
        """SLACK_BOT_TOKEN/SLACK_CHANNEL_ID must NOT use the required
        ${VAR:?...} syntax: unset values should silently leave
        /v1/slack/notify unmounted, not fail `docker compose config`."""
        auth_proxy_env = self.compose["services"]["auth-proxy"]["environment"]
        self.assertEqual(auth_proxy_env["SLACK_BOT_TOKEN"], "${SLACK_BOT_TOKEN:-}")
        self.assertEqual(auth_proxy_env["SLACK_CHANNEL_ID"], "${SLACK_CHANNEL_ID:-}")

    def test_required_secrets_use_required_var_syntax(self) -> None:
        """Secrets must use ${VAR:?msg} so `docker compose config` fails
        loudly instead of silently starting with an empty/missing value
        (which previously left gitrelay/x-mcp silently disabled and sent
        empty Bearer tokens downstream)."""
        raw = COMPOSE_PATH.read_text(encoding="utf-8")
        for key in REQUIRED_VAR_KEYS:
            pattern = re.compile(r"\$\{" + re.escape(key) + r":\?[^}]+\}")
            matches = pattern.findall(raw)
            self.assertGreaterEqual(
                len(matches), 1, f"{key} has no ${{{key}:?...}} reference in compose file"
            )

        # And the optional ones must NOT be forced required.
        self.assertIn("${GITHUB_APP_INSTALLATION_ID:-}", raw)
        self.assertIn("${CLAUDE_PROXY_DEV_TOKEN:-cp_sess_dev}", raw)


if __name__ == "__main__":
    unittest.main()
