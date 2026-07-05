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

Decision: X由来のAI/IT topic digestやtopic notesは、agent本体のrepositoryではなく `7milch/ai-it-research-notes` に保存する。  
Reason: agent system と生成 knowledge を分離し、Git履歴をそのまま knowledge base の更新履歴として扱うため。書き込み権限は document-store / auth-proxy 側に閉じ込め、許可pathを `daily/**`, `weekly/**`, `topics/**`, `queue/**` に限定する。

改訂(2026-07-05): GitHub App 運用の都合により、notes repo と agent 本体 repo を `nishiog` から `7milch` 配下へ transfer した(`7milch/ai-it-research-notes` / `7milch/7mimi-agent`)。Go モジュールパスも `github.com/7milch/7mimi-agent/...` に更新。

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

改訂(2026-07-05): ADR-023 により x-mcp-readonly は auth-proxy(Go)へ統合した。本 ADR の Python 実装は撤去済み。

### ADR-016: Model 選択は config 駆動のソフト制御とする

Decision: 実行 model は `config/policy.yaml` の `model_policy.default_model`(既定: `claude-sonnet-5`)と `config/roles.yaml` の role 別 `model:` フィールド(role.model > default_model の優先順)で決定し、agent-runner コンテナへは `ANTHROPIC_MODEL` 環境変数として注入する(Claude Code は標準でこの env を respect する)。claude-proxy での model 強制(拒否・書き換え)は行わない。`model_policy.known_models` にない model 名は config validate 時に warning のみ。claude-smoke は診断用途のため model_policy を経由せず、CLI 既定 `claude-haiku-4-5`(`--model` で上書き可)とする。

Reason: claude-smoke が model 未指定で Claude Code の既定(Opus 系)により想定外のコストが発生した。目的は「意図しない高コスト model の使用防止」であり、明示的な Opus 利用(Skill 等)は許容したいため、proxy でのハード強制ではなく config 駆動のソフト制御を選ぶ。model 名は credential ではないため env allowlist への追加はセキュリティ境界(ADR-010/012/013)に影響しない。

### ADR-017: daily digest の実データ収集は X_MCP_URL による opt-in とする

Decision: `AiItTopicRunner` の収集は環境変数 `X_MCP_URL` が設定されている場合のみ x-mcp-readonly(MCP `tools/call` `x.search_posts_recent`)による実データ収集を行い、未設定時は従来の mock 収集を維持する。digest 項目は LLM を使わず正規化ポストから決定的に構築する(topic=クエリ、engagement 最大のポストを代表シグナルとする、X post は signal であり evidence としない原則を維持)。クエリは最大3件・max_results 10 でコストを抑制する。

Reason: 実 X API は Pay Per Use の課金対象であり、dry-run・テスト・CI をコストゼロで保つには env 駆動の opt-in が適切(AUTH_PROXY_URL と同じパターン)。LLM 要約は claude-proxy 経由の実行基盤が role 実行に接続されてから導入する。

### ADR-018: notes repo への publish はホスト credential による暫定 local 構成とする

Decision: daily digest の `7milch/ai-it-research-notes` への publish は、orchestration ホスト上の DocumentRepositoryWriter が `.data/notes-repo/` の git checkout に対して path policy(`document_repositories` の allow/deny glob)を事前強制した上で commit/push する。credential はホストの ambient git/GitHub 認証を使い、agent-runner コンテナには一切渡さない。CLI は `--publish` の明示 opt-in(既定は dry-run)。将来は document-store MCP + auth-proxy に credential を移す(ADR-010 の最終形)。

Reason: roadmap の「digest を notes repo に push する縦切り」を最小構成で成立させるため。書き込み先制限は LLM の外側の決定的 path policy で担保し、ホスト credential の利用は local 実行(LocalRunnerBackend)に限定することで、コンテナ境界の credential 不在原則(ADR-010/013)を維持する。

廃止(2026-07-05): git relay の E2E 成功に伴い、本経路(--publish / runner からの writer.publish 呼び出し)は廃止した(ADR-020)。path policy 検証は security/path_policy.py として引き続き有効。

### ADR-019: シグナル要約 LLM は claude-proxy 経由・フォールバック必須の opt-in とする

