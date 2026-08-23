# P-0174 — 秘書はまだ一度も「おはようございます」と言っていない — homelab の昨日の変化を毎朝 Telegram の一行 brief にして、口 (OpenClaw/Telegram) を日常に乗せる

## 目的

VISION の核心「自発的に動く秘書」の最も小さい実装。Discord は予告と納品のための業務チャネルで
あって生活者への接点ではなく、OpenClaw 本番稼働 (P-0090) 以来「口」は返事を待っているだけ。
health latest.json と projects 状態という既存のデータだけで「昨日増えた納品・Degraded の変化・
backup 新鮮さ」を 3 行で毎朝届ける習慣を作る。私的データを一切読まないため lethal trifecta に触れない。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing** (2026-08-23、`project/p-0174` の
checkout でリポジトリルートから実行)。

- [ ] `test -f apps/openclaw/morning-brief-cronjob.yaml`
  — 毎朝の brief を送る CronJob manifest が存在すること。実測 rc=1 (apps/openclaw/ 自体は
    P-0090 で存在するが、morning-brief-cronjob.yaml は未作成)。
- [ ] `python3 -m unittest ops.tests.test_morning_brief`
  — 送信文コンポーザの純関数テストが通り、「データが空でも壊れない」「3 行を超えない」が
    固定されていること。実測 `FAILED (errors=1)` (ops/tests/test_morning_brief.py 未作成 =
    モジュール不在)。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測した。調べ直さなくてよい)

- **新規データは不要。情報源は既存の git ブランチ 2 本**:
  - `ops-health-report` ブランチの `ops/health/latest.json` — `applications[]` (name/sync/health)、
    `generated_at`、`pvc_usage[].backup_listing.files[].mtime` (immich バックアップの実ファイル
    日時 = backup 新鮮さの信号)、`download_budget.by_job` (backup Job の稼働証跡)。前日比には
    同ブランチ `ops/health/history/YYYY-MM-DD.jsonl` (1 日 1 ファイル、latest.json 同形の
    スナップショット 1 行ずつ) の前日最終行と比較する。
  - `origin/ops-state:projects.json` — projects[] に state / prs / created はあるが
    **納品時刻のフィールドが無い**。「前日の delivered」は main の merge commit の日時
    (`Merge pull request #N` の committer date) か GitHub API の `merged_at` で数える必要がある
    (projects.json 単独では前日フィルタ不能 — worker セッションで最初に確定させる論点)。
- **ブランチ読み取りの in-cluster 前例**: apps/ops-health-reporter/report.py が GitHub Contents API
  (`github_request`, report.py:411) で生 JSON を取得する。openclaw は autopilot ns で稼働し、
  `AUTOPILOT_GITHUB_TOKEN` は allowed_autopilot_doppler_keys 済み・apps/autopilot/external-secret.yaml
  が注入済みなので、同 ns の CronJob から読める。
- **Telegram 送信の実績**: TELEGRAM_BOT_TOKEN は P-0090/P-0107 で Doppler 登録・allowlist・
  実送信まで通っている。sendMessage は送信専用 API。api.telegram.org への egress は同一 ns の
  OpenClaw が実証済み。
- **スクリプト搭載の流儀**: configMapGenerator で .py を /scripts に mount (sys.path[0]=/scripts)。
  apps/ops-health-reporter/kustomization.yaml と apps/openclaw/bridge.py (P-0107) の同型。
  純関数は単一ファイルに分離し、テストからは importlib でロードするのが慣習
  (ops/tests/test_download_budget.py 冒頭の経緯注記)。
- **CronJob schedule は JST 評価** (node01 の time.timeZone。既存 CronJob も spec.timeZone は
  書いていない — syncthing/version-watcher の注記)。
- **分離 Job プロファイルは未着**: spec why が名指す P-0161 の `ops/profiles/private-data/` は
  このブランチ上にまだ存在しない。本プロジェクトは私的データを読まないので trifecta プロファイルを
  必要としない。verify の正は `apps/openclaw/morning-brief-cronjob.yaml` であり、P-0161 の完成を
  待ってはいけない。

### 方針 (spec の DoD (1)-(3) をそのまま落とす)

1. `apps/openclaw/morning-brief-cronjob.yaml` (autopilot ns) を CronJob として追加し、
   送信文コンポーザを純関数の単一モジュールとして分離。両方 configMapGenerator で載せ、
   kustomization.yaml に配線する。schedule は毎朝 JST 定時 (既存 CronJob 流儀に従い timeZone 非記入)。
2. 入力は読み取り専用: latest.json + 前日 history jsonl + projects.json (+ 前日納品数の補完源)。
   出力は 3 行 — (a) 前日の delivered/納品 PR 数、(b) アプリ健全性の前日比
   (Healthy↔Degraded 等の変化があったアプリ名)、(c) backup 新鮮さ。データが欠けても行を
   潰さず正直に減らす (壊れないことをテストで固定)。
3. 送信は Telegram sendMessage (TELEGRAM_BOT_TOKEN) の 1 通のみ。受信はしない。
   `--dry-run` では stdout への出力のみで送信しない。
4. unittest (ops/tests/test_morning_brief.py) で最低 2 契約を固定: 「データが空でも壊れない」
   「3 行を超えない」。importlib ロード方式。
5. 通知予算の明記: rules.json の notify 節 (または state.json) に「Telegram の朝 brief は
   1 日 1 通で、Discord 即時送信の daily_budget とは別枠」を書き加える。

## やらないこと

- **受信・返信・双方向対話** — 送信専用。getUpdates/webhook を触らない (受信は OpenClaw bridge
  (P-0107) が既に担う。消費者 1 つの制約を侵さない)。
- **私的データの読み取り** — health/projects の機械データのみ。lethal trifecta に触れない。
- **LLM の呼び出し** — 文面整形は決定論の純関数のみ。
- **health reporter / ops-state 側の変更** — 新規データ源を作らず既存ブランチを読むだけ。
  report.py・runner への手入れは別論点 (1 PR 1 論点)。
- **分離 Job プロファイル (ops/profiles/private-data/) の実装・適用** — P-0161 の領分。
  未着でも本プロジェクトは待たない。
- **Discord 側の通知予算・型の変更** — 既存の daily_budget と 4 型は据え置き。
- **Mission Control / ダッシュボードへの出力追加** — Telegram 1 通のみ。
