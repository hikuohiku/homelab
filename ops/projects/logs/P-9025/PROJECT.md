# P-9025 — 金庫は一度も開けて戻したことがない — vaultwarden の実データを restic から scratch に戻し、パスワードが生きて返ることを初めて実測する

## 目的

人間のパスワード金庫 vaultwarden (sqlite) は restic の日次 backup が存在する一方、
「バックアップから実際に戻してログイン画面が立ち上がる」= 金庫を開けて戻した実績が一度も無い
(archive に復元系 0 件。T-0117 / P-0208 は coder home・syncthing で実証済み)。
しかも vaultwarden の backup CronJob は B2 の download cap 超過で実に失敗した実績がある (P-0111)。
「戻せるはず」を最重要データから証明し、docs/backup.md の復元手順を実測で裏付ける。

## 受入チェックリスト

initializer が 2026-08-24 に `project/p-9025` checkout のリポジトリルートから実行した結果、
**4 項目とも現時点で failing** (P-0341 の `ops/drills/` 系は未 merge で repo に存在しないため、
本 spec の verify はファイル実在を直接検査する形)。

- [ ] `test -f ops/vaultwarden_restore.py`
  — restic 復元スクリプト (vaultwarden ns の backup と同一 credential を使用) が実在すること。
  実測 rc=1 (ファイル未存在)。
- [ ] `test -f ops/projects/logs/P-9025/restore-summary.json`
  — 復元の所要時間・転送量・integrity・行数を書いた結果 JSON が実在すること。
  実測 rc=1 (ファイル未存在)。
- [ ] `python3 -c "import json;d=json.load(open('ops/projects/logs/P-9025/restore-summary.json'));assert d['integrity']=='ok' and d['rows']>0 and d['duration_seconds']>0"`
  — 復元した sqlite が `PRAGMA integrity_check` を通過し、行数 > 0、所要時間 > 0 であること。
  実測 rc=1 (前提ファイル未存在)。
- [ ] `grep -q 'scratch' ops/projects/logs/P-9025/restore-summary.json`
  — 復元先が本番 PVC ではなく scratch であることが結果 JSON に記録されていること
  (本番 PVC 不触の証跡)。
  実測 rc=1 (前提ファイル未存在)。

**verify は DoD の下限であって DoD そのものではない。** spec の dod どおり、
(1) 直近 24h 以内の snapshot が無ければ backup CronJob を 1 回起動して先に snapshot 実在を確定する、
(2) 最新 snapshot を scratch PVC へ復元 (本番 PVC 不触)、
(3) integrity_check と行数照合・checksum 照合、
(4) 復元データで vaultwarden サーバを scratch 起動しログイン画面 (readonly) まで確認、
(5) restore-summary.json に結果を書き docs/backup.md の復元手順に反映、までが本体。

## 設計方針

### 前提 (initializer が 2026-08-24 に実読・実測した。調べ直さなくてよい)

- **復元対象**: vaultwarden の restic リポジトリは `b2:$(RESTIC_B2_BUCKET):vaultwarden`
  (apps/vaultwarden/restic-backup-cronjob.yaml)。backup は毎日 03:40 JST (`vaultwarden-restic-backup`)。
  snapshot は「PVC (db.sqlite3系・icon_cache を除く) + initContainer が Online Backup API で
  一貫コピーした db.sqlite3」の 2 パスを 1 本にまとめたもの。`rsa_key*.pem`・`attachments/`・
  `config.json` も含まれる。P-0111 で B2 の download cap 超過 (毎日 00:00 UTC リセット) による
  backup 失敗実績あり。
