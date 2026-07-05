# 7mimi Agent

MCP-first autonomous research agent inspired by Mercari Engineering's remote-claude / pcp-agent architecture.

Documentation starts here:

- [docs/README.md](docs/README.md)

Related generated notes repository:

- https://github.com/7milch/ai-it-research-notes

## Current implementation status

The first implementation slice provides:

- Python package skeleton
- YAML config loader and validator
- SQLite schema / migration
- deterministic policy engine skeleton
- redaction and path policy helpers
- AI/IT daily digest dry-run runner using mock signals
- Go proxy boundary services (`services/claude-proxy`, `services/auth-proxy`) MVP
- Python proxy clients (`shichimimi_agent.proxies`) with local policy fallback

## Development commands

Run commands from the repository root:

```bash
PYTHONPATH=src python3 -m shichimimi_agent config validate
PYTHONPATH=src python3 -m shichimimi_agent db init
PYTHONPATH=src python3 -m shichimimi_agent schedule list
PYTHONPATH=src python3 -m shichimimi_agent run-job ai-it-x-daily-digest --dry-run
```

Dry-run output is written under `.data/dry-run/` and is gitignored.

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Docker agent-runner

Build the initial one-request-one-container runner image:

```bash
docker build -f Dockerfile.agent-runner -t 7mimi-agent-runner:latest .
```

Run the AI/IT daily digest job inside an isolated `agent-runner` container:

```bash
PYTHONPATH=src python3 -m shichimimi_agent run-job ai-it-x-daily-digest --dry-run --runner container
```

The container runner mounts the repository at `/workspace`, writes dry-run output under `.data/dry-run/`, and does not receive provider/API credentials such as `ANTHROPIC_API_KEY`, X credentials, J-Quants credentials, or GitHub tokens.

By default the container runner uses `--network none` for the current mock/dry-run flow. Future real MCP/proxy integrations can opt into an explicit Docker network.

## Go proxy services

`claude-proxy` and `auth-proxy` are implemented in Go (see ADR-012). They form the security-sensitive network boundary: claude-proxy owns `ANTHROPIC_API_KEY`, auth-proxy owns tool authorization and external API credentials. Python keeps orchestration, research logic, and document generation.

Run Go tests:

```bash
cd services/claude-proxy && go test ./...
cd services/auth-proxy && go test ./...
cd services/egress-proxy && go test ./...
```

Build container images:

```bash
docker build -f services/claude-proxy/Dockerfile -t 7mimi-claude-proxy:latest services/claude-proxy
docker build -f services/auth-proxy/Dockerfile -t 7mimi-auth-proxy:latest services/auth-proxy
```

Run locally:

```bash
# from services/claude-proxy (listens on :18080)
ANTHROPIC_API_KEY=... go run ./cmd/claude-proxy
# from services/auth-proxy (listens on :18081)
go run ./cmd/auth-proxy
```

## Claude Code smoke test (ADR-013)

The agent-runner image bundles Claude Code CLI. `claude-smoke` runs Claude inside an isolated container, pointed at claude-proxy via `ANTHROPIC_BASE_URL` — the container never sees `ANTHROPIC_API_KEY`.

```bash
# 1. start claude-proxy on the host (holds the real credential)
cd services/claude-proxy && ANTHROPIC_API_KEY=sk-ant-... go run ./cmd/claude-proxy

# 2. rebuild the runner image, then run the smoke test from the repo root
docker build -f Dockerfile.agent-runner -t 7mimi-agent-runner:latest .
PYTHONPATH=src python3 -m shichimimi_agent claude-smoke
```

The default task asks Claude to write `hello.md` in the session workspace (`.sessions/<session>/workspace/`). Use `--prompt` to override.

## Git relay (ADR-020)

agent-runner never holds git credentials. Instead, git talks to auth-proxy's `/git/{owner}/{repo}` Smart HTTP relay, which authenticates the runner's session bearer token and forwards to GitHub using a short-lived GitHub App installation access token.

A GitHub App (`7mimi-agent`, permission `Contents: Read and write`) has already been created and installed on `7milch/ai-it-research-notes`. Adding more repos means adding them to that installation.

Start auth-proxy with the relay enabled:

```bash
export AUTH_PROXY_SESSION_TOKEN=$(openssl rand -hex 16)
export GITHUB_APP_ID=<App ID (App settings page)>
export GITHUB_APP_PRIVATE_KEY_PATH="$SHICHIMIMI_AGENT_X_GITHUB_APP_PRIVATE_KEY"
# GITHUB_APP_INSTALLATION_ID is optional — auto-discovered when the App has exactly one installation
cd services/auth-proxy && go run ./cmd/auth-proxy
```

The relay only mounts when `AUTH_PROXY_SESSION_TOKEN` is set and the GitHub App credentials resolve successfully; otherwise auth-proxy logs a non-sensitive reason and keeps serving the `/v1/tool/authorize` routes without it.

