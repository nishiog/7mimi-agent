# CLAUDE.md と .claude/ を読む — AIエージェントに開発を委ねる仕組みの解説

本書は、7mimi-agent リポジトリの `CLAUDE.md` と `.claude/` ディレクトリを題材に、「AIコーディングエージェント(Claude Code)に開発作業そのものを委ねる」仕組みがどのように構成されているかを解説するものである。Claude Code の設定ファイルを触ったことのない読者を想定し、実際のファイルを引用しながら、一つずつ意味を確認していく。口語的な読み物ではなく、順を追って理解を積み上げる教科書として記述する。

## 第1章 準備 — なぜ設定でAIを制御するのか

### 1.1 Claude Code とは何か

Claude Code は、Anthropic が提供するコマンドライン型のAIコーディングエージェントである。人間が自然言語で指示を与えると、ファイルの読み書き、コマンドの実行、テストの実施といった作業を、エージェントが自ら判断して進める。

ここで問題になるのが、エージェントの振る舞いをどう方向づけるかである。毎回すべてを口頭で指示するのは非効率であり、指示のばらつきも生む。そこで Claude Code は、リポジトリの中に置かれた**設定ファイル**を読み込み、その内容に従って振る舞う。設定ファイルは大きく2種類ある。

- **`CLAUDE.md`**: プロジェクトの決まりごとを自然言語で記述する指示書。エージェントは作業前にこれを読む。
- **`.claude/` ディレクトリ**: スキル(定型ワークフロー)、サブエージェント(専門役割)、フック(自動介入)、設定を格納する。

本書はこの2つを順に読み解く。

### 1.2 本書で使う用語

以下の用語を先に定義する。いずれも後の章で実例とともに再確認する。

- **スキル(skill)**: 名前を付けて呼び出せる定型ワークフロー。`/next-task` のようにスラッシュコマンドとして起動する。
- **サブエージェント(subagent)**: 特定の役割に特化した別のエージェント。メインのエージェント(オーケストレーター)が仕事を委譲する相手。
- **フック(hook)**: エージェントの動作の特定の瞬間に自動で割り込むプログラム。本書では「作業を完了しようとした瞬間」に走るフックを扱う。
- **オーケストレーター**: 人間と会話し、全体を統括するメインのエージェント。サブエージェントに作業を割り振り、結果を束ねる。
- **フロントマター(front matter)**: Markdownファイル冒頭の `---` で囲まれた領域。ファイルの設定を機械可読な形で記述する。
- **ADR(Architecture Decision Record)**: アーキテクチャ上の決定を記録した文書。「なぜこう決めたか」を残す。

---

## 第2章 CLAUDE.md — プロジェクトの憲法

### 2.1 CLAUDE.md の位置づけ

`CLAUDE.md` は、リポジトリのルートに置かれる、エージェント向けの指示書である。Claude Code はセッション開始時にこれを読み込み、記述された決まりごとを作業全体に適用する。人間の新規参加者に渡す「開発ガイド」に相当するが、読者が人間ではなくAIである点が異なる。

7mimi-agent の `CLAUDE.md` は約130行で、次の要素で構成される。

- リポジトリの共通コマンド(テストの実行方法、設定検証の方法など)
- アーキテクチャの要点(ディレクトリ構造、設計の中心原則)
- 仕様駆動開発のルール
- ADR 記録の規律
- スキルとサブエージェントの使い方、委譲のルール

このうち、本書のテーマである「AIに開発を委ねる仕組み」に直結するのが、後半の3つ — 仕様駆動開発、ADR規律、委譲ルール — である。以下ではこれらを詳しく読む。

### 2.2 仕様駆動開発のルール

`CLAUDE.md` は、実装の進め方について次のように定める(原文を引用する)。

```text
Docs under `docs/` are the spec; implement according to them, not ad hoc.
Before implementing, check the relevant sections of `docs/architecture/`,
`docs/detailed-design/`, `docs/workflows/` and the latest ADRs in
`docs/planning/adr.md`. If a recent ADR contradicts the older design docs,
**update the design docs first** to reflect the ADR, then implement against
the updated docs.
```

要点は3つである。

第一に、**`docs/` 配下の設計文書が「仕様」であり、その場の思いつき(ad hoc)で実装してはならない**。エージェントは実装前に、関連する設計文書と最新のADRを確認する義務を負う。

