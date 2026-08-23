# P-0128 PROGRESS

## 2026-08-23 セッション 1 — verify #1 を green 化 (帳簿の集計ロジック)

### やったこと

- **verify #1 green**: `python3 -m unittest ops.tests.test_download_budget` → 23 tests OK
  (実測)。`ops/tests/` 全体 (`discover -s ops/tests -t .`) も 172 tests OK で regression 無し。
- 新設 2 ファイル:
  - `apps/ops-health-reporter/download_budget.py` — 純関数のみ。`sum_window()`
    (直近 N 日・UTC 日付単位の集計。壊れた記録は例外でなく skipped に数える)、
    `monthly_estimate()` (窓合計の比例外挿。データゼロで None)、`judge()` (閾値判定)、
    `build_report()` (ConfigMap 群 → latest.json の `download_budget` キーの中身)。
    import 副作用ゼロ (report.py と違い SA token を読まない) なので unit test から直接
    importlib ロードできる
  - `ops/tests/test_download_budget.py` — test_openclaw_bridge.py と同じ importlib
    ロード方式。境界 (warn_ratio ちょうど→warn、cap ちょうど→exceed、窓の両端) と
    不正入力 (bool/負値/文字列 bytes、未来日付=skew、非 dict) を両方向で固定
- verify #2 は**まだ failing** (未配線)。次セッションの最優先。

### 設計判断と理由 (次セッションは再考しないでよい)

1. **純ロジックを report.py から分離した**: report.py は import 時に ServiceAccount
   token を読むため cluster 外の unit test からロードできない。verify #1 の「集計ロジックが
   unit test として存在し、通る」を実現するには分離が必須だった。report.py 側の配線は薄くてよい
2. **判定軸は「1日あたり」のみ**: cap は毎日 00:00 UTC リセット (root_cause.md 実測済み)。
   「月次見積もり vs cap」(DoD(2) の文言) は daily_avg×30 vs cap×30 と数学的に同値なので
   判定軸を増やさず、月次値は monthly_estimate_bytes として見せるだけ + reason 文面に換算値を載せる
3. **cap の実値は決め打ちしない**: B2 コンソールにしか無いため DEFAULT_DAILY_CAP_BYTES=None
   が既定。None の間は status=unconfigured を正直に返す (ok/exceed を偽装しない)。
   実値判明時にここか呼び出し側へ設定する
4. **産出側の推定方法はこのモジュールの管轄外**: restic はダウンロードバイト数を表示しない
   (「Added to the repo」は upload 側)。推定は操作種別ごとのモデルになり、それを作るのは
   namespace 側の採集主体。このモジュールは「runs: [{date, job, bytes}]」という記録形式だけ契約する

### 次セッションへの一言 — verify #2 の配線手順 (目安)

1. `apps/ops-health-reporter/kustomization.yaml` の configMapGenerator files に
   `download_budget.py` を足す (現状 report.py のみ)。CronJob は `python /scripts/report.py`
   で起動するため sys.path[0]=/scripts となり、report.py からの `import download_budget`
   は追加マウント無しで通るはず (kustomize build で確認すること)
2. report.py に `collect_download_budget()` を足し、main() の report dict へ
   `"download_budget": collect(...)` で載せる。これで grep が green になる
3. RBAC 注意: `apps/ops-health-reporter/rbac.yaml` の ClusterRole は configmaps get が
   resourceNames `["pvc-usage-report"]` に絞られている。帳簿を **同じ名前** の ConfigMap の
   追加キー (例: `pvc-usage-report` の `download_budget.json`) にすれば RBAC 変更不要。
   別名 ConfigMap にするなら resourceNames 追加が要る

### 未解決の罠・開いた設計問答 (次セッション以降)

- **誰が runs を書くか未決** (DoD(1) の残り)。今セッションで実測した制約:
  - restic backup/retention CronJob の Pod は `automountServiceAccountToken: false` で
    SA/RBAC が無い → 自分で ConfigMap に書けない
  - pvc-usage-reporter は pods/log 権限が無い → restic Job ログを読めない
  - よって現実解は「各 ns に pods/log get (自 ns のみ) + configmap update の Role を持つ
    小さな採集 CronJob を新設」か「pvc-usage-reporter に自 ns 分だけ pods/log を足す」。
    後者は T-0110 (pods/log は autopilot ns に閉じる判断) との整合をコメントで説明する必要あり
- **推定量モデル案** (根拠: PROJECT.md 決めてあること節): 日次 backup ≈ repo open 時の
  config/index 読み (小さな定数); 週次 retention (prune) ≈ index 再読み込み;
  将来最大消費者は P-0102 の週次 `restic check --read-data-subset=5%` ≈ リポジトリサイズ×5%
  (リポジトリサイズの proxy には既存の pvc_usage 実測 bytes が使える — 同じ ConfigMap にある)
