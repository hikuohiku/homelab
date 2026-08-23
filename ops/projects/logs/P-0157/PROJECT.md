# P-0157 — #49 は「失敗すら起こらない」形で再発する — 全 restic リポジトリの『最後の成功から何時間』を毎時測り、静停止を最初の 1 時間で捕まえる

## 目的

B2 cap 事故 (08-10 夜・08-22 夜) では backup 子 Job の失敗がアプリを赤くしたので ArgoCD
Notifications (P-0139) が鳴らせる。しかし本当に怖いのは「新しい Job が生まれない」静停止 —
オーケストレータ CronJob 自身が死ねば失敗も Degraded も起きず、全アプリは緑のままバックアップだけが
止まる (#49 同型、heart 9 日沈黙同型)。既存の歯止めは静的 (test_backup_coverage.py は manifest の
存在しか見ない) か vaultwarden 限定か一回きりで、coder-pg / immich / coder-workspace-home /
syncthing の 4 リポジトリには「今朝成功したか」を見る者がいない。health reporter が毎回クラスタを
読んでいるので、観測の器に鮮度計を足すだけで閉じる。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0157` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 -m unittest ops.tests.test_backup_freshness`
  — 換算 (最終成功時刻 → hours_since_success) と閾値判定を fixture で両方向固定するテスト
  モジュールが存在し、通ること。
  実測 rc=1 (`ModuleNotFoundError: No module named 'ops.tests.test_backup_freshness'`)。
- [ ] `git fetch origin ops-health-report -q && git show origin/ops-health-report:ops/health/latest.json | python3 -c "import json,sys; f=json.load(sys.stdin).get('backup_freshness'); assert isinstance(f,list) and len(f)>=5 and all(('repo' in x and 'hours_since_success' in x) for x in f)"`
  — health ブランチの latest.json に `backup_freshness` (>=5 要素、各要素に `repo` と
  `hours_since_success`) が載ること。実測 rc=1 (AssertionError — キー未存在)。
  **merge → ArgoCD sync → 次回 reporter 実行 (30 分毎) の後に初めて判定できる gate** であり、
  ローカルでは永遠に green にならない。判定タイミングは soak (rules.soak.minutes=30) と同じ考慮で
- [ ] `grep -qE 'backup_fresh' ops/rules.json`
  — 閾値が運用パラメータの単一情報源 (rules.json) に置かれること。
  実測 rc=1 (`backup_fresh` を含む行は repo 全体に無いことを grep 済み)。

verify は DoD の下限であって DoD そのものではない。DoD(1) の「選んだ理由を記録する」、
DoD(3) の動的 PVC 扱いの明記、DoD(5) の初回実測表は verify が見張らない — PROGRESS.md と
成果物に証跡を残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した。調べ直さなくてよい)

- 5 経路の実体 (apps/ 配下実読)。**すべて日次 CronJob (= 通常周期 24h)**:

  | repo | namespace / CronJob | schedule |
  |---|---|---|
  | vaultwarden | vaultwarden / `vaultwarden-restic-backup` | `40 3 * * *` |
  | coder-postgres | coder / `coder-restic-backup` | `10 3 * * *` |
  | immich | immich / `immich-restic-backup` | `45 2 * * *` |
  | coder-workspace-home | coder / `coder-workspace-home-backup` | `30 3 * * *` |
  | syncthing | syncthing / `syncthing-restic-backup` | `55 3 * * *` |

