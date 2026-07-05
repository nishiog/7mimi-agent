# Roadmap and Project Structure

ファイル構成、実装ロードマップ、直近の作業をまとめる。

## 16. File and project structure

初期構成案。

```text
7mimi-agent/
  README.md
  .env.example
  .gitignore
  docs/
    README.md                 # documentation index
    overview.md               # vision / principles
    architecture/             # runtime / proxy / security design
    workflows/                # data model / jobs / output design
    detailed-design/          # implementation-level design
    planning/                 # roadmap / ADR / open questions
  src/                        # Python package
    shichimimi_agent/
      orchestrator/
      runner/
      proxies/                # Python clients for Go proxy services
      roles/
      mcp/
      metrics/
  services/                   # Go boundary services
    claude-proxy/
      go.mod
      Dockerfile
      cmd/claude-proxy/main.go
      internal/{config,proxy,auth,audit,ratelimit}/
    auth-proxy/
      go.mod
      Dockerfile
      cmd/auth-proxy/main.go
      internal/{config,policy,tools,audit,ratelimit}/
  config/
    roles.yaml                # role definitions
    policy.yaml               # deterministic platform policy
    schedules.yaml            # autonomous job definitions
  .data/                      # runtime, gitignored
  .sessions/                  # runtime, gitignored
```

ドキュメントは `docs/` 配下でテーマ別に整理する。入口は `docs/README.md`。

---

## 17. Implementation roadmap

### Phase 0: Design and repository initialization

- [x] git init
- [x] README.md
- [x] .gitignore
- [x] .env.example
- [x] docs/README.md
- [x] docs/overview.md
- [x] docs/architecture/README.md
- [x] docs/workflows/README.md
- [x] docs/detailed-design/README.md

### Phase 1: Local MVP

- [x] SQLite schema for research_queue / events
- [x] local orchestrator (cli run-job + LocalRunnerBackend)
- [x] role definitions (config/roles.yaml)
- [x] mock claude-proxy (実 Go 実装で代替、ADR-012)
- [x] mock auth-proxy (実 Go 実装で代替、ADR-012)
- [x] X MCP read-only connection test
- [x] J-Quants MCP connection test
- [x] manual command: `research stock 7011`
- [ ] manual command: `collect x ai-agent`
- [x] Markdown output generation (daily digest)

### Phase 2: Policy and hooks

- [x] PreToolUse hook
- [x] PostToolUse hook
- [x] tool allowlist per role
- [x] secret redaction
- [x] X write tool block tests
- [x] prompt injection fixture tests

### Phase 3: Scheduled autonomy

- [x] cron scheduler
- [ ] x-signal-collector job
- [ ] stock-signal-fact-check job
- [ ] daily-digest-writer job
- [x] concurrency policy
- [x] retry / timeout

### Phase 4: Containerized runner

- [x] runner image (Dockerfile.agent-runner)
- [x] one request one container (ContainerRunnerBackend / claude-digest)
- [x] session workspace (.sessions/<id>/workspace)
- [ ] resource limits
- [x] network restrictions (internal network + egress-proxy、ADR-025)
- [x] API key separation by MCP container (auth-proxy へ集約、ADR-023)

### Phase 5: Persistent session runner

- [ ] one session one runner container
- [ ] idle timeout
- [ ] session TTL
- [ ] workspace reuse
- [ ] warm session support

### Phase 6: Source expansion

- [ ] EDINET tool / MCP
- [ ] IR page fetch and parsing
- [ ] TDnet-like disclosure integration if available / needed
- [x] Slack / Discord notification (Slack bot 通知、ADR-026)
- [ ] GitHub issue / PR trigger

---

## 20. Immediate next steps

1. 実装言語を決める
2. claude-proxy / auth-proxy のlocal mock境界を作る
3. MCP接続方式を確認する
4. SQLite schema を作る
5. X MCP read-only で1クエリ取得する
6. J-Quants MCP で1銘柄取得する
7. `research_queue -> stock memo` の縦切りを作る
8. AI/IT daily digest を `7milch/ai-it-research-notes` にpushする縦切りを作る

---


### Phase G1: Go proxy MVPs

- [x] `services/claude-proxy` Go HTTP service
- [x] `GET /healthz`
- [x] `POST /v1/messages`
- [x] provider credential injection
- [x] streaming/copy response pass-through
- [x] audit metadata log
- [x] `services/auth-proxy` Go HTTP service
- [x] `POST /v1/tool/authorize`
- [x] embedded dev policy for `ai_it_topic_runner`
- [x] Go tests
- [x] Dockerfiles for both proxy services