- **DoD(3) の削減候補メモ**: 健康診断系で「メタデータだけで足りる」第一候補は
  `restic check --read-data-subset` の subset 率そのもの (データ読みは cap 直撃)。
  対象プロジェクト (P-0102/P-0116/P-0114/P-0115) は未稼働なので、削減は「設計上の配慮を返す」
  形になる (PROJECT.md やらないこと節どおり、そちらの実装には触らない)

### 発見 (スコープ外。curriculum が拾うこと)

- `ops/tests/test_backup_coverage.py` は PyYAML に依存している (CI の python 環境に
  入っている前提)。download_budget のテストは標準ライブラリのみで書いたが、
  ops/tests 配下に依存の有無が混在している
- report.py の history jsonl は 1 行 1 レポート全文で、`download_budget` を載せると
  1 行あたり数 KB 増える。現状の運用では問題ない規模だが、runs を生のまま全件載せると
  窓 7 日×5 リポジトリで膨らむ。build_report() は集約後の小さい形しか出さないので
  生 runs を latest.json に載せないこと (namespace ごとの daily/by_job 合計だけで十分)

## 2026-08-23 セッション 2 — verify #2 を green 化 (report.py への配線)

### やったこと

- **verify #2 green**: `grep -q 'download_budget' apps/ops-health-reporter/report.py`
  → 実測 rc=0。verify #1 も再実行で green (23 tests OK)。`ops/tests` 全体
  (`discover -s ops/tests -t .`) も 172 tests OK で regression 無し。**これで受入 2 項目とも green**
- 変更 2 ファイル:
  - `apps/ops-health-reporter/kustomization.yaml` — configMapGenerator の files に
    `download_budget.py` を追加 (report.py と同じ ConfigMap)。予想どおり `python /scripts/report.py`
    の sys.path[0]=/scripts で import は解決する。`kubectl kustomize` ビルド成功を実測
  - `apps/ops-health-reporter/report.py` — (a) `import download_budget` + cluster 外ロード用の
    sys.path フォールバック 1 行、(b) `DOWNLOAD_BUDGET_NAMESPACES = ["immich", "vaultwarden",
    "coder", "syncthing"]` (restic リポジトリのある ns。coder は 2 リポジトリが同 ns、syncthing は
    pvc_usage 対象外だが backup あり)、(c) `collect_download_budget()`: 各 ns の
    **pvc-usage-report ConfigMap の追加キー `download_budget.json`** を読み (RBAC resourceNames
    変更を避けるため既存名に乗せた)、`{"runs": [...]}` 形を検査して download_budget.build_report()
    へ渡す。キー無し/JSON 壊れ/runs 非リストは error エントリに倒し他 ns を止めない
    (collect_pvc_usage と同じ思想)、(d) main() の report dict に `"download_budget": collect(...)`
    を追加、(e) notes に帳簿キーの説明文を追記
- **産出側との契約を確定した** (report.py コメント + notes に記載): 各 ns の backup 側 CronJob が
  pvc-usage-report ConfigMap に `download_budget.json` キーで
  `{"runs": [{date: "YYYY-MM-DD", job: 名前, bytes: N}, ...]}` (UTC 日付) を書く
- cluster 外 smoke 実測: SA token 読みをモックして report.py を importlib ロード →
  k8s_get をスタブし collect_download_budget() 経由で集約形・error ns 分離・生 runs 非掲載を確認

### 設計判断と理由 (次セッションは再考しないでよい)

1. **集約対象 ns を pvc_usage と別定数にした**: PVC_USAGE_NAMESPACES に syncthing は無いが、
   帳簿の対象は「B2 ダウンロードを食う者」= restic リポジトリのある ns。RBAC は ClusterRole の
   resourceNames 制約なので新 ns の同名 ConfigMap 読みは追加権限不要
2. **キー欠損を error エントリにした** (空 runs 扱いにしない): 「産出側未稼働」と「稼働したが 0 バイト」を
   latest.json 上で区別できるほうが、DoD(1) の残り (誰が runs を書くか) を実装するときの観測点になる
3. **report.py の import に sys.path.append フォールバックを足した**: smoke 実測で、cluster 内起動では
   解決済みだが importlib での外部ロード時に `import download_budget` が ModuleNotFoundError になることを
   摘出。スクリプト自身のディレクトリをパスに足す 1 行で潰した (CI の check_health_reporter_target.py が
   将来ロード方式を変えても壊れないため)

### 未解決の罠・開いた設計問答 (次セッション以降)