- 「最後の成功時刻」の読み手は 2 系統ある: (a) 子 Job の `completionTime` (Complete=True の
  max)、(b) CronJob の `status.lastSuccessfulTime`。(b) は子 Job が GC された後も残る実績がある
  (T-0117、journal run #183/#205 で 2026-08-06 実測)。workspace-home の子 Job (chb-*) は
  `ttlSecondsAfterFinished=3600` で 1 時間で消えるため、(b) のほうが静停止の観測点として頑健。
  どちらを読むにも batch API の権限が要る
- **reporter SA (`ops-health-reporter`) は現状 batch API を一切読めない**
  (apps/ops-health-reporter/rbac.yaml 実読: pods/pvcs/nodes/applications/metrics/
  configmaps(resourceNames 2 個)/deployments の get,list のみ + autopilot ns 封じの pods/log)。
  つまり「既存 reporter の拡張」なら ClusterRole への batch 追加が、「独立 CronJob」なら新 SA +
  Role の作成が**必ず発生する — RBAC を一切増やさない選択肢は存在しない**。これが DoD(1) の
  実測と理由記録が必須である理由
- 先行例は 2 つ。P-0128 (download-ledger): 各 ns の専用 SA+Role (自 ns jobs get/list) が
  ConfigMap `download-budget` に完了 Job を書き、reporter が集約。ただし帳簿の日付は
  **YYYY-MM-DD 刻み**で時間鮮度には粗すぎる上、スクリプトは 4 ファイル同一で
  `check_download_ledger_script_sync.py` (CI) が drift を検査する — 手を入れると 4 ファイル
  同時修正が必須。P-0126 (version-watcher): reporter 外から latest.json へ書く別 writer の
  先例で、merge + 衝突リトライ (409/422) + 壊れた JSON の復旧という負担の実績が
  P-0126 PROGRESS にある
- reporter 本体は 30 分毎 (`*/30 * * * *`, apps/ops-health-reporter/cronjob.yaml) に latest.json
  を上書きする。「毎時測る」に対して十分な分解能
- rules.json の validate.py `check_heart_config` (L344-362 実読) は既知キーの型しか見ず、
  未知セクションの追加で落ちない。**ただし rules.json は ruleset の人間レビュー必須パス**
  (ファイル冒頭 _comment) — auto-merge を期待しない
- 「autopilot 注記に上げる」の確立パターンが `ops/heart/facts.py` の `budget_alert()` /
  `budget_alert_due()` (P-0128): heart が各ビートで latest.json を読み、warn 以上のときだけ
  抽出して briefing/Discord 予算経路に乗せる。新規通知チャネルは作らない

### 作り方

1. **最初に実装形を決めて記録する** (DoD(1)): reporter 拡張 (rbac.yaml の ClusterRole に
   batch cronjobs/jobs の get/list を追加) か、独立の読み取り専用 CronJob (P-0128 型の
   namespaced Role) か。`kubectl auth can-i --as=system:serviceaccount:ops-health-reporter:ops-health-reporter ...`
   等で reporter SA の現権限を実測し、上の前提 (帳簿の日付粒度・4 ファイル同期制約・別 writer の
   衝突負担) と突き合わせて選び、理由を PROGRESS.md と PR 本文に書く。成功時刻のソースも
   (a)/(b) から選んで同じ場所に記す (GC 耐性の根拠付きで)
2. latest.json の `backup_freshness` は 5 要素以上のリストとし、各要素を最低
   `{repo, namespace, cronjob, last_success_at, hours_since_success, status}` 形にする
   (verify #2 が要求するのは `repo` と `hours_since_success`)。`status` は rules 閾値との
   比較で ok/warn。収集できなかった経路は黙って落とさず error エントリとして載せる
   (collect_download_budget() と同じ思想)
3. rules.json に閾値セクション (例: `"backup_freshness": {"warn_hours": 72}` = 通常周期
   24h の 3 倍) を置き、reporter 側はここだけを読む
4. 換算・閾値判定は純関数に切り、合成入力で両方向 (落ちること / 通ること) を
   `ops/tests/test_backup_freshness.py` に固定する (test_backup_coverage.py の流儀)。
   CI は ubuntu-latest + python3 stdlib のみ
5. DoD(3): coder-workspace-home は PVC が動的 (`coder-<workspace-id>-home`、manifest が
   リポジトリに存在しない) なので個々の PVC を見ず、**オーケストレータ CronJob
   `coder-workspace-home-backup` 自身の最終成功で代用する**旨を成果物に明記する
   (T-0078/T-0117 が同じ代用をやった前例あり)。測定単位が元々 CronJob なら自然に成立する
6. DoD(5): 初回の実測表 (5 リポジトリの現鮮度) を `ops/projects/logs/P-0157/initial-freshness.md`
   に残す。取得方法 (kubectl 実測 or 初回 reporter 出力) と取得時刻を添える
7. merge 後: ArgoCD sync → reporter の次回実行を待ち、health ブランチの latest.json で
   `backup_freshness` が実際に載ったことを実測して PROGRESS.md に証跡を残す (soak 30 分)。

## やらないこと

- **自動修復**。spec が明示的に禁じている (「自動修復はしない」)。CronJob の再起動・B2 cap の
  対策・schedule 変更はしない。注記 (latest.json への出力と heart 経由の浮上) まで
- **retention (forget --prune) 経路の鮮度監視**。対象は 5 つの backup 経路のみ。retention の
  失敗は即データ損失ではなく、download_budget 帳簿が既に完了実績を見ている
- **pod_issues の鮮度化 / job_failures 分離**。P-0061/P-0133 (不採択) の論点で、backup 鮮度とは
  別 PR に分けるべき改造。report.py の既存 collector の判定は触らない
- **Discord / ArgoCD Notifications 配線の新設**。budget_alert 同型で既存経路に乗せるだけで、
  通知チャネル・通知ルールは追加しない
- **免除済み PVC (openclaw-state 等) への backup 新設**、アプリ内蔵 backup (immich 内蔵 dump 等)
  の監視への取り込み。K8s CronJob 由来の 5 経路が対象
- **B2 download cap 問題自体の解決**。P-0111/root_cause.md と download_budget (P-0128) の
  領域。本プロジェクトは「止まったことに気づけるようにする」まで