On the runner side, set `GIT_PROXY_URL` (e.g. `http://host.docker.internal:18081/git`) and `GIT_PROXY_SESSION_TOKEN` (must match `AUTH_PROXY_SESSION_TOKEN`) in the container env. `shichimimi_agent.runner.git_relay_env.build_git_relay_env` turns these into `GIT_CONFIG_*` env vars that route bare `git` through the relay with no on-disk credentials — `runner/claude_smoke.py` wires this in automatically when `GIT_PROXY_URL` is present (and raises early if the session token is missing).

Smoke-test the relay without a full runner container:

```bash
git -c http.http://127.0.0.1:18081/git/.extraheader="Authorization: Bearer $AUTH_PROXY_SESSION_TOKEN" \
    ls-remote http://127.0.0.1:18081/git/7milch/ai-it-research-notes
```

## Resident stack (docker compose)

`docker-compose.yml` (ADR-024) runs claude-proxy, auth-proxy, egress-proxy, and the scheduler (`schedule run`) as long-lived, `restart: unless-stopped` sidecar services so the daily digest job (ADR-021/022) runs without a human starting anything.

`egress-proxy` (ADR-025) enforces network-layer egress control: the scheduler and any agent-runner containers it launches attach only to the Docker-internal `7mimi-internal` network, so their only paths out are claude-proxy, auth-proxy, and egress-proxy. egress-proxy is a small self-built Go forward proxy (CONNECT tunneling for HTTPS, absolute-URI forwarding for plain HTTP) used for WebFetch: it resolves each destination hostname and denies RFC1918/loopback/link-local/unique-local IPs, non-80/443 ports, and `api.anthropic.com` (which must go through claude-proxy instead), dialing the validated IP directly to avoid DNS-rebinding TOCTOU.

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY, AUTH_PROXY_SESSION_TOKEN, X_BEARER_TOKEN,
# GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_HOST_PATH, and (optionally)
# CLAUDE_PROXY_DEV_TOKEN / REPO_ROOT in .env

# the agent-runner image must be built separately — the scheduler launches it
# as a sibling container via the host Docker daemon, docker compose does not
# build it
docker build -f Dockerfile.agent-runner -t 7mimi-agent-runner:latest .

docker compose up -d --build
docker compose ps
docker compose logs -f scheduler
```

Notes:

- The scheduler container mounts the repository at the *same absolute path* on host and container (`REPO_ROOT`, defaulting to `$PWD`) and mounts `/var/run/docker.sock`, so the `docker run -v <path>` commands it issues for agent-runner containers resolve correctly against the host Docker daemon (not the scheduler container's own filesystem).
- claude-proxy and auth-proxy publish `18080`/`18081` on the host for local/dev use; the scheduler and agent-runner containers reach all three proxies by service name (`claude-proxy`, `auth-proxy`, `egress-proxy`) over the internal Docker network `7mimi-internal`, not `host.docker.internal`.
- These host ports are bound on all interfaces (not just loopback) for local dev access; the only defense against LAN access is the session Bearer token, so on untrusted networks block `18080`/`18081` with the host firewall.
- Stop the stack with `docker compose down`; it does not remove `.data/`, `.sessions/`, or agent-runner images.

## Investment-cluster digest (ADR-026)

`invest-x-daily-digest` (role `investment_signal_runner`, cron `0 18 * * *` JST) collects日米株・暗号資産・マクロ signals from X and publishes a Japanese Slack-mrkdwn digest via auth-proxy's `POST /v1/slack/notify` — it never pushes to the notes repo. The runner container only gets `Read,Write,WebFetch` (no git relay, no Slack credential); the orchestrator authorizes the `slack.post_digest` tool call, appends a deterministic investment-advice disclaimer footer, then hands the text to `SlackNotifyClient`, which posts it through auth-proxy (the only holder of the Slack App bot token, chunked ≤3500 chars on line boundaries).

```bash
# requires X_MCP_URL/X_MCP_SESSION_TOKEN, CLAUDE_PROXY_URL/CLAUDE_PROXY_SESSION_TOKEN,
# SLACK_NOTIFY_URL/SLACK_NOTIFY_SESSION_TOKEN (set automatically by docker-compose.yml)
PYTHONPATH=src python3 -m shichimimi_agent invest-digest --job invest-x-daily-digest
```

auth-proxy's `/v1/slack/notify` route delivers via the Slack Web API (`chat.postMessage`) using a Slack App bot token, not an Incoming Webhook — this leaves room to add mention receiving (Events API / Socket Mode) later. To enable it:

1. Create (or reuse) a Slack App and grant it the `chat:write` bot scope, then install it to your workspace to obtain a bot token (`xoxb-...`).
2. Invite the bot to the target channel: `/invite @your-bot-name`.
3. Set `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` in `.env`.

Leaving either unset keeps `/v1/slack/notify` unmounted and the job unable to publish.