- **credential**: vaultwarden ns の Secret `vaultwarden-restic-backup-credentials`
  (ExternalSecret apps/vaultwarden/restic-external-secret.yaml、append-only 鍵
  `B2_ACCOUNT_ID_APPEND_ONLY`/`B2_ACCOUNT_KEY_APPEND_ONLY` = listBuckets/listFiles/readFiles/writeFiles)。
  **restore は読み取り (readFiles) で完結し、P-0341 の実測どおり削除権限つき鍵
  (`vaultwarden-restic-credentials`) は持ち出さない。** ただし spec の dod は「backup と同一の
  restic credential (restic-external-secret.yaml) を使う」と指示しており、参照先 Secret は
  backup CronJob と同じ `vaultwarden-restic-backup-credentials` で確定 (append-only 鍵で足りる)。
- **restore の権限要件**: docs/backup.md (T-0071 / P-0047) の教訓どおり、restic restore は
  CHOWN / FOWNER / DAC_OVERRIDE の 3 capability が必要 (backup 側の DAC_READ_SEARCH では足りない)。
  scratch への復元でも所有権・タイムスタンプ復元で同様。再実行時は前回の残留を `rm -rf` で
  先に掃除する。
- **scratch**: 本番 PVC `vaultwarden-data` には触れない。使い捨ての PVC か emptyDir を復元先に
  使い、検証後に掃除する (T-0071 系の検証専用 PVC/Job は確認後に削除するのが docs/backup.md の流儀)。
- **起動確認**: 復元した sqlite で vaultwarden コンテナを scratch 起動し、HTTP のログイン画面が
  serve される (readonly) ことを確認する。stale な WAL/-shm を持ち込まない (docs/backup.md の復元時注意)。
- **capabilities**: `kubectl-write` のみ。backup CronJob 起動・使い捨て Job/PVC の作成は kubectl CLI
  (MCP は read-only) で行う。`touches_apps: false` のため apps/ の manifest は変えない。

### 決めてあること

- **経路は「Git で manifest を足す + kubectl CLI で実行」ではなく、worker の使い捨て Job 経由を
  基本とする**。spec は `ops/vaultwarden_restore.py` をリポジトリに置くことだけを要求しており、
  実行は autopilot 環境 (in-cluster Pod、restic/kubectl 同梱、B2 credential はなし) から
  `vaultwarden-restic-backup-credentials` を参照する形で行う。B2 の削除系操作・Doppler の
  credential 変更は一切しない。
- **完了判定は verify 4 項目の実測**。restore-summary.json には
  `integrity` (`PRAGMA integrity_check` 結果)、`rows` (db.sqlite3 の主要テーブル行数)、
  `duration_seconds`、`transferred_bytes`、`target` (= scratch) を含める。
- **docs/backup.md の復元手順節に、実測値を基にした vaultwarden 復元手順を書いて戻す** (dod 5)。
  vaultwarden 節 (2026-08-06 の restore-verify) は「復元できた」だけで「ログイン画面が立ち上がる」は
  未確認だったため、そこを埋める形にする。

## やらないこと

- **本番 PVC `vaultwarden-data` への書き込み・復元先の本番化**。復元は scratch に限定
  (verify の `grep 'scratch'` が検査)。本番への切替 (リストア運用) は本プロジェクトの外。
- **apps/ 配下の manifest 変更** (`touches_apps: false`)。backup/retention CronJob・ExternalSecret・
  deployment には触れない。
- **retention (forget --prune) や削除権限つき鍵 `vaultwarden-restic-credentials` の使用**。
  restore は append-only 鍵の読み取りで完結させる (P-0341 の結論)。B2 上の snapshot の削除・
  プルーニングは行わない。
- **Doppler の credential 発行・変更・ローテーション**。物理作業を伴う人間専有 (CHARTER §4)。
- **vaultwarden のバージョン更新・設定変更**。週次点検 (vaultwarden のみ) の論点と混ぜない (1 PR 1 論点)。
- **immich / coder / syncthing への復元手順の展開**。本プロジェクトは vaultwarden だけの実測。
- **ops/backlog.json / ops/state.json / ops/journal/ の更新**。heart が直接 push する領域で
  コンフリクトする (CLAUDE.md)。