- **⚠ 新発見: pvc-usage-reporter の put_configmap は data 全体を置換する**
  (apps/immich/pvc-usage-cronjob.yaml の put_configmap — GET は resourceVersion 取得のみに使い、
  PUT body の data には自分のキーしか入れない)。つまり帳簿産出側を「別 CronJob が同じ ConfigMap に
  追加キーで書く」設計にするなら、素直に PUT すると report.json を吹き飛ばす (逆も然り)。
  産出側は GET → data 辞書にマージ → PUT (resourceVersion 付き。真の競合は 409 で表面化) が必要。
  この置換セマンティクスがあるので、「別名 ConfigMap + RBAC resourceNames 追加」のほうが
  総合的に単純という可能性が出てきた。configmaps get の resourceNames 追加は T-0110
  (pods/log の閉じ込み) とは無関係なので整合問題はない。次セッションで決めてよい
- DoD(1) の残り = runs の産出主体。前セッション実測の制約は有効: restic CronJob Pod は SA 無し
  (automountServiceAccountToken: false)、pvc-usage-reporter に pods/log 権限は無い。
  候補は「小さな採集 CronJob 新設 (自 ns の pods/log get + configmap update)」or
  「pvc-usage-reporter に pods/log を足す」。推定量モデルの案 (日次 backup ≈ 定数,
  prune ≈ index 再読み, P-0102 の read-data-subset 5% ≈ repo サイズ×5%) も前セッション分が有効
- DoD(2) の警報受け口はまだ未実装。budget.status は latest.json に載るようになったが、
  これを見て briefing に乗せる配線は別途。PROJECT.md の前提どおり ops/briefing/ モジュールは
  存在しないので、既存経路 (heart の briefing-queue.jsonl or notify.py digest) から選ぶこと。
  cap 実値未設定の間は judge() が常に unconfigured を返すので警報は鳴らない —
  これは正直な挙動であり、配線だけ先に作って構わない (warn/exceed になったとき初めて鳴る)
- **環境の罠**: このサンドボックスの mktemp は busybox 系でテンプレート末尾の X 以外を許さない
  (`mktemp /tmp/x-XXXX.py` が Invalid argument。`mktemp` 引数なしなら通る)。/tmp/opencode への
  リダイレクトも Permission denied になった。一時ファイルは引数なし mktemp 推奨

### 発見 (スコープ外。curriculum が拾うこと)

- (前セッション分を引き継ぎ) PyYAML 依存の混在、history jsonl の肥大化注意。追加の新規発見なし —
  pvc-usage-reporter の data 置換セマンティクスは本プロジェクトの産出側設計に直接効くため
  上の罠節に置いた (curriculum へ回すほど汎用ではない)

## 2026-08-23 セッション 3 — レビュー指摘 3 点の解消 (産出側新設・契約先変更・警報配線)

### やったこと

レビュー指摘 3 点 (産出側不在 / pvc-usage-report との契約衝突 / 警報受け口不在) を
**全部**解消した。verify 2 項目とも再実行で green (23 tests OK, grep rc=0)。
`ops/tests` 全体 172 tests OK、`ops/heart/tests` 全体 OK (budget_alert の新規 10 tests 含む)、
`ops/check_download_ledger_script_sync.py` 新設して CI (ci.yml consistency checks) に足した。

- **指摘 2 (契約衝突) → 専用 ConfigMap `download-budget` への変更で解消**。
  前セッションで保留していた「別名 ConfigMap + RBAC resourceNames 追加」を採用。
  report.py の `collect_download_budget()` は `/configmaps/download-budget` の
  `report.json` キーを読むように変え、rbac.yaml の configmaps get の resourceNames に
  `"download-budget"` を追加。report.py のコメントに「pvc-usage-reporter が PUT で data
  全体置換するので追加キー契約は吹き飛ぶ」という理由を書き残した (次セッションは再考しないこと)
- **指摘 1 (産出側不在) → 各 ns に download-ledger CronJob 新設** (immich/vaultwarden/coder/
  syncthing の 4 ファイル、埋め込みスクリプトは完全同一 — 同期チェックが CI で担保)。
  設計: 自 ns の Job 一覧を batch API から取得し、Complete かつ KEEP_DAYS(14日) 内のものを
  ownerReferences から親 CronJob 名へ解決 → env `LEDGER_RULES`
  (`cronjob名:bytes,...`。スクリプト内に既定値は持たない) で推定 bytes を付与 →
  既存帳簿と id (=Job 名) でマージ・trim して ConfigMap へ GET→resourceVersion 付き PUT。
  pods/log は読まない (T-0110 の閉じ込みを広げない)。Role は jobs get/list +
  configmaps get/update (resourceNames 制約)。対象 ConfigMap は manifest で事前作成し
  create 権限を持たせない (消されたら 403 で目立って落ちる)。runAsUser 65534・特権なし。
  スケジュールは毎時 :25 (health-reporter :00/:30、pvc-usage :05 とのずらし)
