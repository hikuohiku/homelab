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
