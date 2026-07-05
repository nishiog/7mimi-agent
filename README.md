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