Decision: 収集した X シグナルの要約(what_happened / why_it_matters)は、`CLAUDE_PROXY_URL` と `CLAUDE_PROXY_SESSION_TOKEN` が設定されている場合のみ claude-proxy 経由の `/v1/messages`(非ストリーミング、model は ADR-016 の resolve_model、max_tokens 400)で生成する。LLM 呼び出しは tool `claude.summarize_signals` として PreToolUse 認可を通し、deny・proxy 障害・応答パース失敗のいずれでも決定的構築(ADR-017)へフォールバックして digest 生成を継続する。ポスト本文は信頼できないデータとして system prompt で指示追従を禁止し、出力は JSON 強制+検証する。LLM 出力も signal の要約であり evidence とはしない。

Reason: provider credential を持たない Python orchestration から LLM を使う唯一の経路は claude-proxy であり(ADR-010/012)、要約は digest の品質向上であって可用性要件ではないためフォールバック必須の opt-in が適切。prompt injection は X 由来テキストの主要リスクであり、決定的な認可(hook)と出力形式検証を LLM の外側に置く方針(security boundary 原則)を維持する。

### ADR-020: Git Smart HTTP relay を auth-proxy に実装し、GitHub App 短命 token で書き込みを一本化する

Decision: agent-runner からの git 操作は auth-proxy の `/git/{owner}/{repo}` smart HTTP 透過中継(internal/gitrelay)経由のみとする。runner にはセッション Bearer を `GIT_CONFIG_*` env(URL-scoped `http.<relay>.extraheader`)で注入し、relay は `AUTH_PROXY_SESSION_TOKEN`(未設定時は relay 自体を無効化、デフォルト値なし・fail-closed)と定数時間比較で検証する。GitHub へは GitHub App「7mimi-agent」の installation access token(TTL 1h、残5分で再発行)を `Basic x-access-token:` 形式で注入する。private key はホストのファイル(`GITHUB_APP_PRIVATE_KEY_PATH`、実体は `SHICHIMIMI_AGENT_X_GITHUB_APP_PRIVATE_KEY` が指すパス)として auth-proxy のみが参照する。repo 制限は App の installation 対象(現在 `7milch/ai-it-research-notes` のみ、当面手動管理・将来 `7milch/terraform` で IaC 化)による credential scope で強制し、proxy 側の repo×操作 ACL は複数 role 要件が出るまで実装しない。policy.yaml の `git\s+push` deny パターンは削除する(runner は credential を持たず relay 以外では push 不能のため、書き込み制御点は relay+credential scope に一本化)。runner コンテナ内 git の E2E が成功した時点で ADR-018 のホスト publish 経路(`--publish`)は廃止する。

Reason: ADR-018 は自ら暫定と宣言しており、ホスト credential 依存を解消して「credential-free runner」(ADR-010)を git 書き込みまで拡張するため。強制点を proxy の判定ロジックではなく短命 token の scope に置くのは Mercari pcp-agent と同方式で、判定コードの増殖を避けつつ機械的な制限を維持できる。

### ADR-021: 自律 digest 統合ジョブ(claude-digest)の実行形態

Decision: daily digest の執筆と公開は、agent-runner コンテナ内の Claude Code が行う統合ジョブ `claude-digest` とする。orchestrator が hook 認可付きで X シグナルを事前収集して session workspace の `signals.json` に置き、コンテナ内 Claude は Read/Write/WebFetch/Bash(git:*) のみ許可された状態で、3〜5 トピックの選定・WebFetch による一次情報確認・日本語での執筆(構成は自由、引用は原文可)・git relay 経由の main への push までを自律実行する。不変条件(daily/YYYY/MM/<date>.md への保存、X は signal であり evidence は一次情報のみ、投資助言禁止、本文の大量転載禁止)は prompt で指示し、orchestrator が push 後の clone-back 検証(ファイル存在+日本語含有)で確認する。LLM 通信は claude-proxy、push は git relay 経由で、コンテナは引き続き provider/GitHub credential を一切保持しない。bridge ネットワークにより WebFetch の egress は現状無制限であり、Mercari 方式の DNAT による proxy 強制は将来課題として残す。収集は per-query 耐障害とし、個別クエリの失敗はスキップして failed_queries として記録、全クエリ合計 0 件の場合のみ失敗とする(認可 deny は即時中断)。また、Bash(git:*) の allowlist は git の -c/alias 等により厳密な exec 制限にはならないため、コンテナ内の残存リスク(セッション token の egress 経由持ち出し)は bridge egress 無制限の課題と併せて認識し、DNAT による egress 強制を将来対応とする。

