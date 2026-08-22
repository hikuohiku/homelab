# P-0090 — OpenClaw を Telegram の口として導入する (デプロイ + 決定論パススルー・ブリッジ)

## 目的

人間の依頼 (2026-08-22 grill 済み)。OpenClaw を Telegram bot の「口」として導入し、veto /
書き置き / 状態応答に加えてタスク依頼の構造化受付まで担う。生活ドメイン (Gmail 等) は開かない
(VISION 段階 3 のゲート前)。**絶対条件: 停止系は決定論パススルー** — 受信メッセージの生テキストを
必ず ops-feedback ブランチへ保存し、停止/veto 判定は heart の triage に任せ、OpenClaw の LLM を
停止経路に関与させない。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing** (2026-08-22、`project/p-0090` の
checkout でリポジトリルートから実行、いずれも rc=1)。

- [ ] `test -f apps/openclaw/kustomization.yaml`
  — apps/openclaw/ が存在し Kustomize の入口があること。実測 rc=1 (ディレクトリ自体が未作成)。
- [ ] `grep -q 'openclaw' apps/kustomization.yaml`
  — ArgoCD App of Apps root への登録済みであること。実測 rc=1 (現行 resources は argocd〜
    syncthing の 12 アプリで openclaw を含まない)。
- [ ] `grep -q 'TELEGRAM_BOT_TOKEN' ops/rules.json`
  — rules.json の allowed_autopilot_doppler_keys への追加済みであること。実測 rc=1 (現行は
    CLAUDE_CODE_OAUTH_TOKEN / AUTOPILOT_GITHUB_TOKEN / DISCORD_WEBHOOK_URL /
    OPENCODE_API_KEY の 4 鍵のみ)。

## 設計方針

### 前提 (initializer が 2026-08-22 に実読した。調べ直さなくてよい)

- **書き置きの原本形式**: ops-feedback ブランチの `ops/feedback/inbox/<id>.json`、1 件 1 ファイル、
  `{id, source, received, body}` (`apps/ops-dashboard/app/src/app/api/feedback/route.ts:89-99`)。
  main 直 push 不可能のため GitHub Contents API で専用ブランチへ書く経路が既にある。
  Telegram 版は同形式で `source: "telegram"`。
- **heart 側は既に決定論**: `ops/heart/facts.py` が inbox 新着を読み、`ops/heart/triage.py` の
  `classify()` が veto / stop_all / resume_all / review_needed をキーワードルールのみ (LLM ゼロ)
  で分類する。**生テキストさえ inbox 形式で保存すれば停止経路は既存パススルーに乗る** —
  OpenClaw 側に判定ロジックを新設しないことが絶対条件の実装になる。
- **タスク依頼とのインターフェイスは note の kind フィールドだけ** (P-0091 spec 明記)。
  task-request は同形式に `kind: task-request` を足すだけでよい。器側の消費配線は P-0091 の領分。
- **前例**: digest pin は `apps/autopilot/heart-deployment.yaml:49` 等の `image: ...@sha256:<hex>` 形式。
  アプリ追加は `apps/<name>/kustomization.yaml` + `application.yaml` (CreateNamespace=true) を作り
  root `apps/kustomization.yaml` に 1 行足すだけ (syncthing が直近の手書き全リソース例、PVC もあり)。
  バージョン監視対象は `ops/inventory.json` の targets に image エントリを足すのが流儀。
- **credential**: OPENCODE_API_KEY は Doppler 登録済み (2026-08-22 人間、
  `apps/autopilot/external-secret.yaml:45` が注入実績)。TELEGRAM_BOT_TOKEN /
  TELEGRAM_ALLOWED_USER_ID は人間登録待ち — 未登録なら Discord question 通知で依頼して待つ。
- **状態応答の情報源**: Mission Control は in-cluster で
  `http://ops-dashboard.autopilot.svc` の `GET /api/snapshot`
  (`apps/ops-dashboard/app/src/app/api/snapshot/route.ts`)。

### 方針 (spec の設計決定 (1)-(8) をそのまま落とす。変えるなら理由を PROGRESS.md に書く)

1. `apps/openclaw/` を新設。公式 `ghcr.io/openclaw/openclaw` を @sha256 digest pin、状態 SQLite 用の
   小さな PVC、namespace は autopilot 推奨 (spec 決定)。inventory 登録も同じ PR で行う。
2. パススルー・ブリッジは LLM を経由しない決定論コードとして実装する: allowlist 内の送信者の
   メッセージ生テキストを、LLM 応答の成否と無関係に必ず note 形式 (source: telegram) で
   ops-feedback へ保存する。実現機構 (sidecar での getUpdates 先拾い / OpenClaw の hook 等) は
   最初の worker セッションで OpenClaw の実際の拡張機構を調べて決めてよいが、Telegram getUpdates
   は消費者が 1 つという制約があるため「誰が update を受け取るか」を先に確定させること。
   ops-feedback 書き込み用の GitHub トークンをどの鍵で賄うか (AUTOPILOT_GITHUB_TOKEN 流用 vs
   inbox 書き込み専用の細い token を人間に依頼) も同じく最初に決める論点。
3. allowlist 外の送信者は応答も保存もせず完全無視する (プロンプト注入面を閉じる。spec の絶対条件)。
4. LLM は OPENCODE_API_KEY を流用。OpenClaw が opencode go 系エンドポイントを食えるか最初の
   worker セッションで実測し、不可なら question 通知で人間に安い API の契約を依頼して待つ
   (spec 決定)。契約は人間の手作業なので、ブロック中でも他の部分は進めてよい。
5. 状態応答は /api/snapshot を読んで整形するだけに留め、dashboard 側は変更しない。
6. rules.json の allowed_autopilot_doppler_keys へ TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID を
   追加する。validate.py が CI で検査するため、apps/openclaw/ の ExternalSecret が参照する鍵名と
   揃えること。

## やらないこと

- **器側の task-request 消費配線** (curriculum の最優先原料化・処理済み管理) — P-0091。
  こちらは kind フィールドというインターフェイスを守るだけ。1 PR 1 論点。
- **生活ドメインを開く** (Gmail / Calendar 等) — VISION 段階 3 のゲート前。spec が明示的に閉じている。
- **heart の triage ロジック・veto/stop キーワードの変更** — 既存の決定論分類を使うだけ。
- **OpenClaw の fork・改造・独自ビルド** — 公式イメージを digest pin で使うだけ。
- **他チャネル対応** (WhatsApp / Discord bot 等) — Telegram のみ。
- **Doppler への鍵登録・API 契約** — 人間の手作業。エージェント側は question 通知と待機まで。
- **memory limits の新設** — substrate の規則 (実測の裏付けなしに付けない) を継続。
