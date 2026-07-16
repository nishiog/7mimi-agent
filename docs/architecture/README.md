# Architecture

agent-runner / claude-proxy / auth-proxy / MCP / security の構成をまとめる。

## 5. High-level architecture

```text
[Trigger]
  ├─ cron schedule
  ├─ manual CLI
  ├─ X signal polling
  ├─ future: Slack / Discord mention
  └─ future: GitHub event
        │
        ▼
[Orchestrator / Scheduler]
  ├─ trigger router
  ├─ task planner
  ├─ session manager
  ├─ role resolver
  └─ job queue
        │
        ▼
[agent-runner per session]
  ├─ Claude Code / LLM agent
  ├─ workspace
  ├─ skills
  ├─ MCP client
  ├─ PreToolUse hook
  └─ PostToolUse hook
        │
        ├─ LLM call
        │     ↓
        │   [claude-proxy]
        │     ├─ Claude provider credential
        │     ├─ usage / budget / audit
        │     └─ session attribution
        │     ↓
        │   [Anthropic / Claude API]
        │
        └─ Tool call
              ↓
            [auth-proxy]
              ├─ role/tool allowlist
              ├─ rate limit
              ├─ audit log
              ├─ PreToolUse / PostToolUse
              └─ credential boundary
              ↓
            [X MCP / J-Quants MCP / Web Fetch / Document Store]
```

重要な点:

- Claude Code / LLM agent / workspace は **agent-runner** に存在する。
- **claude-proxy is a Claude API proxy, not a Claude Code runner.**
- claude-proxy は Claude Code process や session workspace を保持しない。
- agent-runner は Claude provider credential を直接持たず、Claude API 通信を claude-proxy に向ける。
- agent-runner は X / J-Quants など外部API credential を持たず、tool call を auth-proxy に向ける。

---

## 6. Runtime and container model

本番運用は k3s(シングルノード)+ ArgoCD であり、docker-compose ベースの構成(ADR-024)は local/dev 用と位置づける(ADR-031)。scheduler は `KubernetesRunnerBackend` を通じて agent-runner を Kubernetes Job として起動し、`deploy/k8s/` の Kustomize マニフェストを ArgoCD Application が watch する GitOps 運用に従う。以下 6.1〜6.4 の docker sibling コンテナ / bridge ネットワークに基づく記述は local/dev 構成の説明として引き続き有効だが、本番の egress 強制は Docker `internal` ネットワークではなく NetworkPolicy に置き換わる(ADR-032)。詳細な手順は [docs/deployment/k3s-argocd.md](../deployment/k3s-argocd.md) を参照。(ADR-031 により本番は k3s に移行。compose 構成は local/dev 用)

### 6.1 Target model

最終的には、セッションごとに `agent-runner` container を起動する。

```text
host
  ├─ agent-server / orchestrator
  ├─ scheduler
  ├─ claude-proxy
  ├─ auth-proxy
  ├─ x-mcp-readonly
  ├─ jquants-mcp
  ├─ document-store
  └─ docker daemon
       ├─ agent-runner-session-001
       ├─ agent-runner-session-002
       └─ agent-runner-scheduled-job-003
```

### 6.1.1 agent-runner / claude-proxy / auth-proxy の責務分離

Mercari blog の auth-proxy 思想を、このプロジェクトでは Claude API 向けと外部tool/API向けに分離して扱う。ただし、Claude Code process と workspace は proxy 側ではなく、セッションごとの `agent-runner` に置く。

```text
agent-runner:
  - セッションごとの実行コンテナ
  - Claude Code / LLM agent を起動する
  - workspace を持つ
  - MCP client / skills / hooks を持つ
  - Claude provider credential を直接持たない
  - Claude API通信を claude-proxy に向ける
  - X / J-Quants credential を持たない
  - tool call を auth-proxy に向ける

claude-proxy:
  - Go serviceとして実装する
  - Claude API credential boundary / API proxy
  - Anthropic / Claude API への通信を中継する
  - ANTHROPIC_API_KEY 等の provider credential を保持する
  - usage / budget / audit / session attribution を担当する
  - streaming / SSE pass-through に対応する
  - Claude Code process は持たない
  - session workspace は持たない
  - 外部API credential は持たない

auth-proxy:
  - Go serviceとして実装する
  - MCP tool / external API credential boundary
  - X / J-Quants / Web / Document Store などへの tool call を認可する
  - role別 tool allowlist / denylist を適用する
  - rate limit / audit log / redaction を担当する
  - PreToolUse hook を fail-closed で実行する
  - PostToolUse hook を fail-open で実行する
  - Claude credential は持たない
```