- **推定モデル**: backup 1 回 ≈ 32 MiB (repo open 時の config/snapshots/index)、
  retention (forget --prune) 1 回 ≈ 512 MiB (書き換え pack の読み直し + index 再読み)。
  桁感であり実測ではない。この桁だと土曜夜の retention 一斉稼働 (4 リポジトリ × 512 MiB)
  がアカウント合計で cap を超え得るという 08-22 事故の形状と辻褄が合う。キャリブレーション
  (B2 コンソール日次グラフとの突き合わせ = 人間専有作業) は LEDGER_RULES の数値差し替えだけで効く。
  ルールに無い CronJob は runs に混ぜず payload の `unknown_jobs` に出す (設定忘れを黙って
  0 扱いにしない)。失敗 Job の部分的消費は数えない = 過小方向の誤差 (既知の限界として YAML
  ヘッダと notes に記載)
- **指摘 3 (警報受け口) → heart の既存流路 2 本に配線**。facts.py に純関数 2 本追加:
  `budget_alert(doc)` (latest.json から warn/exceed のときだけ中身を抽出。ok/unconfigured/
  no_data/壊れは None — judge() 側と同じ「鳴らせる状態になったときだけ鳴る」) と
  `budget_alert_due(alert, prev, today)` (同じ status の同一日内の再通知を cursors の前回記録で落とす。
  status 変化 warn→exceed は同日でも鳴る)。heart.beat は (a) load_health の raw doc を捨てず受けて
  budget_alert を計算、(b) save_cursors より**前に** cursors["download_budget_alert"] を置く
  (後から置くと永続化されず次ビートが積み直す — 実装時に一度やって気づいて直した)、
  (c) briefing-queue.jsonl へ review_needed と同じ位置で append、(d) 二段目で notifier.send("incident")
  (shadow モードでは log のみ)、(e) metrics.jsonl に budget_status フィールド追加。
  選んだ経路と理由: briefing-queue.jsonl は P-0096 (朝の briefing) が回収する予定の既存溜めであり、
  incident 通知は即時性を担う。新しい通知機構は作っていない (VISION「器を太らせる前に使い切る」)

### 実測

- verify #1/#2 再実行 green。上記ユニットテスト全緑
- download_ledger.py を YAML から実抽出し偽 k8s_request で main() を回した e2e 実測:
  Complete Job のみ集計・窓外/未完了/orphan 排除・unknown_jobs 記録・id マージ (同一 id は
  今回分優先)・trim・resourceVersion 付き PUT・壊れた既存帳簿からの再出発、まで確認
- report.py の collect_download_budget() 経由の結合実測: 産出側 payload → 集約形
  (daily_bytes/by_job/window_total/monthly_estimate/budget=unconfigured) まで到達。
  生 runs (id/estimated) は latest.json 形に漏れないことを確認
- kustomize build: vaultwarden/coder/syncthing/ops-health-reporter は実測 OK。
  **immich だけサンドボックスに helm が無く未検証** (helmCharts を使うのは immich のみ、
  既存制約)。download-ledger-cronjob.yaml 単体のビルドと 4 ファイルの YAML パースは実測 OK。
  push 後の wrapper/CI で immich の kustomize build (--enable-helm) が通ることを確認すること

### 未解決の罠・開いた設計問答 (次セッション以降)

- 推定値 32 MiB / 512 MiB は**未較正**。B2 コンソールの実値でのキャリブレーションと cap 実値の
  DEFAULT_DAILY_CAP_BYTES 設定は人間専有作業 (管理コンソール)。needs-human 化するならここ。
  較正されるまで warn/exceed は理論上しか鳴らない (unconfigured で沈黙 — 正しい挙動)
- coder の LEDGER_RULES は 4 CronJob 分 (coder-restic-backup/coder-restic-retention/
  coder-workspace-home-backup/coder-workspace-home-backup-retention) を入れた。CronJob 名の
  改名があったら LEDGER_RULES と同期すること (unknown_jobs に出れば気づける)
- heart 側の警報抑制は「同一日内 & 同一 status」単位なので、warn が続く限り毎日 1 回ずつ
  briefing に積まれる (意図的)。これがうるさいようなら次のレビューで議論すること
- 環境の罠は前セッション分が有効 (mktemp 引数なし推奨、/tmp/opencode へのリダイレクト不可)。
  追加分: このサンドボックスには helm/pip/ruff が無い。ruff F821 相当は py_compile で代用した

### 発見 (スコープ外。curriculum が拾うこと)

- 新規なし。pvc_usage.py と同型の「同一埋め込みスクリプト複製 + CI 同期チェック」パターンが
  3 ファイル → 4 ファイルに増えただけ (check_pvc_usage_script_sync.py と同型のチェックを追加済み)
