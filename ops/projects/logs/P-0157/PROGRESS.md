# P-0157 PROGRESS

後続セッションは PROJECT.md とこのファイルと git log だけを文脈として引き継ぐ。
やったことをここに残す。ここに書かなかったことは存在しなかったことになる。

## 2026-08-23 initializer

- PROJECT.md / PROGRESS.md を作成。verify 3 項目とも failing を実測 (rc=1 × 3:
  ModuleNotFoundError / AssertionError / grep 該当なし)。実装は未着手。

## 2026-08-23 session 2 (worker)

### やったこと

verify #1 (test module) を軸に、測定パイプライン全体を実装した。commit 01ba8462。

- `apps/ops-health-reporter/backup_freshness.py` 新設: 純関数のみ
  (`build_report(cronjob_items, job_items, now, warn_hours)`)。成功時刻は
  **主系統 (b) CronJob status.lastSuccessfulTime**、副系統 (a) Complete=True の子 Job
  completionTime max。GC 耐性の根拠 (workspace-home 子 Job ttl=3600s) はモジュール冒頭に記録済み
- report.py へ `collect_backup_freshness()` を配線 (latest.json の `backup_freshness` キー)。
  CronJob/Job 一覧取得が失敗したら collect() 経由で**全体**を error にする
  (片方だけ欠けると全経路が偽の error エントリになるため)
- rbac.yaml: ClusterRole に batch cronjobs/jobs get/list を追加
- rules.json: `"backup_freshness": {"warn_hours": 72}` 追加
- `ops/tests/test_backup_freshness.py`: 33 テスト (両方向固定)。repo 全体 276 テスト green、
  validate.py 0 error、check_health_reporter_target.py ok も実測

### DoD(1) の選定と理由 (PR 本文にも転記すること)

「既存 reporter の拡張」を採択。独立 CronJob (P-0128 型) だと新 SA + 4 namespace 分 Role +
GitHub token secret + latest.json 別 writer (P-0126 実測の衝突リトライ・壊れ JSON 復旧) が
全部要る。reporter は既に 30 分毎にクラスタを読んで latest.json を上書きしているので、
ClusterRole への読み取り専用 batch 追加 1 ブロックで閉じる。増権は get/list のみ。

### 分かったこと / 実測した罠

1. **configMapGenerator で `../../ops/rules.json` を embed する案は不採用になった**。
   実際に `kubectl kustomize apps/ops-health-reporter` を打ったら load restrictor で
   拒否された (`file is not in or below ...`)。ArgoCD 既定の LoadRestrictionsNone を
   信じる変更は reporter 全体の sync を壊しかねないので、**report.py が GitHub Contents API
   で base ブランチ (main) の rules.json を読む**形に変更した (get_raw_content() を再利用)。
   注意: ops-health-report ブランチ上の rules.json は分岐時点で凍結されるので読まないこと。
   閾値取得失敗時は backup_freshness.DEFAULT_WARN_HOURS (=72、rules.json と同値を
   コメントで結合管理) にフォールバックし、測定自体は落とさない
2. 換算は丸め前に判定すること: 71.9999h は round(…, 2) で 72.0 になるので、丸め値で
   judge すると境界誤発報する (テスト test_status_uses_raw_hours_not_rounded_value で固定)
3. Failed Job にも completionTime は付く。Complete 条件で絞らないと「失敗し続けている」を
   「新鮮」と誤報する (テストで固定)

### verify 現状

- [x] #1 `python3 -m unittest ops.tests.test_backup_freshness` — rc=0 実測
- [ ] #2 health ブランチ latest.json — merge → ArgoCD sync → reporter 次回実行後 gate。
      ローカルでは永遠に green にならない (PROJECT.md 冒頭どおり)
- [x] #3 `grep -qE 'backup_fresh' ops/rules.json` — rc=0 実測

### 次のセッションへの一言