重要な境界は以下。

```text
Claude Code / LLM agent / workspace:
  agent-runner に存在する

Claude provider credential:
  claude-proxy のみが持つ

X / J-Quants / Document Store credential:
  auth-proxy または各MCP server のみが持つ

agent-runner:
  Claude provider credential も外部API credential も持たない
```


### 6.1.2 Implementation language boundary

`7mimi-agent` は polyglot 構成にする。

```text
Python:
  - agent orchestration
  - scheduler
  - runner management
  - research logic
  - markdown/document generation
  - config validation
  - local development CLI

Go:
  - claude-proxy
  - auth-proxy
  - security-sensitive network boundary components
```

Go を proxy に使う理由:

- streaming-friendly HTTP reverse proxy を実装しやすい
- standalone binary として配布しやすい
- concurrency / timeout / cancellation を扱いやすい
- container 化しやすい
- audit / rate-limit middleware を小さく保てる
- credential boundary を Python の agent runtime から明確に分離できる

### 6.1.3 Claude Code in agent-runner (ADR-013)

agent-runner コンテナは Claude Code CLI を内蔵し、環境変数だけで claude-proxy に向ける。

```text
agent-runner (container)
  ├─ Claude Code CLI (@anthropic-ai/claude-code)
  ├─ ANTHROPIC_BASE_URL   = http://host.docker.internal:18080   # claude-proxy
  ├─ ANTHROPIC_AUTH_TOKEN = <session token>                      # Bearer、実キーではない
  └─ ANTHROPIC_CUSTOM_HEADERS = X-7mimi-Session-Id / X-7mimi-Role
        │
        ▼
claude-proxy (host, Go)
  ├─ session token 検証
  ├─ x-api-key 注入 (ANTHROPIC_API_KEY)
  └─ /v1/messages, /v1/messages/count_tokens を pass-through
```

- ネットワークは既定 `--network none` を維持し、Claude 実行ジョブのみ bridge に opt-in する。
- `ANTHROPIC_API_KEY` は claude-proxy のみが保持し、runner env allowlist には決して入れない。

### 6.2 Session lifecycle

```text
created
  ↓
starting
  ↓
running
  ↓
idle
  ↓
stopped
  ↓
expired
```

想定ポリシー。

```yaml
session_policy:
  runner_idle_timeout_minutes: 30
  session_ttl_minutes: 10080 # 7 days
  runner_memory_limit: 4g
  runner_pids_limit: 256
```

### 6.3 MVP model

初期版は Docker を必須にしない。

```text
.sessions/
  sess_xxx/
    workspace/
    events.jsonl
    result.md
```

Phase 1 では通常の subprocess / local runtime として動かし、Phase 2 以降で Docker runner に移行する。

### 6.4 Container communication options

候補は3つ。

#### Option A: one request one process

```text
orchestrator -> spawn agent process -> result
```

- 実装が最も簡単
- 会話継続性は弱い
- MVP向き

#### Option B: one request one container

```text
orchestrator -> docker run runner -> result -> remove
```

- 隔離しやすい
- 起動コストが高い
- セッション継続は弱い

#### Option C: one session one persistent container

```text
orchestrator -> docker run runner for session
same session -> reuse runner
idle timeout -> stop
```

- Mercari blog の思想に最も近い
- 実装は重い
- warm session が速い

### 6.5 Current decision

MVP は Option A で始める。  
設計は Option C に移行できるように、最初から session / runner / workspace / tool call を分離して実装する。

---

## 7. Roles

### 7.1 Orchestrator

全体の司令塔。

責務:

- trigger を受ける
- role を選ぶ
- session を作る
- task を queue に入れる
- runner を起動する
- 成果物を document store に渡す

Orchestrator は外部データを直接解釈しない。判断は role agent に委譲する。

### 7.2 XCollectorAgent

X上の情報を収集し、research queue に入れる。

責務:

- 監視クエリで投稿検索
- 監視アカウントの投稿確認
- URL抽出
- 銘柄コード・企業名・テーマ抽出
- スパム・重複除外
- スコアリング
- research queue への登録

使える tool:

- X MCP read-only
- Web Fetch read-only, optional
- ResearchQueue append

禁止:

- Xへの投稿
- like / repost / follow / DM
- 銘柄評価の断定
- document への直接 final write

