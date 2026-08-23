# P-0187 PROGRESS

> 後続セッションはここに追記する。PROJECT.md と git log だけが引き継ぎ文脈なので、
> やったこと・判断と理由・verify 実測値をここに残すこと。

## セッション 1 (2026-08-23)

### やったこと — verify #1・#2 (回転選択ロジックの正本 + unittest 固定)

- `ops/tools/restic_integrity.py` 新設 (純関数のみ・標準ライブラリのみ・import 副作用なし)。
  関数: `slot_for_date()` / `repo_offset()` / `subset_arg()` / `plan()` /
  `coverage_from_records()` / CLI (`python3 ops/tools/restic_integrity.py plan --repo X`)
- `ops/tests/test_restic_integrity.py` 新設: **30 テスト**。
  `from ops.tools import restic_integrity as ri` の直接 import
  (test_version_watch.py 流儀。download_budget の importlib 方式は不要だった)
- 自己実測: verify #1 rc=0 / verify #2 OK (30 tests)。**全体回帰も実施:
  `unittest discover -s ops/tests -t .` = 319 tests OK** (既存に影響なし)
- commit db0d22283

### 設計の確定事項 (次のセッションはこの契約を壊さないこと)

- スロット式: `((year*12+month-1) + sha256(repo_id)[:8バイト big endian] % T) % T + 1`、
  T=3 月次。キーは **UTC 日付**。同じ月内なら日付によらず同スライス → 当月内のリトライや
  実行日ずれで読む場所が変わらない (再実行可能性の中身)
- restic へ渡すのは **N/T 形式のみ**。パーセント形式は毎回ランダム抽出で回転にならない
- カバー率の記録契約: integrity Job は**成功時のみ** `{date: "YYYY-MM-DD", slot: N}` を
  専用 ConfigMap へ書く。集計側 `coverage_from_records(repo, records, today)` は記録の日付から
  `slot_for_date()` で期待値を再導出し、一致しない記録は inconsistent として採らない
  (書き間違いでカバー率が盛れない)。未来日 → skew 扱い、窓外 → 除外。
  セッション 3 (reporter) はこの関数を使う
- ゴールデン値 (@2026-08, T=3): vaultwarden slot2 / immich slot3 / coder-postgres slot2 /
  coder-workspace-homes slot2 / syncthing slot1。offset は順に 0/1/0/0/2

### 次のセッション (#3: CronJob 実装) への設計メモ

1. **restic/restic:0.19.1 イメージに python は無い**。スロット計算は python:3.14-alpine の
   initContainer で `restic_integrity.py plan --repo X` を叩いて KEY=VALUE 行
   (SUBSET=…/SLOT=…) を共有 emptyDir に書かせ、restic 本体コンテナが読む
   (vaultwarden の sqlite-snapshot initContainer と同じ構図)。
   正本の ConfigMap 埋め込みコピーと ops/tools/ 側の drift 検査を新設すること
   (check_download_ledger_script_sync.py 流儀の拡張が楽) — 未着手
2. Job 本体: `restic check --read-data-subset="$SUBSET"` の 1 コマンドで metadata+data 部分読み。
   credential は既存 `<app>-restic-backup-credentials` (PROJECT.md 前提どおり)。
   repository not initialized 時の skip 句 (`restic snapshots || exit 0`) は retention 側に
   既存の型があるので踏襲。`set -eu` 必須 (失敗握り潰し禁止)
3. **LEDGER_RULES への登録を忘れない** (PROJECT.md 注意書きどおり):
   vaultwarden / immich / coder / syncthing の download-ledger-cronjob.yaml に
   integrity CronJob 名 : 推定 bytes (= repo 総量 ÷ 3) を追加。coder ns は
   coder-postgres と coder-workspace-homes の **2 job 分**。推定根拠のコメント必須
4. スケジュール分散の実測値 (本セッションで再確認済み): 日次 backup は immich 02:45 /
   coder-pg 03:10 / ws-homes 03:30 / vw 03:40 / sync 03:55。retention は全 5 本が
   **日曜朝 UTC** (immich 03:45 / vw 04:00 / coder-pg 04:10 / ws-homes 04:30 / sync 04:50)。
   integrity は (a) apps 間で実行日を分散 (b) 日曜を避ける (c) 00:00 UTC リセットの
   予算内の時刻、の 3 条件で決める。月次 cron の「第 n 曜日」表現に注意
5. 成功時の記録書き込み先 ConfigMap 名を各 ns で決める必要あり。reporter 側 (#4) の
   rbac.yaml resourceNames 追加を忘れずに (PROJECT.md レポート畳み込み節)

### 分かったこと / 罠

- **bash の $((0x...)) は int64 wrap する**: sha256 先頭 16 桁が 0x7fff... を超える repo
  (coder-postgres / syncthing) で mod 計算が負になりゴールデン値を誤る。独立検証は
  `sha256sum + bc` の多倍長でやること (実際 1 度やらかして bc でやり直した)
- **repo_offset は cycle 依存** (sha256 % cycle)。T を変えると offset も変わり
  スロット表が全部変わる。テストフィクスチャを手計算するときは注意 (cycle=2 の
  テストで 1 庚ミスして実機確認で修正済み)
- wrapper サンドボックスの grep は BusyBox で `--include` 非対応 → verify #3 のコマンド列は
  実装後でもサンドボックスでは rc=1 になる (PROJECT.md 受入チェックリストの注記どおり、
  CI の GNU grep 前提)。find+xargs と全文検索の 2 経路での裏取りを次セッションで推奨

### 発見 (spec 外。curriculum が拾うもの)

- なし (autopilot-core に backup が無い件は PROJECT.md 前提に記録済み)

### 次のセッションへの一言

verify #3 (integrity CronJob を 4 namespace 分) が次。上の設計メモ 1〜5 がすべて。
その次 (#4) は report.py への畳み込み + rbac resourceNames。
