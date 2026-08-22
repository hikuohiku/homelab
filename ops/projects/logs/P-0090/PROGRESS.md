# P-0090 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## セッション記録

<!-- 1 セッション 1 ブロック。何をやったか / 分かったこと / 次への一言 を書く -->

### initializer (2026-08-22)

PROJECT.md を起票した。受入 3 項目は全て failing を実測済み (詳細は PROJECT.md)。
実装には着手していない。

### worker #1 (2026-08-22) — apps/openclaw の器 + credential 配線

**やったこと**: 受入 3 項目を一括で green にした (器フェーズ)。

- `apps/openclaw/` を新設: application.yaml (ArgoCD App、namespace autopilot)、
  pvc.yaml (openclaw-state 2Gi、Prune=false)、config.yaml (ConfigMap openclaw-config
  = openclaw.json)、external-secret.yaml (openclaw-credentials 4 鍵)、deployment.yaml
  (digest pin、initContainer が config を PVC へ配置、probe /healthz・/startupz)
- root `apps/kustomization.yaml` 登録 / `ops/rules.json` 鍵追加 /
  `ops/check_credential_map.py` 地図更新 / `ops/inventory.json` 登録 /
  CLAUDE.md・apps/README.md 追記 (check_app_list_sync.py が要求する)
- CI 相当検査を全て自力で green 実測: validate.py (0 error)、check_credential_map.py
  (+ 単体テスト 22 件)、check_app_list_sync.py、check_version_sync.py、
  `kubectl kustomize` (openclaw 単体と apps root)、ops/tests 全体 (68 件 OK)

**分かったこと / 設計確定事項**:

- **OpenClaw は OpenCode を built-in provider として持つ**。OPENCODE_API_KEY env +
  `opencode-go/<model>` refs (docs.openclaw.ai/gateway/config-tools「OpenCode」節)。
  spec 決定 (4) の「食えるか実測」はドキュメント裏付けで解消 → 安い API 契約の依頼は不要の見込み。
  config 側 primary は ops/models.json 同様 `opencode-go/ox-alpha-free` にした
  (ただし Pod 起動での実測はまだ。models.json ↔ openclaw.json 揃えは手動運用、inventory note 参照)
- **決定論パススルーの機構を確定した**: Telegram getUpdates の消費者は OpenClaw gateway
  自身のみ (grammY long polling)。sidecar が getUpdates を先拾いすると conflict (409) で
  禁止。bridge は **internal hook (`message:received`)** — HOOK.md + handler.ts 形式を
  `hooks.internal.load.extraDirs` から読ませる。LLM を経由せず gateway 内で発火し、context に
  content/from/metadata.senderId がある。絶対条件 (停止経路に LLM を関与させない) の実現機構。
  handler 実装は次セッション (下記引き継ぎ参照)
- **allowlist 外完全無視は OpenClaw 標準機能で実現**: `channels.telegram.dmPolicy:
  "allowlist"` + `allowFrom`。pairing 承認も使わない。groups 未設定なら groupPolicy 既定
  allowlist で group 全遮断 (DM 専用 bot)。config 側 allowFrom は
  `["${TELEGRAM_ALLOWED_USER_ID}"]` (env substitution) — env 未解決時は誰にも一致しない fail-closed
- **digest 実測**: ghcr.io/openclaw/openclaw **2026.7.1 = sha256:6a31d44b2944e7adcd2b582bf6fb463111264ebca97a0201795b799135bd102c**
  (index/マルチアーキ。registry API の docker-content-digest ヘッダ読み)。docs 上 version tag は immutable。
  dated tag (`-rYYYYMMDD`) は docs 記載があるが tags list に見当たらず未使用
- **OPENCLAW_GATEWAY_TOKEN を spec 決定 (8) 外で足した**: gateway auth required by default
  (docs.openclaw.ai/gateway/configuration) のため。人間への依頼鍵が 3 つになった (下記)。
  rules.json / check_credential_map.py 双方に同じ PR で登録済み (PROJECT.md 方針 6 の整合)
- **config 配置方式**: ConfigMap 直接 mount は不可 — gateway が openclaw.json を
  rename-atomic で書くため subPath mount だと書き込みが壊れる。initContainer (同一 digest
  イメージを root 流用。busybox を新規持ち込みしないことで inventory busybox GROUPS を広げない)
  が毎起動時に chown + PVC copy。Git が常に正本、再起動で Git 内容に戻る

**人間への依頼 (Doppler homelab/prd)**: 未登録のため ExternalSecret は Sync 失敗、Pod は
CreateContainerConfigError 待機 (想定どおりの状態)。

1. `TELEGRAM_BOT_TOKEN` — @BotFather 発行の bot トークン
2. `TELEGRAM_ALLOWED_USER_ID` — 人間の Telegram 数値 user ID
3. `OPENCLAW_GATEWAY_TOKEN` — `openssl rand -hex 32` 等のランダム文字列

**次セッションへの引き継ぎ (bridge hook 実装)**:

- 作るもの: `apps/openclaw/hook/telegram-feedback-bridge/{HOOK.md,handler.ts}` を
  ConfigMap 化して extraDirs (/opt/hooks 等) に readOnly mount。handler は fire-and-forget
  (`void` 投げ) で、metadata.senderId == TELEGRAM_ALLOWED_USER_ID を確認してから
  受信本文を note 形式 `{id, source: "telegram", received, body}` で ops-feedback ブランチの
  inbox へ Contents API 書き込み (id 形式・分岐は apps/ops-dashboard/app/src/app/api/feedback/route.ts
  の newNoteId()/ensureBranch() が原本)
- GitHub token は AUTOPILOT_GITHUB_TOKEN 流用推奨 (ops-dashboard-github-token と同じ根拠:
  書き先は autopilot 専用ブランチの 1 ディレクトリのみ)。external-secret.yaml に data エントリを
  1 行足すだけ — rules.json / check_credential_map.py への追記は不要 (既に許可済み)
- config.yaml には `hooks.internal.enabled: true` + entries + load.extraDirs を足す
- 注意: message:received の context.content はコマンド風メッセージで command body を優先する
  (raw body でなくなる場合あり、hooks doc 明記)。生テキスト保存として要実測
- **罠**: OpenClaw config は strict validation で未知キーがあると gateway が起動しない。
  今回載せたキーは docs 例からのみ選んだが実 Pod 検証は未実施。起動失敗時は `openclaw doctor`
  出力を config.yaml 冒頭コメントに記録すること
- test_backup_coverage.py の EXEMPT_PVCS に ("openclaw", "openclaw-state") を理由付き免除済み
  (bot 状態は再構築可能・原本は ops-feedback ブランチに残る)。実データを蓄積する運用に変えたら
  restic backup を足すこと
