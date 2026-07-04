# Architecture Decision Records

設計判断の履歴をまとめる。

## 18. ADR: decisions so far

### ADR-001: Single design document

Decision: 設計は `docs/design.md` に集約する。  
Reason: ドキュメントが散らばることを避けるため。

### ADR-002: MCP-first architecture

Decision: 外部サービス連携は原則 MCP 経由にする。  
Reason: Agent runtime と API 実装・認証情報を分離しやすく、role-based policy を適用しやすいため。

### ADR-003: X is signal, not evidence

Decision: X情報は調査トリガーとして扱い、銘柄評価の根拠にはしない。  
Reason: Xには噂、ノイズ、ポジショントーク、誤情報が混ざるため。

### ADR-004: J-Quants MCP as primary stock data source

Decision: 日本株の構造化データは J-Quants MCP を主たる取得口にする。  
Reason: 契約済みであり、自律AgentからMCPとして扱いやすいため。

### ADR-005: X MCP read-only in initial version

Decision: 初期版では X MCP の write tool を無効化する。  
Reason: 自律投稿・like・follow などは事故時の影響が大きく、human-in-the-loop が必要なため。

### ADR-006: Start local, design for containers

Decision: MVPは local runner で始めるが、設計は session-based container runner へ移行可能にする。  
Reason: 最初からコンテナ管理を作り込むと重いため。ただしMercari blogの思想であるセッション隔離は将来の中核にする。

### ADR-007: PreToolUse fail-closed, PostToolUse fail-open

Decision: 危険操作を止める hook は fail-closed、計測 hook は fail-open。  
Reason: セキュリティは安全側に倒し、計測は本体動作を妨げないため。

### ADR-008: Python as initial implementation language

Decision: 初期実装言語は Python とする。  
Reason: データ収集、SQLite、金融データ処理、スケジューラー、バッチ実行との相性がよく、まず自律リサーチの縦切りを作るため。Bot/UI統合は後から追加する。

### ADR-009: Config-first minimal platform policy

Decision: role / policy / schedule は `config/roles.yaml`, `config/policy.yaml`, `config/schedules.yaml` に分離する。  
Reason: 設計ドキュメントは1本に保ちつつ、実行時設定は機械可読なYAMLとして管理するため。

### ADR-010: Split LLM credential proxy and tool auth proxy

Decision: Claude API credential boundary を **claude-proxy**、外部tool/API側の認証・認可境界を **auth-proxy** と呼び、別コンポーネントとして扱う。  
Reason: Claude provider credential と、X/J-Quants/Document Store 等の外部API credential を同じ実行面に置かないため。claude-proxy は Claude API への通信中継、provider credential、usage/budget/audit/session attribution を担当する。Claude Code process と workspace はセッションごとの agent-runner に置く。auth-proxy は MCP tool call の認可・監査・rate limit・secret分離を担当する。

### ADR-011: Dedicated public repository for AI/IT generated notes

Decision: X由来のAI/IT topic digestやtopic notesは、agent本体のrepositoryではなく `nishiog/ai-it-research-notes` に保存する。  
Reason: agent system と生成 knowledge を分離し、Git履歴をそのまま knowledge base の更新履歴として扱うため。書き込み権限は document-store / auth-proxy 側に閉じ込め、許可pathを `daily/**`, `weekly/**`, `topics/**`, `queue/**` に限定する。

---


### ADR-012: Implement proxy boundary services in Go

Decision: Implement `claude-proxy` and `auth-proxy` as Go services, while keeping agent orchestration and research logic in Python.

Reason: These proxies are security-sensitive network boundary components. Go is better suited for HTTP reverse proxying, streaming/SSE, concurrency, small static binaries, and container deployment. Python remains better for agent orchestration, data processing, research workflows, and Markdown generation.

### ADR-013: Run Claude Code CLI inside agent-runner via claude-proxy

Decision: agent-runner コンテナに Claude Code CLI(Node.js + `@anthropic-ai/claude-code`)をインストールし、LLM 通信は環境変数(`ANTHROPIC_BASE_URL` → claude-proxy、`ANTHROPIC_AUTH_TOKEN` = session token、`ANTHROPIC_CUSTOM_HEADERS` = `X-7mimi-Session-Id` / `X-7mimi-Role`)で claude-proxy に向ける。claude-proxy は `/v1/messages` 系エンドポイント(`count_tokens` 含む)を pass-through する。container runner の既定は `--network none` のまま維持し、Claude 実行時のみ明示的に bridge ネットワークへ opt-in する。

Reason: ADR-010 の「Claude Code process と workspace は agent-runner に置き、provider credential は claude-proxy に置く」構成を実際に動く形にするため。Claude Code は標準環境変数で base URL / Bearer token / 追加ヘッダを差し替えられるため、コード改変なしで credential boundary を通せる。ネットワークは既定 deny(none)を保ち、必要なジョブだけ opt-in することで isolation の原則を崩さない。

### ADR-014: Rename Python package sevenmimi_agent to shichimimi_agent

Decision: Python パッケージ名を `sevenmimi_agent` から `shichimimi_agent` へ、配布名/console script/argparse prog を `sevenmimi-agent` から `shichimimi-agent` へ統一する。Docker イメージ名(`7mimi-agent-runner` など)、Go サービス(services/)、`X-7mimi-*` ヘッダ、リポジトリ名は 7mimi ブランドのまま変更しない。

Reason: プロジェクト名 7mimi の読みは「しちみみ」(shichi-mimi)であり、Python パッケージだけが英語読み(seven)になっていた命名の不整合を解消するため。7mimi 表記自体はブランドとしてイメージ名・ヘッダ・サービス名に残す。

### ADR-015: x-mcp-readonly を Python 製 MCP プロトコル準拠サーバとして実装

Decision: X API アクセス用の `x-mcp-readonly` は、MCP プロトコル(JSON-RPC 2.0、Streamable HTTP transport: `POST /mcp` で `initialize` / `tools/list` / `tools/call`)に準拠した Python 製サーバとして `src/shichimimi_agent/mcp/` に実装する(stdlib のみ、追加依存なし)。公開 tool は read-only の 4 種(`x.search_posts_recent`, `x.get_posts`, `x.get_users`, `x.get_users_by_username`)に限定し、write 系 tool は実装しない。X API credential(`X_BEARER_TOKEN`)はこのサーバプロセスの環境変数のみに置き、agent-runner・auth-proxy には渡さない。runner からの利用は従来どおり auth-proxy の tool 認可を通過してから行う。

Reason: ADR-012 の Go 化対象は reverse-proxy 型の境界サービス(claude-proxy / auth-proxy)であり、x-mcp-readonly はデータ収集サービスなので Python 側(research/orchestration 領域)に置く。最初から MCP プロトコルに準拠することで、将来 Claude Code / 他 MCP クライアントから直接接続でき、独自 HTTP API からの移行コストを避けられる。credential をサーバ env のみに置くのは ADR-010 の credential 分離原則(X credential は auth-proxy または各 MCP server のみが保持)に従うため。
