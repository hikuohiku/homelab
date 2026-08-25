# P-9047 — 写真の金庫は一度も開けられたことがない — immich を restic から scratch に丸ごと戻し、写真と DB が生きて返ることを初めて実測する

## 目的

復元の実証は vaultwarden (P-9025)・k3s 状態ストア (P-9017)・coder workspace home (P-0288) と並んだのに、
最大のデータ保持者 immich だけは P-0005 の初期棄却以来一度も復元されていない。
08-22 の B2 download cap 超過で backup 子 Job が落ちて全アプリ Degraded になった実測 (P-0111)
以降、「取れているだけの backup」の信用は揺らいでおり、restic 5 本の CronJob が取る backup が
本当に戻せるかを immich で初めて証明する。この実証は P-0291 (immich postgres 16.14) の安全網にもなる。

## 受入チェックリスト

initializer が 2026-08-25 に `project/p-9047` checkout のリポジトリルートから実行した結果、
**3 項目とも現時点で failing** (wrapper の実測どおり)。

- [ ] `test -f ops/tools/immich_restore_drill.py && python3 ops/tools/immich_restore_drill.py --check`
  — 復元 drill 本体が実在し、`--check` (自己検査) が rc=0 で終わること。
  実測 rc=1 (ファイル未存在)。
- [ ] `kubectl get configmap -n autopilot immich-restore-drill-report -o jsonpath='{.data.photo_count}' | grep -q '^[0-9][0-9]*$'`
  — 成功記録 ConfigMap `autopilot/immich-restore-drill-report` の `photo_count` (復元 DB の写真数) が
  数字として残っていること。実測 rc=1 (ConfigMap 未存在)。
- [ ] `python3 ops/tools/immich_restore_drill.py --verify-freshness --max-age 3d`
  — drill の成功記録が 3 日以内 (最新) であること。復元実測が古いままだと落ちる。
  実測 rc=2 (前提ファイル未存在)。

**verify は DoD の下限であって DoD そのものではない。** spec の dod どおり、
(1) immich の最新 restic snapshot (DB + uploads の両方) を scratch namespace に復元し、
(a) postgres が起動して vchord が生き、(b) 復元 DB の assets 行数が本番の実測と一致し、
(c) 復元された immich-server の /api が 200 を返す、ところまでが本体。
手順を `ops/tools/immich_restore_drill.py` に再実行可能な形で固定し、
成功記録 (復元日時・snapshot id・写真数・所要時間) を ConfigMap `immich-restore-drill-report` に残す。

## 設計方針

### 前提 (initializer が 2026-08-25 に実読した。調べ直さなくてよい)

- **backup 構成**: `immich-restic-backup` CronJob (apps/immich/restic-backup-cronjob.yaml、
  schedule `45 2 * * *` = UTC 17:45) が `immich-library` PVC を 1 本で backup する
  (`b2:$(RESTIC_B2_BUCKET):immich`)。immich-server (v3.1.0) 内蔵の日次 DB ダンプ
  (毎日 02:00 UTC、keepLastAmount 14) が `UPLOAD_LOCATION/backups/*.sql.gz` に落ちるため、
  snapshot 1 本に **DB ダンプと uploads が両方入る** (docs/backup.md「immich の restic バックアップ」)。
- **credential**: append-only 鍵 `immich-restic-backup-credentials` (immich ns, restic-external-secret.yaml)。
  restore は `readFiles` で完結するので削除権限つき `immich-restic-credentials` は持ち出さない
  (P-0341 の結論。P-9025/P-0047 で実証済み)。
- **restic restore の権限**: CHOWN / FOWNER / DAC_OVERRIDE の 3 capability が必要
  (docs/backup.md「復元試験 (T-0071)」の教訓)。backup 側の DAC_READ_SEARCH では足りない。
  再実行時は scratch を先に `rm -rf` で掃除する。
- **postgres**: cloudnative-vectorchord `16.14-1.1.1` (apps/immich/postgres.yaml)。ダンプは
  pgvector/embeddings を含むフルダンプ (除外なし) で、ファイル名
  `v{serverVersion}-pg{postgresVersion}` を突き合わせてから復元する (docs/backup.md の注意)。
- **既存パターン**: 復元 drill の最短路は P-9025 (`ops/vaultwarden_restore.py`) と同型 —
  使い捨て Job を kubectl CLI で apply、initContainer で restic バイナリを emptyDir へ、
  main は python (標準ライブラリのみ)。snapshot の直近性チェックで fail-closed にする。

### 決めてあること

- **実行は worker の使い捨て Job 経由**。復元先は scratch namespace (本番の
  `immich-library` / `immich-postgres-data` には不触)。apps/ の manifest は変えない。
- **`ops/tools/immich_restore_drill.py` に手順を固定**: snapshot 直近性確認 → restic restore →
  最新 .sql.gz を gunzip → scratch postgres (vectorchord) へ `psql` → postgres 起動 + vchord 生存確認 →
  assets 行数を本番実測と照合 → immich-server を scratch 起動し /api が 200 を返す確認 →
  ConfigMap `immich-restore-drill-report` (autopilot ns) に成功記録 (復元日時・snapshot id・
  写真数・所要時間) を書く。
- **capabilities**: `kubectl-write` のみ。touches_apps: false。

## やらないこと

- **本番 PVC への書き込み・復元先の本番化**。復元は scratch namespace に限定。本番への
  切替 (リストア運用) は本プロジェクトの外。
- **apps/ 配下の manifest 変更** (`touches_apps: false`)。immich の backup/retention CronJob・
  ExternalSecret・Deployment には触れない。
- **retention (forget --prune) や削除権限つき鍵 `immich-restic-credentials` の使用**。
  B2 上の snapshot の削除・プルーニングは行わない。
- **Doppler の credential 発行・変更・ローテーション**。物理作業を伴う人間専有 (CHARTER §4)。
- **immich のバージョン更新・設定変更**。P-0291 (postgres 16.14) の安全網になるが、本プロジェクトは
  復元の実証のみ。drill の定期実行 (CronJob) 化も spec が要求していないためやらない (1 PR 1 論点)。
- **ops/backlog.json / ops/state.json / ops/journal/ の更新**。heart が直接 push する領域で
  コンフリクトする (CLAUDE.md)。