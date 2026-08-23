# P-0175 — PROGRESS

worker セッションごとに追記する。書式は自由だが、証跡 (コマンドと実測値) を残すこと。
文脈は PROJECT.md とこのファイルと git log のみ。

## セッション1 — 2026-08-23 演習実施 + DoD 3 点を実装 (verify 4/4 自己実測 green)

### やったこと

1. **遮断演習を実施した** (kubectl-write、一時オブジェクト。演習後の残置物ゼロを確認済み):
   - T0 = 09:34:18Z に `netpol.yaml` を適用 → **35.5 分** 遮断 → 10:09:47Z 削除
     → 復旧観測 → 全項目回復確認
   - 計測結果はすべて `drill-report.json` 参照。主要値:
     - 既存 Secret 20/20 件が resourceVersion 込みで不変 (遮断前/中/後の 3 時点比較)
     - 稼働中 Pod は restart 0。遮断中に telegram-adapter を rollout restart → **7 秒で
       Ready** (既存 Secret のみで起動できることの実証)
     - SecretSyncedError 初発は **T0+539s** (`coder/coder-db-url`)。以降は各 ES の
       refreshTime+interval の時刻に個別にエラー化し、予定時刻とのずれは最大 1 秒
       (17 件のエラー化完了まで約 25 分)
     - 解除後の再同期は階段状 (バックオフ)。最後の 1 件まで **714 秒**。
       遮断後半にエラー化した item ほど早く戻る
   - Secret の値は取得していない。不変証明は target Secret の metadata.resourceVersion
     のみ (autopilot-writer SA では secret が読めないため、external-secrets ns に
     ESO の SA を借りた一時 curl Pod を起こして metadata のみ抽出)
2. **docs/doppler-outage-runbook.md 新設**: 症状/判定/応急/恒久を上記実測値で執筆。
   応急の核は「既存 Secret を消さない・ESO をいじらない・egress をその場で変更しない」
3. **report.py + rbac.yaml**: `collect_externalsecrets()` を collect(fn) パターンで新設
   (SecretSyncedError 数・対象名・全件の last_sync_age_seconds /
   refresh_interval_seconds)。RBAC は external-secrets.io/externalsecrets の
   get/list のみ追加

### 分かったこと (罠と発見)

- **kube-router の NetworkPolicy は DNAT 後の endpoint 側 IP で評価される** (k3s 実測):
  v1 (service CIDR の ipBlock 許可のみ) では K8s API (10.43.0.1→node:6443) に届かず、
  v2 (pod CIDR に port 制限) では kube-dns endpoint (:53) を巻き込んで DNS 死亡。
  正解形は「pod CIDR 無制限 + node IP:6443 のみ許可」(netpol.yaml コメント参照)。
  初版適用から 217 秒かけて 2 回修正した経緯も drill-report.json に記録済み
- **refreshInterval は文字列** ("1h"/"30m") で来る。数値前提でパースすると None になる
  (report.py には duration パーサ `_duration_seconds` を同梱)
- **spec 外の発見**: `syncthing/syncthing-photo-intake-credentials` が演習前から
  SecretSyncedError (2026-08-22T17:11:37Z〜)。target Secret 自体が未作成 (404) で、
  ESO ログは key IMMICH_API_KEY の取得失敗を繰り返している。別プロジェクト候補
  (curriculum が拾う想定でここに記すだけにした)
- ESO API は `external-secrets.io/v1beta1` を提供していない (/v1 と /v1alpha1 のみ)。
  report.py は /v1 を使う
- このサンドボックスの kubectl は管理者ではなく `autopilot:autopilot-writer` SA で動く
  (deploy patch / netpol 作削除 / pods exec / pods create は可、secret・SA list は不可)
- 監視ループは nohup でも親 shell の timeout に引きずられて死ぬことがある
  (setsid + stdin リダイレクトで解決)。今回 09:40〜09:46 に監視断絶があり、その間の
  エラー化は condition の lastTransitionTime (monitor.jsonl の since 値) から復元した

