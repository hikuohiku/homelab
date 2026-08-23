# P-0145 — vaultwarden / coder 追従滞留の sweep 記録

実施日: **2026-08-23** (P-0145 worker セッション。すべて当日の一次情報)
対象: `ops/inventory.json` のうち #49 の前科がある場所と同じ「policy=auto のまま滞留している」2 target。
今後の見張りは version watcher (P-0126) の領域で、本 sweep は**滞留の解消と `last_swept_at` の起算**が目的。

## sweep 表

| # | 対象 (id) | 現状 | 上流 | 判定 | 更新内容 | PR |
|---|-----------|------|------|------|----------|-----|
| 1 | `vaultwarden` | 1.37.1-alpine (滞留。1.37.1 は 2026-07-29 リリース) | `1.37.2` (2026-08-22, prerelease 無し) | **更新する**。リリースノート Note 冒頭に「クライアント 2026.8.0+ のサポートに必須」とあり、#49 型のクライアント互換切断を放置しない。breaking change 宣言なし。migration 無し (`git diff --name-status 1.37.1 1.37.2 -- migrations/` = 空)。alpine イメージ実在を Docker Hub で確認 (last_pushed 2026-08-22T12:18:50Z) | `apps/vaultwarden/deployment.yaml` の image を `vaultwarden/server:1.37.2-alpine` へ + inventory `current`/`last_swept_at` 同時更新 (commit ab619631) | 本プロジェクトの PR (branch `project/p-0145`、採番待ち — push 後に後続セッションがここへ追記) |
| 2 | `coder` | v2.35.3 (stable, 2026-07-27) | stable 系最新 = `v2.35.4` (2026-08-10)。mainline v2.36.0/v2.36.1 は note の除外規定により触らない | **更新する** (stable 内 1 ホップ)。本文冒頭 `> ## Stable (since August 10, 2026)` でチャンネル確定。server 側 security fix 1 件 (GHSA-h58h-qvv5-xvwg, workspace proxy hostname prefix 拒否 — 本環境は workspace proxy 未使用だが server 防御修正なので取り込み方向)。BREAKING CHANGES 節なし。migration 無し (T-0023 手法の file listing diff 再現: `git diff --name-status v2.35.3 v2.35.4 -- coderd/database/migrations/` = 空、diff 全体 25 ファイルにも migrat/schema を含まず)。ghcr には `v2.35.4` (v 付き) のみ存在を tags API で確認 | `apps/coder/deployment.yaml` の image を `ghcr.io/coder/coder:v2.35.4` へ + inventory `current`/`last_swept_at` 同時更新 (commit 0ed287c6) | 同上 (同一 PR 内の別 commit。「1 PR 1 コンポーネント」は wrapper の 1 project = 1 PR モデルのため commit 単位で分離。revert は各 commit 単位で可能) |

## 共通事項

- **証跡**: 判断材料になった原文引用・URL・実測コマンドは
  [release_notes_vaultwarden.md](release_notes_vaultwarden.md) /
  [release_notes_coder.md](release_notes_coder.md)。
- **mirrors**: initializer の grep 実測どおり両 target とも mirrors 未設定
  (二重管理 pin 無し)。今回も 1 ファイルずつの変更で済んだ。
- **CODEOWNERS**: 触るパス (`apps/vaultwarden/deployment.yaml`, `apps/coder/deployment.yaml`,
  `ops/inventory.json`, `ops/projects/logs/P-0145/*`) はいずれも保護対象外
  (`.github/CODEOWNERS` 実読) → auto-merge 条件の判定は通常どおり。
- **手元検証**: `kubectl kustomize apps/vaultwarden` / `apps/coder` の render 成功、
  `python3 ops/validate.py` (0 error、warning 11 件は既存の backlog refs 系で本件無関係)、
  `python3 ops/check_version_sync.py` 全 ok を各 commit 前に実測済み。

## ロールバック手順 (CHARTER §4 — PR 本文にも同じ内容を載せること)

- **vaultwarden**: `git revert` 該当 commit → ArgoCD sync で旧イメージに戻る。
  SQLite は起動時 migration のみで、**1.37.1→1.37.2 は schema 変更無し**を実確認済みのため
  「コードは revert できてもスキーマは戻らない」問題は今回は発生しない。データ (`/data`) には触らない。
- **coder**: 同じく `git revert` → sync。PostgreSQL スキーマも **v2.35.3↔v2.35.4 で不変**
  (migrations dir diff 空) を確認済み。workspace PVC は Deployment とは別ライフサイクルなので影響なし。

## merge 後に残る作業 (DoD の残り)

ArgoCD sync 後に両 Application が Synced/Healthy に戻ったことを health ブランチで実測する:

```
git fetch origin ops-health-report && git show origin/ops-health-report:ops/health/latest.json
```

- substrate.md の訂正済み注意: coder/vaultwarden の Degraded は backup CronJob の子 Job 失敗
  由来のことがあるため、Application 自体の状態と直近 backup Job の成否を区別して判定する。
- 実測結果 (`generated_at` と両アプリの状態) は PROGRESS.md に記す。