未実施は DoD(2) の heart 注記配線 (facts.budget_alert 同型:
ops/heart/facts.py に backup_freshness_alert() を作り heart.py L336/L413 付近の
budget beat と同じ形で cursors キー (例: backup_freshness_alert) を足す。テストは
ops/heart/tests/test_budget_alert*.py を写す)、DoD(5) の初回実測表
(merge 後に `git show origin/ops-health-report:ops/health/latest.json` から
backup_freshness を拾って initial-freshness.md に取得時刻付きで書く)、そして
#2 の merge 後確認。rules.json は人間レビュー必須パスなので auto-merge は期待しないこと。

## 2026-08-23 session 3 (worker)

### やったこと

DoD(2) の heart 注記配線を実装した (budget_alert 同型、新規通知チャネルは作らない)。

- `ops/heart/facts.py` に `backup_freshness_alert(doc)` と
  `backup_freshness_alert_due(alert, prev, today)` を追加 (budget_alert /
  budget_alert_due の直後に置いた)
- `ops/heart/heart.py`: budget beat と同じ形で cursors キー
  `backup_freshness_alert` を配線。流路は既存 2 本だけ:
  briefing-queue.jsonl (`source="backup-freshness (warn)"`) と incident 通知。
  cursors への書き込みは save_cursors より **前** (P-0128 レビュー指摘の順序契約)。
  metrics.jsonl に `backup_fresh_warn_count` を追加
- テスト: `ops/heart/tests/test_backup_freshness_alert.py` (12 tests) +
  `test_backup_freshness_beat.py` (3 tests, 実物 Heart.beat() をパッチして回す
  結合テスト。cursors の save 前書きを崩すと即落ちする断言を含む)

### 設計判断 (記録)

1. **no_data / error / unconfigured は鳴らさない** (warn のみ抽出)。DoD(2) が求めるのは
   「閾値の超過」の注記であって「測定できていない」ことではない。特に CronJob 再作成直後は
   lastSuccessfulTime が空になり no_data になる — これを即鳴きすると修復作業そのものが
   誤報を出す。静停止 (#49 型) は lastSuccessfulTime が 72h を跨いだ時点で warn に
   なるのでこの経路で捕まる
2. **due() の抑制単位は stale_repos の集合**。budget は status 変化 (warn→exceed) で
   再鳴するが、鮮度には段階が無いので「集合が変わったら」(warn 経路の増加・回復どちらでも)
   同日でも再通知に倒した。「増えた」は新しい情報であり、「回復」も 1 回の追加通知として
   可視性に寄与する (集合が戻る発振は日次周期では起きない想定)。日付が変われば同じ集合でも
   再度鳴らす (毎日の確実な可視性。budget_alert_due 同型)
3. reason 文 (`coder-restic-backup (80.5h)、…` 形) は facts 側で組み立てた。
   reporter は warn 行に detail/reason を持たないため (error/no_data 専用)

### verify 現状

- [x] #1 `python3 -m unittest ops.tests.test_backup_freshness` — rc=0 実測 (34 tests)
- [ ] #2 health ブランチ latest.json — merge → ArgoCD sync → reporter 次回実行後 gate。
      ローカルでは永遠に green にならない (変化なし)
- [x] #3 `grep -qE 'backup_fresh' ops/rules.json` — rc=0 実測
- 追加実測: 新規 15 tests green / `ops/heart/tests` 全体 211 tests OK /
  repo 全体 discover (`unittest discover -s ops -t .`) 523 tests OK /
  `ops/validate.py` 0 error

### 次のセッションへの一言

実装系はすべて完了 (session 2 の測定パイプライン + 本 session の注記配線)。
残りは **merge 後にしかできない 2 作業** だけ:

1. verify #2 の gate 確認: merge → ArgoCD sync → reporter の次回実行 (30 分毎) を待ち、
   `git show origin/ops-health-report:ops/health/latest.json` で backup_freshness が
   >=5 要素載ったことを確認
2. DoD(5): 同じ latest.json から 5 経路の現鮮度を拾い、取得時刻付きで
   `ops/projects/logs/P-0157/initial-freshness.md` に初回実測表として書く

どちらも merge 前には絶対に進めないので、それ以外の作業は無い。rules.json は人間レビュー
必須パスなので auto-merge は期待しないこと (session 2 と同じ)。heart 注記の実環境での
発報確認はしなくてよい (warn になるまで数日かかる。fixture で両方向固定済み)。