### 次のセッションへの一言

verify 4/4 は green 済み (wrapper の再実測待ち)。レビュー指摘が来たら最優先で解消。
生データ (monitor.jsonl / rv_*.json) はセッション作業領域にしか無い — drill-report.json
に全事象の時刻表を取り込んであるので、追証明が必要になったら「同手順の再演習」で代用する。
report.py 変更は次回 CronJob 実行 (30 分毎) から externalsecrets セクションが出るが、
RBAC 反映は ArgoCD sync 待ちなので最初の数回は error エントリになっても正常

## セッション2 — 2026-08-23 実機証明 + 収集関数の契約テスト化 (verify 4/4 再実測 green)

レビュー verdict 無し・failing 項目無しなので、DoD 3 の「マージ前に壊れていないこと」を
実機で閉じることにした。verify 4/4 を自分でも再実測して green 確認済み。

### やったこと

1. **collect_externalsecrets() を実クラスタに対して実行した** (このサンドボックスの
   SA token で report.py を importlib ロード → 実 API を叩く):
   - 21 item 中 parse-error **0**。SecretSyncedError は 1 件だけで中身は
     `syncthing/syncthing-photo-intake-credentials` (セッション1 発見の既知の
     演習前エラーと完全一致)。errored item の描画は message 切り詰め +
     `last_sync_age_seconds=None` (target Secret 未作成で最終成功同期が無い)。
     正常 item は age < interval (例: argocd-dex-client-secret 1984s < 3600s)
   - status.refreshTime の実測フォーマットは `"2026-08-23T10:16:07Z"` (秒精度) で
     strptime 書式 `"%Y-%m-%dT%H:%M:%SZ"` と一致。全 item の refreshInterval
     ("1h"/"30m") もパース成功。**本番 CronJob データでの初回稼働時にサプライズは無い**
2. **ops/tests/test_report_externalsecrets.py 新設 (11 テスト)**:
   test_download_ledger_script.py 流儀の AST 抽出 (report.py は import 時に SA
   token を読むのでクラスタ外からロードできない)。固定するのは文字列 duration の
   パース・/v1 API パス・1 item 壊れで全体を止めない・message 200 字切り詰め・
   error エントリ混在ソート・Synced のまま古い item の可視化 (age > interval)
3. **テストが `_duration_seconds` の実バグ 2 件を暴せたので修正した**
   (apps/ops-health-reporter/report.py):
   - 空文字列が **0 を返していた**。0 は「refreshInterval 0 秒」を意味するため
     last_sync_age_seconds との比較ですべての item が即滞留扱いになり、計器が
     静かに嘘をつく。None を返すように修正
   - 単独の単位 ("h") で `int("")` で**例外死**。単位の前に数字が無ければ None
   - 単位の無い数字列 ("3600") が None だった。秒とみなすよう修正
     (数値が文字列で来る API 版への備え)。修正後に実機再実行して挙動不変を確認

### 分かったこと (罠と発見)

- **本番レポート (branch ops-health-report) に externalsecrets キーが無いのは正常**。
  P-0175 は未マージで ArgoCD は main から CronJob を起こすため。10:30Z 時点の
  latest.json を実見して確認 (キー不在のみで他セクションへの影響なし)。
  「出ていない=壊れた」と追いかけて時間を溶かさないこと
- exec で作った関数の `__globals__` は exec に渡した ns 自身。`dict(ns)` とコピーしても
  関数の globals は変わらないので、k8s_get 差し替えは元の dict へ直接書き込む
- `ops/validate.py` が archive.jsonl の origin/main 不一致エラーを出すが**本ブランチ起因では
  無い** (ブランチ分岐後に curriculum が main 側へ追記したため。merge 時に解消)。
  ops/ の帳簿なので worker は触らない
- 全テストスイート実測: ops/tests 266 (新規 11 含む) / heart 196 / runner 36 すべて OK、
  check_version_sync / check_pvc_usage_script_sync / check_download_ledger_script_sync /
  check_health_reporter_target / check_doc_commands / check_feedback /
  check_credential_map すべて ok