Reason: 収集(x-mcp-readonly)・LLM 境界(claude-proxy)・書き込み境界(git relay)の全部品が credential 分離済みで揃ったため、これらを組み合わせた「調査から公開まで」の自律縦切りを最小構成で成立させる。事前収集を orchestrator 側に置くのは、X API 呼び出しを決定的な認可・監査の下に保ち、コンテナ内 LLM の tool 面を最小化するため。Claude Code から MCP 直結への移行は後続の検討事項とする。

### ADR-022: cron scheduler は単一プロセス・逐次実行の常駐ループとする

Decision: `schedule run` は Python 単一プロセスの常駐ループとして実装する(stdlib のみ、分単位精度、Asia/Tokyo 固定)。ジョブは逐次実行し、`concurrency_policy: forbid` は同一分内の二重発火防止として実装する(単一スレッド逐次実行のため実行の重複はそもそも発生しない)。`active_deadline_seconds` はワーカースレッド + join(timeout) による打ち切り、`backoff_limit` は即時リトライ回数として解釈する。実行結果の DB 記録は executor(実行本体)側の責務とし、スケジューラは発火・リトライ・結果返却のみを行う。executor が登録されたジョブのみ実行し(現時点は `ai-it-x-daily-digest` → claude-digest パイプラインのみ)、未実装 role のジョブは skip として記録する。デーモン化(launchd/systemd)は行わず、プロセス管理はホスト側の責務とする。

Reason: Phase 3(scheduled autonomy)の最小構成として、外部依存なしで cron 定義(config/schedules.yaml)を実行に移すため。ジョブ数が少なく実行時間も分オーダーのため、並列実行・分散実行は現時点で不要。スケジューラの責務を「発火とリトライと記録」に限定し、実行本体は既存の runner 経路(認可・監査・credential 分離済み)に委ねる。

### ADR-023: x-mcp-readonly を auth-proxy に統合し X credential を Go 境界に集約する

Decision: ADR-015 の Python 製 x-mcp-readonly サーバを撤去し、同一の MCP プロトコル契約(JSON-RPC 2.0、`POST /mcp`、read-only 4 tool、21.13.1 正規化、redaction、token 非漏洩)を auth-proxy(Go)の `internal/xmcp` として再実装する。`X_BEARER_TOKEN` は auth-proxy のみが保持し(未設定時は /mcp を mount しない)、X_MCP_URL は auth-proxy(:18081)を指す。Python 側は MCP クライアント(`mcp/client.py`)のみを維持する。/mcp は gitrelay と同一のセッション Bearer(AUTH_PROXY_SESSION_TOKEN、定数時間比較)で保護し、X_BEARER_TOKEN とセッション token の両方が設定された場合のみ mount する。クライアントは X_MCP_SESSION_TOKEN で同 token を送出する。

Reason: credential 保有者を Go 境界サービス(auth-proxy: tool 認可 + git relay + X API)に集約し、監査の一本化と常駐プロセス削減(4→3)を得るため。ADR-015 時点では「データ収集は Python」の整理だったが、実装後の運用で credential 分散と常駐プロセス数の方が支配的な関心事になったため方針を改訂する。正規化・redaction のロジックは小さく、Go 移植のコストより集約の利得が上回ると判断した。

### ADR-024: 常駐化は docker compose によるサイドカー構成とする

Decision: claude-proxy・auth-proxy・scheduler(`schedule run`)の常駐は単一の `docker-compose.yml` で管理する(restart: unless-stopped、healthcheck 付き)。scheduler コンテナは `/var/run/docker.sock` をマウントし、agent-runner を sibling コンテナとしてホストの Docker daemon で起動する。このためリポジトリはホストと同一絶対パスで scheduler コンテナにマウントし、セッション workspace の `-v` パス整合を保つ。secrets は gitignored な `.env`(env_file)と read-only の pem マウントで注入し、イメージには焼き込まない。proxy 類は 18080/18081 をホストに公開し、runner/scheduler からは `host.docker.internal` で到達する。proxy の 18080/18081 は全インターフェースに公開する(sibling runner が host-gateway 経由で到達するため loopback bind は不可)。LAN 内の第三者アクセスはセッション Bearer のみで防御されるため、信頼できないネットワークではホスト側 firewall で遮断する運用とする。