第二に、**最新のADRが古い設計文書と矛盾する場合は、先に設計文書をADRに合わせて更新してから実装する**。これは「コードと文書の乖離」を防ぐための順序の指定である。矛盾を放置したまま実装すると、次にその文書を読むエージェントが古い前提で動いてしまう。

この規律により、エージェントの実装は「毎回ゼロから判断する」のではなく、「文書化された決定の上に積み上げる」ものになる。判断のばらつきが抑えられ、複数のセッションをまたいでも一貫性が保たれる。

### 2.3 ADR記録の規律

`CLAUDE.md` は、設計上の決定を必ずADRとして記録することを求める。

```text
Any change that alters architecture, security boundaries, language/tooling
choices, or platform policy **must** be recorded as an ADR in
`docs/planning/adr.md` **in the same work session** (append-only, numbered
sequentially). This applies to changes under `docs/architecture/`,
`docs/detailed-design/`, `docs/workflows/`, and `config/*.yaml`. A Stop hook
(`.claude/hooks/adr-check.sh`) blocks completion when those paths changed
without an `adr.md` update.
```

ここで注目すべきは、この規律が**単なるお願いではなく、フックによって機械的に強制される**点である。文中に「A Stop hook ... blocks completion(Stopフックが完了をブロックする)」とある。設計に関わるファイル(`docs/architecture/` など、`config/*.yaml`)を変更したのにADRを更新していなければ、エージェントは作業を完了できない。このフックの中身は第5章で読む。

ADRは追記のみ(append-only)で、番号は連番で振る。過去の決定を書き換えるのではなく、新しい決定を末尾に足していく。これにより「いつ・なぜ・何を決めたか」の履歴がそのまま残る。第2.2節の仕様駆動開発と合わせると、「決定はADRに残し、実装は文書に従う」という循環が成立する。

### 2.4 委譲ルール — 誰が何をするか

`CLAUDE.md` は、開発作業を複数の役割に分担する方法を定める。

```text
- **Implementation loop**: implementer → tester → reviewer. Repeat until the
  tester returns `SUCCESS` **and** the reviewer returns `APPROVE`. Any
  `SPEC-ISSUE` verdict stops the loop and escalates to the user.
- **Doc updates go through doc-updater**: the orchestrator decides the exact
  content first, then hands doc-updater concrete instructions. doc-updater
  records decisions; it never makes them.
- Subagents must not spawn further subagents; only the orchestrator delegates.
```

3つのルールが述べられている。

第一の**実装ループ**は本書の中心概念である。実装は「implementer(実装)→ tester(テスト)→ reviewer(レビュー)」の順で進み、テスターが `SUCCESS` を、レビュアーが `APPROVE` を返すまで繰り返す。どちらかが仕様の欠陥(`SPEC-ISSUE`)を報告したら、ループを止めて人間に判断を仰ぐ。これらの役割は第3章で読むサブエージェントである。

第二の**文書更新は doc-updater を通す**。ただし重要な但し書きがある。「オーケストレーターが正確な内容を先に決め、doc-updater には具体的な指示を渡す。doc-updater は決定を記録するのであって、決定を下さない」。文書を書く役割と、内容を決める役割を分離している。

第三に、**サブエージェントはさらにサブエージェントを生成できない**。委譲できるのはオーケストレーターだけである。これは制御の階層を平坦に保ち、誰が誰に指示したかを追いやすくするための制約である。

この委譲構造を図にすると、次の関係になる。

- オーケストレーター(人間と会話)が全体を統括する
- 実装は implementer → tester → reviewer のループで進む
- スコープや技術判断が要る局面では product-manager / tech-lead に相談する
- 文書・ADRの記録は doc-updater が担う

次章では、これらのサブエージェント一つ一つの定義を読む。

---

## 第3章 .claude/agents/ — 専門役割のサブエージェント

### 3.1 サブエージェントの定義形式

`.claude/agents/` には、6つのサブエージェントがそれぞれ1つのMarkdownファイルとして定義されている。ファイルは「フロントマター(設定)」と「本文(役割の指示)」で構成される。まず implementer の定義を見る。

```text
---
name: implementer
description: Implements code based on specifications.
model: sonnet
disallowedTools: Agent
---
You are a Software Engineer responsible for implementation (Implementer Agent).
Based on the provided specification (Spec) or task, implement clean and highly
maintainable code.
```

`---` で囲まれた領域がフロントマターである。各項目の意味は次のとおりである。

- `name`: エージェントの名前。オーケストレーターがこの名前で呼び出す。
- `description`: 役割の要約。どの場面でこのエージェントを使うかの判断材料になる。
- `model`: このエージェントが使う言語モデル。implementer は `sonnet` を使う。
- `disallowedTools: Agent`: このエージェントが使えないツールの指定。`Agent`(サブエージェントの起動)を禁じている。これが第2.4節の「サブエージェントはさらにサブエージェントを生成できない」を機械的に実現している。

フロントマターの下の本文が、そのエージェントに与えられる役割の指示である。implementer の本文は「仕様に基づいてクリーンで保守性の高いコードを実装せよ」と述べる。

### 3.2 モデルの使い分け

6つのエージェントは、役割の性質に応じてモデルを使い分けている。フロントマターの `model` を並べると次のようになる。

| エージェント | モデル | 役割 |
|---|---|---|
| implementer | sonnet | 仕様に基づくコード実装 |
| tester | sonnet | テストの作成・実行 |
| doc-updater | sonnet | 文書・ADRの記録 |
| reviewer | opus | 品質・セキュリティ・アーキテクチャのレビュー |
| tech-lead | opus | アーキテクチャ・技術選定の判断 |
| product-manager | opus | スコープ・ユーザー価値の判断 |

原則が読み取れる。**手を動かす作業(実装・テスト・記録)は sonnet、判断を要する作業(レビュー・技術判断・スコープ判断)は opus** を割り当てている。判断の質が結果を左右する役割ほど、能力の高いモデルを充てる設計である。これはコストと品質の釣り合いを取るための配分でもある。

### 3.3 判定を返す役割 — tester と reviewer

実装ループ(第2.4節)を成立させているのが、tester と reviewer が返す明確な**判定(verdict)**である。tester の本文は、テスト実行後に定型の判定を返すよう指示されている。

- `[TEST-EXECUTION]: SUCCESS` — テスト成功
- `[TEST-EXECUTION]: FAIL` — テスト失敗(implementer に修正を戻す)
- `[TEST-EXECUTION]: SPEC-ISSUE` — 仕様自体の問題(ループを止めて人間へ)

reviewer も同様に定型の判定を返す。

- `[CODE-REVIEW]: APPROVE` — 承認
- `[CODE-REVIEW]: CONCERNS` — 懸念あり(修正を戻す)
- `[CODE-REVIEW]: REJECT` — 却下
- `[CODE-REVIEW]: SPEC-ISSUE` — 仕様の問題

判定が定型の文字列であることが重要である。オーケストレーターはこの文字列を機械的に読み取り、「SUCCESS かつ APPROVE ならループを抜ける」「FAIL なら implementer を再度呼ぶ」といった制御を確実に行える。人間の曖昧な言い回しではなく、決まった記号で結果を返すことで、ループの進行を自動化している。

reviewer のフロントマターには、他のエージェントにない特徴がある。

```text
---
name: reviewer
description: Reviews code and architecture for quality, security, and best practices.
model: opus
tools: Read, Grep, Glob, Bash
---
```

`tools` に `Bash` が含まれる。reviewer は Bash を使えるので、実際にテストを走らせたり、コードを検索したりして、机上ではなく実行に基づいたレビューができる。一方 implementer や tester のような書き込みを伴う役割とは、使えるツールの範囲が意図的に異なっている。

### 3.4 判断を担う役割 — product-manager と tech-lead

新機能を作る局面では、実装に入る前に「作るべきか」「実現可能か」を判断する必要がある。これを担うのが product-manager と tech-lead である。product-manager の定義を見る。

```text
---
name: product-manager
description: "The Product Manager manages all product concerns: MVP scope,
user value, risk management, and roadmap tracking. Use this agent when scope
needs to be evaluated, prioritized, or validated against user needs."
tools: Read, Grep, Glob, WebSearch
disallowedTools: Agent
model: opus
---
```

product-manager が使えるツールは `Read, Grep, Glob, WebSearch` に限られる。**ファイルの書き込みができない**。これは意図的である。この役割の仕事は「判断」であって「実装」ではない。読む・調べることはできるが、コードを変えることはできない。役割とツール権限が一致している。

product-manager と tech-lead は、それぞれ `[PM-SCOPE]: APPROVE` と `[TL-FEASIBILITY]: APPROVE` という判定を返す。tester / reviewer と同じく、定型の判定でループの進行を制御する。新機能のワークフロー(第4.3節)では、この2つの承認を得てから実装に入る。

---

## 第4章 .claude/skills/ — 定型ワークフロー

### 4.1 スキルの定義形式

`.claude/skills/` には3つのスキルがあり、それぞれ1つのディレクトリと `SKILL.md` を持つ。スキルは「名前を付けて呼び出せる定型ワークフロー」である。`next-task` の定義冒頭を見る。

```text
---
name: next-task
description: Trigger this skill when the user asks "what should we do next",
"resume work", "check current status", or asks for the next task to implement.
argument-hint: "[optional context or focus area]"
user-invocable: true
allowed-tools: Read, Grep, Glob, Bash, WebSearch, Agent(doc-updater, implementer, tester, reviewer), AskUserQuestion
disallowed-tools: Write, Edit, Replace
model: opus
---
# Next Task Orchestration Workflow (next-task)
```

フロントマターの項目を読む。

- `name`: スキル名。ユーザーは `/next-task` として呼び出す。
- `description`: 発火条件。「次に何をすべきか」「作業を再開したい」といった要求で起動する。
- `argument-hint`: 呼び出し時に渡せる引数の説明。
- `user-invocable: true`: 人間がスラッシュコマンドで直接呼べることを示す。
- `allowed-tools`: このスキルが使えるツール。注目すべきは `Agent(doc-updater, implementer, tester, reviewer)` である。next-task は**この4つのサブエージェントを呼び出す権限を持つ**。つまり next-task はオーケストレーターとして実装ループを回せる。
- `disallowed-tools: Write, Edit, Replace`: **ファイルの書き込みができない**。next-task 自身はコードを書かず、実装は implementer に委譲する。役割の分離がここでも徹底されている。
- `model: opus`: 統括役なので判断力の高いモデルを使う。

`allowed-tools` と `disallowed-tools` の組み合わせが、そのスキルの権限を精密に定義している。next-task は「サブエージェントを呼べるが自分では書き込めない」統括専用スキルである。

### 4.2 next-task — 作業の再開と継続

`SKILL.md` の本文は、スキルが起動されたときにエージェントが従うべき手順を段階的に記述する。next-task は7つのステップを持つ。

1. 状況分析(`docs/planning/` と未完了のIssue、git履歴を確認)
2. 次タスクの候補を提示し、人間に選ばせる
3. Issue作成とブランチの切り出し(`issue-N`)
4. implementer による実装
5. tester → reviewer のループ(SUCCESS かつ APPROVE まで)
6. doc-updater による文書・ADR更新
7. Issueへのコメントとクローズ、マージ

このステップ列は、第2.4節の委譲ルールをワークフローとして具体化したものである。人間が `/next-task` と打つだけで、状況把握から実装・テスト・レビュー・記録・完了までの一連が、定められた順序で進む。本文には「テスターが SUCCESS を返し、かつレビュアーが APPROVE を返すまでこのサイクルを繰り返せ。このループを飛ばすな」といった厳格な指示が明記されており、品質ゲートを省略できないようになっている。

### 4.3 new-spec と brainstorm — 新しいものを作る

残る2つのスキルは、新機能や新しい構想を扱う。`new-spec` のフロントマターを見る。

```text
---
name: new-spec
description: Trigger this skill when the user asks to create a new feature,
implement a new requirement, brainstorm specifications, or start developing
a new function.
allowed-tools: Read, WebSearch, Agent(doc-updater, implementer, tester, reviewer, tech-lead, product-manager), AskUserQuestion
disallowed-tools: Write, Edit, Replace
model: opus
---
```

next-task との違いは、`Agent(...)` に **tech-lead と product-manager が加わっている**点である。new-spec は新機能を作るスキルなので、実装ループの4役に加えて、実装前の判断を担う2役を呼び出せる。手順も、仕様のブレインストーミング → PM/tech-lead の承認ゲート → 実装ループ、という順序になっている。作るべきかを判断してから作る、という流れがスキルとして固定されている。

`brainstorm` はさらに前段階の、まだ仕様が存在しない構想を扱う。フロントマターの `disallowed-tools: Bash` が示すように、コマンド実行すら禁じられた、純粋な思考のためのスキルである。

3つのスキルは、開発の段階に対応している。**brainstorm(構想)→ new-spec(新規実装)→ next-task(継続実装)**。どの段階でも、統括スキル自身は書き込まず、専門サブエージェントに委譲する構造は共通している。

---

## 第5章 .claude/hooks/ — 完了を止めるフック

### 5.1 Stopフックの登録

第2.3節で触れた「ADRを更新しないと完了できない」仕組みを、いよいよ読む。まず、フックの登録を見る。`.claude/settings.json` の内容である。

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/adr-check.sh",
            "timeout": 15,
            "statusMessage": "ADR更新チェック中..."
          }
        ]
      }
    ]
  }
}
```

`hooks.Stop` は、エージェントが**作業を完了しようとした瞬間(Stop)**に走るフックの登録である。ここに `adr-check.sh` というシェルスクリプトを、コマンドとして登録している。`timeout: 15` は15秒でタイムアウトする指定、`statusMessage` は実行中に表示されるメッセージである。

Stopフックは、エージェントが「終わります」と言う直前に割り込む。フックが完了を許可すれば終われるが、拒否すれば終われず、指摘された内容に対処してから再度完了を試みることになる。

### 5.2 adr-check.sh — 設計変更を検出する

フックの本体を読む。前半は準備である。

```bash
#!/bin/bash
# Stop hook: block completion when design changes lack an ADR update.
input=$(cat)

# Prevent infinite loop: if we already blocked once this turn, let Claude stop.
if [ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null)" = "true" ]; then
  exit 0
fi

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0
```

- `input=$(cat)` で、フックに渡される情報(JSON形式)を標準入力から読む。
- 次の `if` が**無限ループの防止**である。フックが一度ブロックすると、`stop_hook_active` が真になる。すでに一度ブロックした後であれば、`exit 0`(完了を許可)して抜ける。これがなければ、対処後も延々とブロックし続けてしまう。一度指摘したら、次は通す、という設計である。
- `git rev-parse` でリポジトリのルートに移動する。git管理下でなければ何もせず終了する。

続いて、変更されたファイルを集める。

```bash
# Changes in this work window: uncommitted diff + commits from the last 30 minutes.
changed=$(
  {
    git diff --name-only HEAD -- 2>/dev/null
    git log --since="30 minutes ago" --name-only --pretty=format: 2>/dev/null
  } | sort -u | sed '/^$/d'
)
```

「この作業で変更されたファイル」を2つの経路から集める。一つは未コミットの差分(`git diff`)、もう一つは直近30分のコミットに含まれるファイル(`git log --since`)。両者を合わせて重複を除く。コミット済み・未コミットの両方を対象にすることで、「今の作業で触ったもの」を漏れなく拾う。

### 5.3 判定 — 設計ファイルの変更にADRが伴うか

集めたファイルのうち、設計に関わるものを抽出し、ADRの更新があるかを確認する。

```bash
design=$(printf '%s\n' "$changed" | grep -E '^(docs/(architecture|detailed-design|workflows)/|config/[^/]+\.ya?ml$)')

if [ -n "$design" ] && ! printf '%s\n' "$changed" | grep -q '^docs/planning/adr\.md$'; then
  jq -n --arg files "$(printf '%s' "$design" | head -10)" '{
    decision: "block",
    reason: ("設計変更が検出されましたが docs/planning/adr.md が更新されていません。\n変更ファイル:\n" + $files + " ...")
  }'
fi
exit 0
```

判定は2つの条件の組み合わせである。

- `design` — 変更ファイルの中に、`docs/architecture/`・`docs/detailed-design/`・`docs/workflows/` 配下、または `config/` 直下の `.yaml`/`.yml` があるか。これらが「設計に関わるパス」である。
- 後半の `grep -q '^docs/planning/adr\.md$'` — 変更ファイルの中に `docs/planning/adr.md` が含まれるか。

条件は「設計ファイルが変更された(`design` が空でない)、**かつ** ADRファイルは変更されていない(`!` で否定)」である。この両方が成り立つとき、`jq` で `{"decision": "block", "reason": "..."}` というJSONを出力する。この `decision: "block"` が、エージェントの完了を拒否する。理由文には、どのファイルが設計変更に該当したかが列挙される。

逆に言えば、設計ファイルを変更したときに `docs/planning/adr.md` も一緒に変更していれば、条件は成立せず、フックは何も出力せずに `exit 0` する。完了が許可される。

### 5.4 フックが強制する規律

この30数行のシェルスクリプトが、第2.3節で `CLAUDE.md` が述べた「設計変更にはADRを伴え」という規律を、**言葉のお願いではなく実行される検査**に変えている。

エージェントが設計ファイルを変更してADRを書き忘れたまま完了しようとすると、フックがブロックし、理由を返す。エージェントはそれを読み、ADRを追記するか、あるいは「これは誤字修正なのでADR不要」と人間に一言伝えてから再度完了する(第5.2節の無限ループ防止により、2度目は通る)。

重要なのは、この強制が**AIの自己申告に依存していない**点である。エージェントが「ADRを書くのを忘れないようにしよう」と心がけるのではなく、忘れたら機械的に止まる。前章までのプロキシが「セキュリティ判定をAIの外側の決定的なコードに置く」思想を持っていたのと同じ発想が、開発プロセスの規律にも適用されている。

---

## 第6章 全体像 — 設定が形づくる開発の流れ

### 6.1 3つの構成要素の連携

ここまで読んだ要素を、一つの流れとして結ぶ。

- **CLAUDE.md** が、プロジェクトの決まりごと(仕様駆動・ADR規律・委譲ルール)を宣言する。
- **skills** が、その決まりごとを実行可能なワークフローに落とす。人間が `/next-task` や `/new-spec` と打つと、統括スキルが起動する。
- **agents** が、ワークフローの各段階を担う専門役割を提供する。統括スキルは自分では書き込まず、implementer・tester・reviewer らに委譲する。
- **hooks** が、規律を機械的に強制する。設計変更にADRが伴わなければ完了を止める。

人間の関与は「何をするか」の指示と、要所での承認・判断に絞られる。「どう進めるか」(実装 → テスト → レビュー → 記録 → 完了)は、設定に書かれた手順が担う。

### 6.2 一貫する思想 — 決定と実行の分離

`.claude/` 全体を貫く思想は、**「決定を下す役割」と「作業を実行する役割」の分離**である。

- 統括スキル(next-task / new-spec)は決定と委譲を行うが、コードは書かない(`disallowed-tools: Write`)。
- product-manager / tech-lead は判断するが、実装はしない(`tools` に書き込み系がない)。
- doc-updater は記録するが、内容は決めない(`CLAUDE.md` が明記)。
- implementer / tester は手を動かすが、さらなる委譲はできない(`disallowedTools: Agent`)。

それぞれの役割に対して、使えるツールの範囲が権限として与えられ、役割を越えた行為ができないようになっている。これは第5章のフックと同じく、「そうあるべき」を「そうしかできない」に変える構造である。

### 6.3 なぜこう作るのか

AIエージェントは有能だが、その判断は非決定的である。同じ指示でも、日によって、あるいは文脈によって、異なる進め方をとりうる。開発という積み重ねの作業では、この揺らぎが品質のばらつきや手順の抜けを生む。

`CLAUDE.md` と `.claude/` は、この揺らぎを設定で抑え込む装置である。決まりごとを文書に固定し(CLAUDE.md)、手順をワークフローに固定し(skills)、役割と権限を分離し(agents)、規律をフックで強制する(hooks)。AIには「何をしたいか」の柔軟さを残しつつ、「どう進めるか」の骨格を外側から与える。

これは、前章までのプロキシ群がセキュリティに対してとった姿勢 — AIを信頼するのではなく、AIが揺らいでも壊れない側にシステムを置く — を、開発プロセスそのものに適用したものである。7mimi-agent のコードの大半が、実際にこの仕組みの下で、サブエージェントの分業によって書かれている。設定ファイルが、開発の進め方そのものを形づくっているのである。