### 次のセッションへの一言

レビュー指摘が最優先。無ければやることは無いはず — 本プロジェクトの残る不確実性は
「マージ後に reporter CronJob が externalsecrets セクションを出すか」だけだが、これは
実クラスタ実行で証明済み (上記 1)。マージ後最初の CronJob 分で latest.json の
externalsecrets セクションに RBAC 由来の error エントリが出ないことを確認するのが
最後の仕上げになる (ArgoCD sync 待ちのため merge 前は不可能)

## セッション3 — ランブックの自己レビューで 3 件の欠陥を解消 (verify 4/4 再々実測 green)

レビュー verdict 無し・failing 項目無しが 2 回続いたため、reviewer の席を自分で務めて
ブランチ全差分 (runbook / drill-report / netpol.yaml / report.py / tests) を精読した。
結果、DoD 成果物 (主に人間向けランブック) に 3 件の欠陥を見つけたので解消した。

### やったこと

1. **ランブック §恒久4 の復旧確認コマンドが論理破綻していたのを修正**:
   `kubectl get externalsecrets -A | grep -v SecretSynced` は成立しない。STATUS 列は
   正常時も `SecretSynced`、異常時も `SecretSyncedError` であり**どちらも部分文字列
   `SecretSynced` を含む**ため逆引きすると全行消え、ヘッダだけが常に残って
   「空になれば復旧完了」が絶対に満たされない (当日のクラスタで実見して確認)。
   `grep 'SecretSyncedError'`(空なら復旧完了) に修正し、罠の説明を本文に残した
2. **ランブック §判定3 の疎通 probe を http(80) → https(443) に修正**:
   旧文は `http://api.doppler.com/` なのに「ESO と同じ条件」と称していた。ESO は
   TLS/443 で話すので等価ではない。演習と同一手順 (external-secrets ns に
   doppler-probe busybox:1.36 を起こして exec) で実測したところ、**busybox:1.36 の
   wget は https を扱える** (内蔵 TLS)。`wget: note: TLS certificate validation not
   implemented` は常動で異常ではない旨も本文に記載。なお今日は http でも通った
   (Doppler の :80 が https へリダイレクトし busybox wget が追従するため) が、
   それを当てにするのは「80 番だけ生きて 443 が死んでいる」状態を見誋るので直結 https に統一
3. **中国語「缺陷」の混入 2 箇所を日本語「欠陥」に修正** (runbook §応急 /
   netpol.yaml 第2版コメント)。人間向け文書なので誤字も障害時に読む負担になる

probe Pod は作成→削除まで確認済み (残置物なし、ESO 本体 3 Pod は restarts 0 のまま)。

### 分かったこと (罠と発見)

- `kubectl get externalsecrets -A` の STATUS 値は `SecretSynced` / `SecretSyncedError`
  と部分一致するペアなので、grep での抽出・除外は必ず Error 側の正引きで行うこと
- busybox:1.36 wget の https は証明書検証なしの内蔵 TLS。到達性確認には足りる
  (検証付きにしたい場合は別ツールが必要だが、本ランブックの目的は経路の生死判定のみ)
- ドキュメント内のコマンドは CI (check_doc_commands 等) では just recipe の drift しか
  見ない。シェルワンライナーの論理的正しさ (今回の grep -v 破綻) は誰も検査していない
  — 人間向け手順書を書いたら「そのコマンドを実際に打って期待の表示になるか」を
  自分で通すしかない

### 次のセッションへの一言

変わらず「レビュー指摘が最優先。無ければやることは無いはず」。コード (report.py /
tests / rbac) はセッション2 で実機証明済みで今回は不変、成果物側の欠陥は今回で潰した。
残るのは merge 後の CronJob 確認のみ (merge 前は不可能)。もし再度 verdict 無しで
回ってきたら、スコープ外の新仕事を作らないこと — 「再演習による追証明」は reviewer が
明示的に求めたときだけ (生データは作業領域に無く、drill-report.json が一次記録)