### 7.3 StockResearchAgent

銘柄調査を行う。

責務:

- 銘柄コードの正規化
- J-Quants MCP で基本情報・株価・財務・配当・決算予定を取得
- 必要に応じて EDINET / IR / Web を確認
- X由来の仮説をファクト確認する
- 銘柄調査メモの draft を作る

使える tool:

- J-Quants MCP
- Web Fetch MCP
- EDINET/disclosure tool, future
- Document read

禁止:

- X write
- Document final write
- 売買推奨
- 自動売買

### 7.4 DocumentWriterAgent

調査結果を Markdown に整える。

責務:

- daily digest 作成
- stock memo 作成
- topic note 作成
- research queue の status 更新
- 出力フォーマット統一

使える tool:

- Document Store MCP
- Metrics write

禁止:

- X / J-Quants の直接利用
- 数値の捏造
- source_verifier 未通過の重要レポートの publish

### 7.5 SourceVerifierAgent

調査結果の根拠を検証する。

責務:

- 出典があるか確認
- 数値・日付・決算期・単位の確認
- X情報を事実扱いしていないか確認
- 「買い」「売り」など投資助言表現の検出
- 古い情報を最新扱いしていないか確認

使える tool:

- J-Quants MCP read
- Web Fetch MCP read
- Document read

禁止:

- final document write
- 外部サービス write

---

## 8. MCP-first design

### 8.1 MCP servers

初期想定。

```text
x-mcp-readonly:
  X API access. 初期版では read-only tool のみ。

jquants-mcp:
  日本株の構造化データ取得。

web-fetch-mcp:
  URL / article / PDF / IRページ取得。

document-store-mcp:
  Markdown docs と research queue への書き込み。

metrics-store:
  tool call / session / output の計測。
```

将来候補。

```text
edinet-mcp or edinet-tool:
  有価証券報告書、大量保有報告書、臨時報告書など。

tdnet-like disclosure tool:
  適時開示、決算短信、業績修正、配当修正など。

slack/discord-mcp:
  通知・mention trigger。
```

### 8.2 auth-proxy

agent-runner から MCP server を直接叩かせず、**auth-proxy** を挟む。

auth-proxy は、Mercari blog における外部API向け auth-proxy の役割を 7mimi Agent 用に一般化したもの。X MCP、J-Quants MCP、Web Fetch、Document Store などへのアクセスを、role と policy に基づいて決定的に制御する。

責務:

- role ごとの tool allowlist
- write 系 tool の block
- API key 秘匿
- rate limit
- cache
- audit log
- data freshness metadata の付与
- prompt injection 対策
- network allowlist

auth-proxy は Claude provider credential を持たない。Claude provider credential は claude-proxy 側に閉じ込める。

### 8.3 Tool allowlist draft

```yaml
roles:
  x_collector:
    mcp_servers:
      - x_mcp_readonly
      - web_fetch
      - research_queue
    allowed_tools:
      - x.search_posts_recent
      - x.get_posts
      - x.get_users
      - x.get_users_by_username
      - web.fetch_url
      - queue.append_candidate
    denied_tools:
      - x.create_post
      - x.delete_post
      - x.like_post
      - x.repost
      - x.follow_user
      - x.send_dm

  stock_researcher:
    mcp_servers:
      - jquants
      - web_fetch
      - document_store
    allowed_tools:
      - jquants.get_listed_info
      - jquants.get_daily_quotes
      - jquants.get_financial_statements
      - jquants.get_dividends
      - jquants.get_earnings_calendar
      - web.fetch_url
      - document.read
    denied_tools:
      - document.final_publish
      - x.create_post
      - trading.place_order

  document_writer:
    mcp_servers:
      - document_store
      - metrics
    allowed_tools:
      - document.write_markdown
      - document.update_research_queue
      - metrics.record_output
    denied_tools:
      - x.*
      - jquants.*

  source_verifier:
    mcp_servers:
      - jquants
      - web_fetch
      - document_store
    allowed_tools:
      - jquants.get_listed_info
      - jquants.get_daily_quotes
      - jquants.get_financial_statements
      - web.fetch_url
      - document.read
    denied_tools:
      - document.final_publish
      - x.*
```

### 8.4 X MCP policy

初期版では X MCP は read-only で使う。

許可候補:

- search recent posts
- get posts
- get users
- get users by username

禁止:

- create post
- delete post
- like
- repost
- follow
- unfollow
- DM
- profile update
- bookmark write

