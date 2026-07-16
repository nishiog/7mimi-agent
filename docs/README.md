# 7mimi Agent Documentation

Status: Draft v0.2  
Date: 2026-07-04  
Owner: 7milch

7mimi Agent の設計ドキュメント入口です。ドキュメントはテーマごとに分割して管理します。

## Reading order

1. [Overview](overview.md)
   - Vision
   - Mercari Engineering blog から取り込む思想
   - Goals / Non-goals
   - Core principles

2. [Architecture](architecture/README.md)
   - High-level architecture
   - Runtime and container model
   - Roles
   - MCP-first design
   - Security design

3. [Workflows and Outputs](workflows/README.md)
   - Data model
   - Workflows
   - AI/IT Topic Documentation Runner
   - Output templates
   - Scheduler design
   - Metrics and observability

4. [Detailed Design](detailed-design/README.md)
   - Python package structure
   - SQLite schema
   - Orchestrator / Scheduler / Session / Runner
   - claude-proxy / auth-proxy
   - Hooks / MCP integration
   - AI/IT runner and stock research runner
   - Testing strategy / CLI / DoD

5. Planning
   - [Roadmap and project structure](planning/roadmap.md)
   - [Architecture Decision Records](planning/adr.md)
   - [Open questions](planning/open-questions.md)

6. Deployment
   - [k3s + ArgoCD](deployment/k3s-argocd.md)

## Related repositories

- Agent system: `7milch/7mimi-agent`
- Generated AI/IT notes: `7milch/ai-it-research-notes`

## Documentation policy

- 設計は `docs/` 配下でテーマ別に整理する。
- 実行時設定は `config/*.yaml` を正とする。
- ADR は `docs/planning/adr.md` に追記する。
- 実装レベルの詳細は `docs/detailed-design/README.md` に集約する。
- 生成される調査Markdownは `7milch/ai-it-research-notes` に置き、agent本体repoには置かない。
