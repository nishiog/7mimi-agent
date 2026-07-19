# k3s + ArgoCD Deployment

Status: Draft v0.3  
Date: 2026-07-19  
Owner: 7milch

7mimi Agent の本番運用(k3s + ArgoCD、ADR-031 / ADR-032)の構築・運用手順をまとめる。docker-compose(ADR-024)は local/dev 用として別途 `docker-compose.yml` を参照すること。

## 前提

- k3s v1.36+ シングルノード(`john-cooper-works`)、ArgoCD 導入済み、StorageClass `local-path`(default)
- ノードに Docker daemon は不要(イメージは GitHub Actions が ghcr.io に push する。ADR-031 の通り、対象ノードは containerd のみで sibling コンテナ起動は成立しない)

## ArgoCD 側の必須設定

`deploy/k8s/kustomization.yaml` の `configMapGenerator` はリポジトリルートの `config/*.yaml` を参照しており、これは kustomization root(`deploy/k8s/`)の外側にあるファイルの参照にあたる。ArgoCD の repo-server がデフォルトの load restrictor でこれを拒否するため、`argocd-cm` ConfigMap に以下が設定されている必要がある。

```yaml
kustomize.buildOptions: "--load-restrictor LoadRestrictionsNone"
```

この設定は ADR-035 により、クラスタ管理側リポジトリ(private、app-of-apps 構成)の ArgoCD bootstrap kustomization が argocd-cm への patch として GitOps 管理しており、手動 `kubectl patch` は不要(クラスタ再構築時も ArgoCD 自己管理の sync で再現される)。これが無いと 7mimi-agent Application が sync できない(`kustomize build` がエラーになる)。

## Secret 一覧と投入手順(runbook)

Secret の実体は Git に一切置かない。`deploy/k8s/` の各マニフェストは Secret 名のみを参照し、初回に運用者が out-of-band で `kubectl` により投入する。

以下は `deploy/k8s/*.yaml` の `secretKeyRef` / secret volume を実際に読み取った一覧。

| Secret 名 | キー | 帰属サービス | 必須・任意 |
|---|---|---|---|
| `7mimi-agent-secrets` | `ANTHROPIC_API_KEY` | claude-proxy | 必須 |
| `7mimi-agent-secrets` | `AUTH_PROXY_SESSION_TOKEN` | auth-proxy(自身の検証用)/ scheduler(`X_MCP_SESSION_TOKEN` / `GIT_PROXY_SESSION_TOKEN` / `AUTH_PROXY_SESSION_TOKEN` / `SLACK_NOTIFY_SESSION_TOKEN` として再利用) | 必須 |
| `7mimi-agent-secrets` | `CLAUDE_PROXY_SESSION_TOKEN` | scheduler | 必須 |
| `7mimi-agent-secrets` | `GITHUB_APP_ID` | auth-proxy(git relay、ADR-020) | 必須 |
| `7mimi-agent-secrets` | `GITHUB_APP_INSTALLATION_ID` | auth-proxy | 任意 |
| `7mimi-agent-secrets` | `X_BEARER_TOKEN` | auth-proxy(x-mcp、ADR-023) | 必須 |
| `7mimi-agent-secrets` | `SLACK_BOT_TOKEN` | auth-proxy(Slack 通知、ADR-026) | 任意(未設定時は `/v1/slack/notify` が unmount され invest digest の Slack publish のみ機能しない) |
| `7mimi-agent-secrets` | `SLACK_CHANNEL_ID` | auth-proxy | 任意(`SLACK_BOT_TOKEN` と同様) |
| `7mimi-agent-secrets` | `SLACK_SYSLOG_CHANNEL_ID` | auth-proxy(scheduler ジョブ成否の syslog 通知、ADR-034) | 任意(未設定時は syslog 通知のみ無効化され、digest 配送・ジョブ実行には影響しない) |
| `7mimi-agent-secrets` | `JQUANTS_REFRESH_TOKEN` | auth-proxy(jq.* evidence tools、ADR-027) | 任意(未設定時は jq.* tools が unmount され `research stock` の J-Quants evidence 取得のみ機能しない) |
| `github-app-key` | `github-app-key.pem` | auth-proxy(`GITHUB_APP_PRIVATE_KEY_PATH=/secrets/github-app-key.pem` として volume mount) | 必須 |
| `ghcr-pull-secret` | `.dockerconfigjson` | 全 Deployment / scheduler ServiceAccount の `imagePullSecrets`(pull 用) | 任意(ghcr イメージが public の間は不要 — imagePullSecrets の参照先が無くても匿名 pull にフォールバックする。private 化する場合は必須) |

注: `JQUANTS_REFRESH_TOKEN` は issue #33 で配線済み(auth-proxy.yaml の optional secretKeyRef)。キー未投入でも他機能には影響しない。

### 投入例