Reason: 毎朝の自律 digest(ADR-021/022)を人手なしで回すため、プロセス管理を Docker の restart/healthcheck に委ねる。docker.sock マウントは実質ホスト root 相当の権限だが、scheduler イメージは自前ビルド・自前コードのみで外部入力を実行しないため、launchd 複数管理より単純さを優先する。egress の DNAT 強制(#17)は本構成の上に重ねる。

### ADR-025: agent-runner の egress は internal ネットワーク + egress-proxy で強制する

Decision: compose 環境では agent-runner を `internal: true` の Docker ネットワーク(7mimi-internal)のみに接続し、外部への直接到達を遮断する。runner から見える経路は claude-proxy(LLM)、auth-proxy(tool 認可・git relay・x-mcp)、egress-proxy(WebFetch 用 forward proxy)の 3 つに限定する。egress-proxy は自前 Go 実装の CONNECT/HTTP forward proxy で、解決済み IP に対して RFC1918・loopback・link-local・ULA を拒否し(検証済み IP へ直接 dial して DNS rebinding を防ぐ)、443/80 以外のポートと `api.anthropic.com` への直行(claude-proxy 迂回)を拒否し、metadata のみを監査ログに残す。`EGRESS_ALLOW_HOSTS` によるドメイン allowlist 化を後日の絞り込みとして残す。ローカル dev(compose なし)では従来の bridge + host.docker.internal 構成を維持する。

Reason: ADR-021 で認識した「bridge egress 無制限」の残存リスクに対し、macOS Docker Desktop では iptables/DNAT を直接制御できないため、Mercari 方式(ネットワーク層強制)を Docker ネイティブの internal ネットワーク+単一出口 proxy に翻訳して実現する。egress-proxy を自前 Go 実装とするのは、第三者 proxy イメージの supply chain リスクを避け、既存の Go 境界サービス群と同じ監査・テスト規律に載せるため。WebFetch の一次情報確認は広範な公開 Web を必要とするため、MVP では public IP 宛 80/443 を許可しつつ、内部網・メタデータサービス・provider API 直行を機械的に遮断することを優先する。

### ADR-026: 投資クラスタ digest は Slack 通知とし、Slack credential は auth-proxy に置く

Decision: 投資クラスタ(日米株・暗号資産・マクロ)の daily digest(job `invest-x-daily-digest`、role `investment_signal_runner`)は notes repo への push ではなく Slack 通知を出力先とする。Slack への送信は auth-proxy の `POST /v1/slack/notify` を経由し、auth-proxy が Slack App の bot token(SLACK_BOT_TOKEN、chat.postMessage、投稿先は SLACK_CHANNEL_ID)を単独保持する。(セッション Bearer 必須、行境界で ≤3500 字に分割、metadata のみ監査)runner コンテナには Slack への経路も git relay も与えない(allowedTools は Read/Write/WebFetch のみ)。投資助言禁止の免責フッターは LLM に依存せず orchestrator が送信直前に決定的に付加する。暗号資産の項目は既定で「未確認シグナル」ラベルとし、公式一次情報を WebFetch 確認できた場合のみ verified と表記する。既知の制限: auth-proxy の Go DevEngine(埋め込み dev policy)は `ai_it_topic_runner` のみを定義しており、`AUTH_PROXY_URL` を scheduler に配線する場合は policy.yaml との整合(investment_signal_runner の追加)が必要である。

Reason: 投資シグナルは鮮度が価値であり、push 型の Slack が適切。Slack bot token は secret を含む credential であるため ADR-010/012 に従い Go 境界サービスに置き、LLM コンテナから分離する。助言禁止(anti-goal)の guardrail は push チャネルでは知覚リスクが上がるため、prompt 依存ではなく決定的なプラットフォーム層(footer 付加・digest 構造の検証)に置く。当初は Incoming Webhook を予定したが、将来メンション受信(Events/Socket Mode)へ拡張するため Slack App の bot token 方式に改めた(2026-07-05 改訂)。