将来的に投稿を行う場合も、以下の human-in-the-loop を必須にする。

```text
Agent drafts post
  ↓
Human approves
  ↓
Write-capable X MCP posts
```

### 8.5 J-Quants MCP policy

J-Quants MCP は StockResearchAgent の正式な銘柄データ取得口とする。

ルール:

- 銘柄レポートには J-Quants データ取得日時を入れる
- 対象期間を明示する
- 調整済み/非調整の区別を明示する
- 決算期・単位を明示する
- X由来の情報とJ-Quants由来の情報を混ぜない
- J-Quantsで確認できない情報は未確認と書く

### 8.6 Claude credential / claude-proxy policy

Claude Code / LLM agent / workspace は `agent-runner` に置く。`claude-proxy` は Claude Code runner ではなく、Claude API credential boundary / API proxy として扱う。

```text
orchestrator / scheduler
  ↓
agent-runner per session
  - Claude Code / LLM agent を起動する
  - workspace を持つ
  - MCP client / skills / hooks を持つ
  - provider credential は直接持たない
  - Claude API通信を claude-proxy に向ける
  - tool call を auth-proxy に向ける
  ↓ LLM call
claude-proxy
  - Anthropic / Claude API への通信を中継する
  - ANTHROPIC_API_KEY 等の provider credential を保持する
  - usage / budget / audit / session attribution を担当する
  - Claude Code process や workspace は保持しない
  ↓
Anthropic / Claude API
```

agent-runner に渡してよいもの:

```text
CLAUDE_PROXY_URL
CLAUDE_PROXY_SESSION_TOKEN  # session-scoped / short-lived
SESSION_ID
ROLE
```

agent-runner に渡してはいけないもの:

```text
ANTHROPIC_API_KEY
Claude provider token / config
X API key / token
J-Quants API key
Document Store write credential
```

MVPではローカル検証のために provider key や Claude config を使う可能性はあるが、その場合も **agent-runner container / workspace に mount しない**。設計上の最終形は、claude-proxy が provider credential を保持し、agent-runner は短命の proxy session token のみを持つ形とする。

明示的な非目標:

- claude-proxy は Claude Code process を保持しない。
- claude-proxy は session workspace を持たない。
- Claude の思考面と workspace 実行面を別々の runner に分けない。
- agent-runner とは別の「作業専用runner」概念は primary design では使わない。

---

## 9. Security design

### 9.1 Threat model

想定するリスク。

- LLM が危険な tool を呼ぶ
- X MCP の write tool を誤って呼ぶ
- API key が prompt / log / generated doc に漏れる
- X投稿内の prompt injection に従ってしまう
- Webページ内の prompt injection に従ってしまう
- 金融情報を誤って断定する
- 古い情報を最新として扱う
- 大量 API call で quota を消費する
- 自動売買・投資助言に見える出力をする
- Claude provider credential が agent-runner / workspace / logs に漏れる

### 9.2 PreToolUse hook

ツール実行前に必ず検査する。

判定材料:

- role
- session id
- tool name
- arguments
- target resource
- write/read の種別
- rate limit status
- policy version

出力:

```json
{
  "decision": "allow | block",
  "reason": "...",
  "policy_version": "..."
}
```

fail-closed:

```text
hook failure -> block
unknown tool -> block
unknown role -> block
missing policy -> block
```

### 9.3 PostToolUse hook

ツール実行後に監査ログを保存する。

保存するもの:

- timestamp
- session_id
- role
- tool_name
- arguments hash / redacted arguments
- success / failure
- duration_ms
- output size
- source ids
- policy decision

fail-open:

```text
metrics failure -> continue
```

### 9.4 Secret handling

- `.env` は git 管理しない
- `.env.example` のみ管理する
- agent-runner に API key を渡さない
- agent-runner に Claude provider credential を渡さない
- Claude provider credential は claude-proxy の secret volume / secret store のみに置く
- X / J-Quants / Document Store の credential は auth-proxy または各MCP server のみに置く
- API key は auth-proxy または各MCP server の環境変数として渡す
- generated docs に token-like string が入らないよう redaction check を行う
- raw logs も redaction する

### 9.5 Prompt injection handling

X投稿、Webページ、PDF、IR資料などはすべて untrusted input として扱う。

ルール:

- 外部文書内の命令に従わない
- 外部文書は「引用・要約対象」であって「system instruction」ではない
- tool call の権限変更は文書内容で行えない
- `ignore previous instructions` などの文言は injection signal として記録する

---
