# P-0145 PROGRESS

後続セッションは PROJECT.md とこのファイルと git log だけを文脈として引き継ぐ。
やったことをここに残す。ここに書かなかったことは存在しなかったことになる。

## 2026-08-23 initializer

- PROJECT.md / PROGRESS.md を作成。verify 3 項目とも failing を実測 (rc=1 × 3)

## 2026-08-23 worker セッション 2

### やったこと

- **vaultwarden 1.37.1→1.37.2-alpine** (commit ab619631): リリースノート原文読了
  (「クライアント 2026.8.0+ のサポートに必須」が更新の主根拠)。migration 無しを
  tag 間 diff (`migrations/` 空) で確認、alpine イメージ実在を Docker Hub API で確認。
  deployment.yaml の image + コメント、inventory `current`/`last_swept_at`
  (2026-08-23T04:54:24Z) を同一 commit で更新。
- **coder v2.35.3→v2.35.4** (commit 0ed287c6): stable チャンネル判定は本文冒頭
  `> ## Stable (since August 10, 2026)` で確定 (P-0029 手法の再現)。mainline v2.36.x は回避。
  server 側 security fix 1 件 (GHSA-h58h-qvv5-xvwg) を含む。migration 無しを T-0023 手法
  (migrations file listing diff = 空) で再確認。ghcr のタグは **v 付き形式のみ存在**
  (リリース本文の `docker pull ...:2.35.4` は v 無し表記だがレジストリに無いので無視した)。
- 証跡: release_notes_vaultwarden.md / release_notes_coder.md 新設。
- 棚卸し: sweep.md 新設 (対象・現状・判定・rollback 手順・PR 欄)。
- 各 commit 前に `kubectl kustomize apps/{vaultwarden,coder}` render、
  `ops/validate.py` (0 error)、`ops/check_version_sync.py` 全 ok を実測。
- **verify 3 項目とも自力で green 実測済み** (rc=0 × 3)。

### 分かったこと / 罠

- `/tmp/opencode` は root 所有で書けない (uid 10001 から Permission denied)。
  一時ディレクトリは mktemp で作ること。
- GitHub API rate limit は unauthenticated 60/h で、セッション途中で残り 4 まで減った。
  releases/trees 系の API を連打しないこと。**tag 間 file diff は git partial clone
  (`git clone --filter=blob:none --no-checkout`) + `git diff --name-status A B -- path` が
  API を消費せず T-0023 手法そのもの**で確実。registry 確認 (Docker Hub / GHCR tags API) も
  rate limit 対象外。
- GHCR の tags/list はページング必須 (既定 100 件、lexical 順)。`?n=1000` でも足りなければ
  `&last=<既知のタグ>` で継続。`last` に実在しないタグを渡すと `tags: null` が返る。
- 「1 PR 1 コンポーネント」は wrapper の 1 project = 1 PR モデルと両立しないため、
  **commit 単位で分離した** (ab619631 / 0ed287c6)。revert は各 commit 単位で可能。
  sweep.md にもこの注記あり。
- CODEOWNERS 保護パスには今回触っていない (deployment/inventory/logs はいずれも対象外)
  → auto-merge 条件の通常判定で問題ない見込み。

### 残っている作業 (DoD の最後の 1 つ)

merge → ArgoCD sync 後の health 確認。手順は sweep.md 末尾に書いてある:
`git fetch origin ops-health-report && git show origin/ops-health-report:ops/health/latest.json`
で両 Application の Synced/Healthy を実測し、`generated_at` と状態をここに記す。
Degraded 判定の際は backup CronJob 子 Job 失敗由来の偽陽性に注意 (PROJECT.md 前提節)。

### 次のセッションへの一言

push 後に PR 番号が採番されたら sweep.md の「採番待ち」欄を埋め、merge 後は上記 health
確認を実測して PROGRESS.md に証跡 (generated_at + 両アプリの状態) を残す — それだけで完成。