```bash
kubectl create secret generic 7mimi-agent-secrets \
  --namespace 7mimi-agent \
  --from-literal=ANTHROPIC_API_KEY='<value>' \
  --from-literal=AUTH_PROXY_SESSION_TOKEN='<value>' \
  --from-literal=CLAUDE_PROXY_SESSION_TOKEN='<value>' \
  --from-literal=GITHUB_APP_ID='<value>' \
  --from-literal=GITHUB_APP_INSTALLATION_ID='<value>' \
  --from-literal=X_BEARER_TOKEN='<value>' \
  --from-literal=SLACK_BOT_TOKEN='<value>' \
  --from-literal=SLACK_CHANNEL_ID='<value>'

kubectl create secret generic github-app-key \
  --namespace 7mimi-agent \
  --from-file=github-app-key.pem=/path/to/7mimi-agent.private-key.pem

kubectl create secret docker-registry ghcr-pull-secret \
  --namespace 7mimi-agent \
  --docker-server=ghcr.io \
  --docker-username='<github-username>' \
  --docker-password='<ghcr PAT, read:packages>' \
  --docker-email='<email>'
```

`GITHUB_APP_INSTALLATION_ID` / `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID` は `optional: true` の secretKeyRef なので、値を持たない場合は該当行を省いてよい(auth-proxy 側の該当機能のみ無効化される)。

**投入前の非空チェック(必須)**: `.env` から値を流し込む場合、対象キーが空文字でないことを必ず確認すること(例: `. ./.env; echo ${#AUTH_PROXY_SESSION_TOKEN}`)。空値はハッシュ照合による突合をすり抜け、auth-proxy は該当エンドポイントを mount しない・claude-proxy は既定トークンにフォールバックするなど、原因の分かりにくい機能不全になる(issue #30 で実例あり)。

注記: ghcr への **push** credential は GitHub Actions 側(ワークフローの `GITHUB_TOKEN`、`packages: write` 権限)であり、k8s 側の `ghcr-pull-secret`(pull 用の Personal Access Token または同等の read:packages 権限を持つトークン)とは別管理である。

## イメージ供給と更新フロー

- main への push で `.github/workflows/build-images.yaml` が 5 イメージ(scheduler / agent-runner / claude-proxy / auth-proxy / egress-proxy)を `ghcr.io/7milch/7mimi-agent-*:sha-<短縮12桁SHA>` に push する。`:latest` タグは使わない。
- 反映は自動書き換えではなく、`deploy/k8s/kustomization.yaml` の `images[].newTag` と `deploy/k8s/scheduler.yaml` の `RUNNER_IMAGE` env をレビュー可能な PR として更新する(2箇所を同じ SHA に揃えること)。
- PR merge 後、ArgoCD が Application(`deploy/argocd/application.yaml`)の sync により反映する(`selfHeal: true` / `prune: false`)。GitHub Actions は build/push のみを担当し、deploy(CD)は行わない。

## デプロイ手順

1. Secret 投入(上記 runbook)
2. クラスタ管理側リポジトリ(private)の app-of-apps に 7mimi-agent の Application 定義が含まれており、ArgoCD ブートストラップ後に root Application 経由で自動登録される(ADR-035)。`deploy/argocd/application.yaml` は参照用サンプルであり、緊急ブートストラップ時のみ `kubectl apply -f deploy/argocd/application.yaml` で手動登録できる(spec はクラスタ管理側と同一に保つこと)
3. ArgoCD UI / `argocd app get 7mimi-agent` で sync 状態を確認する

注: ArgoCD 本体とクラスタ管理側リポジトリの credential のブートストラップは前提であり、Secret 群は本 runbook のとおり out-of-band 投入が必要(ゼロタッチ DR ではない)。

## 受け入れ検証(必須)

### egress 遮断の実機検証

runner Job Pod と同じ label(`app.kubernetes.io/name: 7mimi-agent-runner`)を付けたテスト Pod を立て、NetworkPolicy(`deploy/k8s/networkpolicy.yaml`)が意図通り強制されていることを確認する。

```bash
kubectl run netpol-test \
  --namespace 7mimi-agent \
  --labels="app.kubernetes.io/name=7mimi-agent-runner" \
  --image=curlimages/curl --rm -it --restart=Never -- sh
```

Pod 内から:

- (a) RFC1918 宛(例: `curl -m 3 http://10.0.0.1/`)が到達不可であること
- (b) `api.anthropic.com` への直行(例: `curl -m 3 https://api.anthropic.com/`)が到達不可であること
- (c) 任意の public host:443(例: `curl -m 3 https://example.com/`)が到達不可であること
- claude-proxy(18080)/ auth-proxy(18081)/ egress-proxy(18082)と kube-dns(53)のみ到達できること

k3s は `--disable-network-policy` が設定されていないこと(組み込み kube-router netpol controller が有効であること)を事前に確認しておく(ADR-032)。

### スモーク

```bash
PYTHONPATH=src python3 -m shichimimi_agent run-job ai-it-x-daily-digest --dry-run --runner kubernetes
```

## 制約・既知の限界

- `local-path` PVC はノードの hostPath 実体でバックアップ無し。SQLite・セッション成果物の耐久性はノードディスクに依存する。
- scheduler は `TZ=Asia/Tokyo` 前提(ADR-022)。
- `nodeSelector` でノードを固定している(`local-path` のノードアフィニティに合わせるため)。
- digest(`claude_digest` / `invest_digest`)は `RUNNER_BACKEND=kubernetes` のとき k8s Job として Claude CLI を実行する(ADR-033、issue #31)。invest digest の k3s 実機での Slack 配送検証は未実施(Slack credential 設定後に別途確認)。
