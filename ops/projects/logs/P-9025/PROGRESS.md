# P-9025 PROGRESS

## 2026-08-24（実装・実測完了）

### やったこと

- `ops/vaultwarden_restore.py` を作成。1 本で「最新 snapshot の直近性確認 (24h) → scratch へ
  restore → /data レイアウト組み立て → PRAGMA integrity_check + 全テーブル行数 → sha256 照合
  (`restic dump latest /staging/db.sqlite3`) → restore-summary.json 書き出し」まで完結。
  標準ライブラリのみ。restic バイナリは `RESTIC_BINARY` で注入。
- `ops/tests/test_vaultwarden_restore.py` を追加（フェイク restic による end-to-end。CI の
  `unittest discover -s ops/tests` に乗る）。全部で 541 tests OK。
- **実測（本番データ）**: vaultwarden ns に使い捨ての scratch PVC (`vaultwarden-restore-scratch`)
  + Job（initContainer で restic/restic:0.19.1 のバイナリを /tools へコピー、main は
  python:3.14-alpine、credential は `vaultwarden-restic-backup-credentials`=append-only 鍵、
  securityContext は CHOWN/FOWNER/DAC_OVERRIDE + drop ALL、runAsUser 0）を `kubectl apply` で投入。
- **結果**: snapshot `c51d21fa`（当日 03:40 JST の日次 backup、4h34m 前 → DoD(1) の 24h 以内、
  backup CronJob の追加起動は不要）を復元。integrity=`ok`、rows=863、users=1、
  duration=12s、transferred=1,832,910 bytes、checksum_match=true。→ `restore-summary.json` を
  `ops/projects/logs/P-9025/` に書いた。
- **起動確認 (DoD 4)**: 復元した /data で `vaultwarden/server:1.37.2-alpine` を scratch 起動。
  `GET /` が HTTP 200（`<title>Vaultwarden Web</title>` + `layout_frontend` = ログイン画面）、
  `/alive` 200、`/api/accounts/profile` 401。**初めて「開けて戻した」実績**。
- **docs/backup.md**: 復元手順節（実測値つき）を追記。検証用リソースは全削除済み。

### 分かったこと

- **append-only 鍵で復元は完結する**（readFiles）。削除権限つき鍵は使わなかった（P-0341 の結論
  どおり）。→ PROJECT.md の「backup と同一の restic credential」は `-backup-credentials` で確定。
- **復元した db.sqlite3 は root:root 0644 になる**（backup の staging 一貫コピーを root の
  initContainer が作るため）。それでもサーバ (uid 1000) は動く: `/data` ディレクトリを 1000:1000
  に chown すれば SQLite が WAL/-shm を書けるため。**本番切替時は db.sqlite3 を 1000:1000 に
  chown してから起動する**（docs/backup.md に明記）。
- 復元後の /data に stale な WAL/-shm は無い（backup 側で除外済み。起動時に新規作成）。

### 発見（スコープ外、curriculum へ）

- **vaultwarden-restic-backup CronJob が 403 `b2_download_file_by_name` で断続失敗**（2d3h /
  28h / 27h 前の 3 回 Error）。P-0111 の B2 download cap 超過と同型。当日 (03:40 JST) は成功して
  いたが、**日次 backup の信頼性はまだ P-0111 の緩和（cap 超過時に次サイクルで自愈するだけ）の
  まま**。download cap そのものを上げる・backup 時刻を cap リセット直後に移す等の恒久対策は
  未実施のまま。
- 上記 403 失敗は retention の `forget --prune` ではない（backup 側のみ）。

### 次のセッションへ

- 4 つの verify はすべて green（`test -f ops/vaultwarden_restore.py` / `test -f
  ops/projects/logs/P-9025/restore-summary.json` / integrity+rows+duration / grep scratch）。
- 残タスクは無い想定。wrapper が PR を出す。レビューで「restore-summary.json は
  kubectl logs から取った値」を指摘されたら、`restore-summary.json` と `docs/backup.md` の数値が
  同一であることを確認するだけで足りる。
- 罠: 復元 Job の main コンテナは `python:3.14-alpine`（sqlite3 標準ライブラリ）。restic バイナリ
  は initContainer から /tools/restic にコピー。再現するならこの構成をそのまま使うこと。