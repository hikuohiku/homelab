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

## セッション 2 (2026-08-23)

### やったこと — verify #3 (integrity CronJob を 4 namespace / 5 repo 分)

- `apps/{vaultwarden,immich,coder,syncthing}/restic-integrity-cronjob.yaml` 新設。
  各ファイル: script ConfigMap (`restic_integrity.py` 正本コピー + `run_integrity.py`
  ドライバ) + records ConfigMap (事前作成・空) + SA/Role/RoleBinding
  (`restic-integrity`, configmaps get/update のみ resourceNames 制約) + CronJob。
  coder は 1 ファイルに 2 CronJob (postgres / workspace-homes) で script CM・SA・
  Role は共有。kustomization 4 件に配線済み
- ドライバの流れ: plan() → `restic snapshots` 失敗なら未初期化扱いで skip (retention
  既存型。記録は書かない) → `restic check --read-data-subset=$SUBSET` 1 回で metadata+
  部分読み → **非ゼロは握り潰さず SystemExit** (ArgoCD Degraded → briefing/incident 経路)
  → 成功時のみ記録 {date, slot, subset, cycle, packs_read} を merge/trim して
  coverage_from_records() と一緒に `<repo>-integrity-records` CM の report.json へ PUT
- **設計メモ 1 からの変更 (理由付き)**: 「python initContainer が KEY=VALUE を書き、
  restic 本体コンテナが読む」方式ではなく、**restic バイナリを initContainer で emptyDir
  に退避させ (静的リンク Go バイナリなのでイメージをまたげる)、python 本体コンテナから
  subprocess で呼ぶ** 方式にした。busybox wget は K8s API の PUT を送れないため restic
  コンテナ側に記録書き込みを持たせられない (= レポート畳み込みの産出側が作れない)。バイナリ
  退避なら plan→check→記録が 1 プロセスフローに収まり、失敗時の意味論も素直になる。
  契約 (N/T 形式・正本同梱・drift 検査・成功時のみ記録) はすべて維持
- スケジュール確定 (JST 評価。分散表は各 yaml ヘッダにも記載):
  vaultwarden 毎月 2 日 14:20 / immich 6 日 13:40 / coder-postgres 10 日 15:10 /
  ws-homes 14 日 13:30 / syncthing 18 日 14:50。「第 n 曜日」表現は使わず固定日
  (k8s cron の DOM+DOW 併用は OR 意味論で nth-weekday を書けないため)。
  午後帯 = UTC 04-06 時台で 00:00 UTC リセット直後の予算を使い、日次 backup 帯
  (02:45-03:55) とも retention 帯 (日曜 03:45-04:50) とも時刻帯が離れる
- LEDGER_RULES に 5 エントリ登録 (根拠コメント付き): vaultwarden 35651584 /
  immich 153092096 / coder-postgres 138412032 / coder-workspace-homes 1396703232 /
  syncthing 34603008 bytes。式は「index/config 読み 32 MiB + 総量 ÷ 3」で総量は
  docs/backup.md の実測 (T-0066 pvc_usage / T-0117 手動 Job / T-0071 復元) から。
  download_ledger.py ブロック自体は触っていないので sync check は通過し続けている
- `ops/check_restic_integrity_script_sync.py` 新設 + ci.yml に登録:
  run_integrity.py は 4 ファイル同一、restic_integrity.py は 4 ファイル同一**かつ
  正本 ops/tools/restic_integrity.py とも一致** (二重管理の drift 検査)
- `ops/tests/test_restic_integrity_manifest.py` 新設 14 テスト: schedule 分散方針
  (月次固定日・13-15 時台・5 本で日重複なし) / env↔Role resourceNames↔事前作成 CM の
  3 箇所一致 / LEDGER_RULES 登録値 / kustomization 配線 / 埋め込みドライバ純関数
  (AST 抽出。非 dict 混入でも落ちないことを含む)
- 自己実測: verify #1 OK / verify #2 OK (30 tests) / verify #3 = 'read-data-subset'
  を含む apps/**.yaml が **8 ファイル** (python 全文走査で実測。サンドボックス BusyBox
  grep は相変わらず --include 非対応なので CI GNU grep 前提で 8 ≥ 4 で green) /
  verify #4 は未着手 (rc=1 のまま)。全体回帰 `unittest discover -s ops/tests -t .`
  = **333 tests OK** (319 + 14)。consistency checks 全部 + validate.py も green
  (validate の warning 11 件は既存分)

### 設計の確定事項 (#4 の reporter はこの契約を読むこと)

- records ConfigMap 名: `<repo>-integrity-records` (vaultwarden-integrity-records /
  immich-… / syncthing-… / coder-postgres-… / coder-workspace-homes-…)。
  report.json キー 1 個に payload 全体
- payload 形: `{generated_at, namespace, repo, records: [{date, slot, subset, cycle,
  packs_read}], coverage: <coverage_from_records() の戻り値そのもの>, last_check}`。
  reporter 側は records を集めて `coverage_from_records(repo, records, today)` を
  自前で呼んでもよいし、coverage をそのまま載せてもよい
- rbac.yaml (ops-health-reporter) へ追加すべき resourceNames: 上記 5 名
- 失敗時は Job 非ゼロ終了 → ArgoCD appTree health → briefing/incident (既存経路。
  DoD(2) ここで充足済み。reporter 側の追加対応は不要のはずだが、applications 収集の
  health 反映は既存どおり)

### 分かったこと / 罠

- check_download_ledger_script_sync.extract_block_scalar は戻り値の**先頭にキー行自身の
  残り (空行)** を含む。ledger 同士の比較では打ち消されるので今まで顕在化しなかったが、
  正本ファイルとの比較では dedent 後に lstrip("\\n") が必要
- k8s cron は DOM と DOW を両方制限すると OR になる (POSIX 意味論)。「毎月第 n 曜日」
  は書けない → 固定日 + 時刻帯での分離にした
- python:3.14-alpine を root で動かす前提なので restic の cache ($HOME/.cache) は
  container layer に書かれ、emptyDir は読み取り専用マウントで足りる
- サンドボックスに ruff/pip/kustomize/helm は無い。F821 は CI 専用項目として残る
  (新規 py ファイルは ast.parse で構文確認済み)。kustomization の resources 存在
  検査は python で代替実施

### 発見 (spec 外。curriculum が拾うもの)

- coder-workspace-homes の推定読み ≈1300 MiB/回 は retention 1 回分の推定 (512MiB-1GiB)
  を上回る全 integrity 中の最大所。docs/backup.md の「B2 無料枠 10GB」記述と cap 実値が
  人間コンソールで判明した際、LEDGER_RULES 差し替え + 必要なら PROJECT.md の調整方向
  (T 増やし & 頻度上げ) の検討材料になる。integrity 自体はストレージを消費しない
- schedules が JST 評価であることの根拠 (node01 time.timeZone) は各所コメントで言及
  されているが spec.timeZone 明示ゼロの状態が続いている。クラスタ移行等で崩れる可能性

### 次のセッションへの一言

verify #4: ops-health-reporter report.py への畳み込み + ops-health-reporter/rbac.yaml の
resourceNames 追加 (名前は上の契約)。collect_integrity() 相当を collect_download_budget()
流儀 (産出側/集約側分割、import 副作用のある report.py 本体は unit test から直接ロード
しない) で。これで verify 4 点が全部 green になるはず。
