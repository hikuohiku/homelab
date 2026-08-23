# P-0258 PROGRESS

## ログ

### セッション 1 (2026-08-23)

受入 3 項目とも failing で起動。**verify 1 (`kubectl kustomize apps | grep -q 'name:
recovery-canary'`) を green 化する DoD(1)+(3) の canary アプリ一式を実装した。**
reporter 側 (verify 2/3) は未着手。

やったこと:

1. **`apps/recovery-canary/` 新設** (6 ファイル):
   - `namespace.yaml` — 専用 Namespace `recovery-canary`。DoD(3) の「専用 namespace ラベル」
     として `app.kubernetes.io/name: recovery-canary` + `recovery-canary: isolated` を付与
     (selector には使わない一覧用マーカー)。本体アプリのリソースは一切無い
   - `deployment.yaml` — canary 本体。pause コンテナ 1 replica
     (`registry.k8s.io/pause:3.10`)。非 root (65534)、SA token 不マウント、
     resources 無し (T-0055: pause の実消費は未実測につき limits/requests とも付けない)
   - `rbac.yaml` — SA `recovery-canary-probe` + Role/RoleBinding。権限は
     deployments get/delete (resourceNames `["recovery-canary"]`) +
     configmaps create / get,update (resourceNames `["recovery-probe"]`) のみ。
     pods 権限は不要 (Ready 判定は Deployment の status.readyReplicas 観測で行う)
   - `cronjob.yaml` — runner ConfigMap (埋め込みスクリプト) + CronJob。
     schedule `"43 3 * * *"` (JST 評価。既存 collector :00/:05/:25/:30/:37/09:40 と
     非重複)。backoffLimit 0、activeDeadlineSeconds 1500。イメージは
     version-watcher 同様 `python:3.14-alpine` (標準ライブラリのみ方針)
   - `kustomization.yaml` / `application.yaml` — version-watcher と同型
     (automated prune+selfHeal、CreateNamespace=true)
2. **runner スクリプト** (cronjob.yaml 内 `recovery_probe_runner.py`) の計測手順:
   - DELETE → 応答に旧オブジェクトが載ればその `metadata.uid` を採る
   - **wait_gone**: 404 になるまで poll (10 秒刻み)。ただし再作成が最初の poll よりも
     速い環境 (webhook 等) では新オブジェクトが既に居るため、**uid 変化をもって旧来の
     消滅とみなす**。これが無いと即時修復環境で「消失確認できない」誤失敗になる
     (モックシミュレーションで実証 → 実装済み)
   - **wait_ready**: 再作成された Deployment の `status.readyReplicas == spec.replicas`
     を待つ。タイムアウト時に一度でも再作成を観測したかで phase を分岐
     (`wait-recreate` = reconciliation すら届いていない / `wait-ready` = 作られたが
     Ready にならず)
   - 一括上限 HEAL_TIMEOUT_S=1200 秒。超えたら ok=false の記録を書いて exit 1
     (Job 失敗は pod_issues 収集と appTree health の既存経路にも乗る)
   - 計測値は専用 ConfigMap `recovery-probe` の report.json キーへ
     (GET→resourceVersion 付き PUT / 無ければ POST。dashboard-smoke ランナーと同じ流儀)。
     ConfigMap 自体は manifest に事前作成しない (tracking label 無しで create するので
     ArgoCD 管理外になり、prune/selfHeal と競合しない)
3. root `apps/kustomization.yaml` へ登録 + CLAUDE.md の Apps 列挙と apps/README.md
   (期待される出力・ディレクトリ構造) に recovery-canary を追記
   (CI の check_app_list_sync.py が両 doc への言及を要求する)

### report.json の契約 (次セッションの verify 2 はこの形を固定すること)

```json
{
  "schema": 1,
  "tool": "recovery_canary",
  "project": "P-0258",
  "generated_at": "2026-08-25T18:43:12Z",
  "ok": true,
  "deleted_at": "2026-08-25T18:40:05Z",
  "namespace": "recovery-canary",
  "deployment": "recovery-canary",
  "last_recovery_seconds": 187,
  "ready_at": "2026-08-25T18:43:12Z"
}
```

失敗レコードは `ok: false` で **last_recovery_seconds を持たず**、代わりに
`phase` (delete / wait-deletion / wait-recreate / wait-ready) と `error` (人間向け文面)
を持つ。秒数の捏造をしないため。集約側は dashboard_smoke 流儀で
status ok / fail / stale / no_data へ判定し、ok レコードだけが
`last_recovery_seconds` int を latest.json へ載せる形が自然 (verify 3 の
assert 条件と整合)。stale 閾値は日次 CronJob なので DASHBOARD_SMOKE_STALE_AFTER_S
(26h) と同じ考え方で。

## 分かったこと / 実測

- **verify 1 は自分の環境で green を実測済み**: `kubectl kustomize apps | grep -q
  'name: recovery-canary'` rc=0 (kubectl v1.35.0 / Kustomize v5.7.1)。
  render 結果は 7 docs (Namespace, SA, Role, RoleBinding, ConfigMap, Deployment,
  CronJob) で全て yaml.safe_load 通過
- 埋め込みスクリプトは py_compile 通過 (254 行)。純関数 (iso /
  deployment_ready / build_report) を AST 抽出して unittest 相当の検査を実施:
  - 成功レコード last_recovery_seconds は int、deleted_at/generated_at は
    %Y-%m-%dT%H:%M:%SZ
  - deployment_ready は replicas 不明・bool replicas・readyReplicas 0 を倒す
- **main() の制御フローをモック k8s API で 7 シナリオ実測** (happy path /
  即時再作成 race / 再作成されない / Ready にならない / 旧 uid 残置 /
  DELETE=404 からの継続 / DELETE 403)。全部意図どおりの phase・exit code
- **worker 環境の /tmp/opencode は root 所有で書けない** (P-0193 の発見を再実測)。
  mktemp を使えば問題無し
- **check_app_list_sync.py は main で既に 6 件の drift エラーを出す** (version-watcher /
  nats / autopilot-core が CLAUDE.md と apps/README.md に無い。origin/main checkout で
  再現確認済み = 既存問題であり本プロジェクトの変更では無い)。recovery-canary 分は
  両 doc に追記済みなので新規 drift はゼロ。CI の required check になっていないのか
  main は通っている → 発見節へ記載済み

## 発見 (仕様外。curriculum が拾うこと)

- `ops/check_app_list_sync.py` が origin/main で 6 件の drift を出す (上記)。
  チェック自体は T-0156 で入ったはずだが required check で無いのか main で沈黙している
- ArgoCD の reconciliation 間隔は values.yaml で未設定 (= 上流既定 3 分)。
  夜間の計測値は最大 ~3 分 + Pod 起動数十秒になると想定される。初回実測で確認すること
- canary Application は毎晩 03:43〜03:47 JST 前後に OutOfSync/Progressing になる
  (Deployment 消滅〜selfHeal 再作成の窓)。CHARTER §2 の「applications 全て
  Synced/Healthy」チェックはこの窓に当たると誤報しうる → **reporter の notes 文言
  (verify 2 の 3 点セットの 1 つ) に必ず「夜間の一時的 Degraded/Progressing は仕様」
  と書くこと** (PROJECT.md の前提にもある)

## verify 自己実測

| # | コマンド | 結果 |
|---|---------|------|
| 1 | `kubectl kustomize apps \| grep -q 'name: recovery-canary'` | **green (rc=0)** |
| 2 | `python3 -m unittest ops.tests.test_recovery_probe_parse -v` | red (未着手。次セッション) |
| 3 | `git show origin/ops-health-report:ops/health/latest.json ...` | red (merge + 初回夜間 run + reporter run 待ち) |

追加実施: ops/tests 全体 440 本 green (既存への影響無し) / render 7 docs の YAML parse /
埋め込みスクリプト py_compile / 純関数・制御フローのモック検査 (上記)

## 次のセッションへの一言

**verify 2 をやる**: (a) `apps/ops-health-reporter/rbac.yaml` の configmaps get
resourceNames に `recovery-probe` を追加、(b) report.py に
`collect_recovery_probe()` (+ 純関数は単一ファイルモジュール `recovery_probe.py`
に分離 — download_budget.py 流儀。report.py は import 時 SA token を読むため
cluster 外から直接 import できません), (c) notes 文言 (夜間 Degraded 誤報防止の注記含む。
「発見」節参照)、(d) `ops/tests/test_recovery_probe_parse.py` 新設 — 契約は上の
report.json の契約節どおり (ok=true のとき last_recovery_seconds が int / 壊れた記録は
no_data / 古い記録は stale / phase 付き fail レコードの扱い)。テストの型は
test_download_budget.py (importlib 直ロード) か test_report_dashboard_smoke.py
(AST 抽出 + k8s_get 差し替え) を参照。verify 3 は merge 後の初回夜間 run (03:43 JST)
+ reporter run (:00/:30) で初めて green になるので、実装側の完成宣言は verify 2 まで。
初回実行で落ちたら pod ログと recovery-probe ConfigMap の error/phase で切り分けること
(RBAC 漏れなら phase=delete の HTTP 403 に出る)

### セッション 2 (2026-08-23)

**verify 2 を green 化した** (reporter 側 3 点セット + テスト新設)。verify 1 は
再実測で継続 green、既存テストへの影響ゼロ。

やったこと:

1. **`apps/ops-health-reporter/recovery_probe.py` 新設** — 計測記録のパース/要約の
   純関数モジュール (download_budget.py 流儀。import 副作用なし、cluster 外から
   importlib 直ロード可):
   - `build_summary(payload, now)` → status ok / fail / stale へ判定
     (no_data は report.py 側 collect が例外を拾って付ける)
   - `parse_utc` (産出側 iso() 書式のみ厳格受付) / `coerce_seconds`
     (0 以上の int のみ。bool は int 派生なので明示的に弾く)
   - 定数: `RECOVERY_PROBE_NAMESPACE = "recovery-canary"` /
     `STALE_AFTER_S = 26 * 3600` (dashboard-smoke と同じ考え方) / `ERROR_LIMIT = 200`
2. **report.py**: `import recovery_probe` 追加 +
   `collect_recovery_probe()` (`/api/v1/namespaces/recovery-canary/configmaps/recovery-probe`
   を読み build_summary へ。collect_dashboard_smoke と同型) +
   main() report dict へ `"recovery_probe": collect(collect_recovery_probe)` +
   notes 文言 (recovery_probe キーの説明 + **「毎晩 03:43〜03:47 JST の
   OutOfSync/Progressing/Degraded は仕様であり誤報しないこと」の注記を含む**。
   発見節の懸念はここで解消済み)
3. **rbac.yaml**: configmaps get の resourceNames に `recovery-probe` 追加 (render 実測:
   `['pvc-usage-report', 'download-budget', 'dashboard-smoke', 'recovery-probe']`)
4. **kustomization.yaml**: configMapGenerator files に recovery_probe.py 追加
   (render 実測: script ConfigMap の keys = download_budget.py / recovery_probe.py / report.py)
5. **`ops/tests/test_recovery_probe_parse.py` 新設** — 27 テスト。
   純関数は importlib 直ロード (test_download_budget 流儀)、
   report.py の collect は AST 抽出 + k8s_get 差し替え
   (test_report_dashboard_smoke 流儀)。両方の契約を固定

このセッションで固定した集約側の契約細部 (前セッションの契約節を具体化):

- **秒数を載せるのは status=ok のときだけ**。fail には産出側が誤って
  last_recovery_seconds を持たせても絶対に載せない (捏造ガード。テスト済み)。
  stale も載せない — 昨晩の数字を今日の状態のように見せないため
- ok レコードでも seconds が bool / 非整数 / 負値 / 欠損なら ValueError → no_data
  (「帳簿の壊れ」と「装置の故障」の区別を保つ。_dashboard_smoke_summary と同じ思想)
- 鮮度最優先: age > STALE_AFTER_S でのみ stale (ちょうど境界は鳴らさず)。
  stale beats fail (古い失敗記録より沈黙を先に報せる)
- fail は phase (文字列のみ通す。空/非文字列はキー省略+reason が unknown) と
  error (200 字切り詰め) を載せる。RBAC 漏れの切り分け (phase=delete + HTTP 403 文面)
  が latest.json まで届く
- 要約には tool/project/schema/namespace/deployment 等の定数フィールドを載せ直さない
  (history jsonl の 1 行膨張止め)

## 分かったこと / 実測 (セッション 2)

- **test_report_dashboard_smoke.py の collect 系 stale テストは潜在フラワ**:
  fixture の generated_at を NOW 定数 (2026-08-23 03:00 UTC) 基準で作るのに、
  collect_dashboard_smoke 内部では実壁時計の datetime.now() で鮮度判定している。
  偶一致 (実行時刻が NOW 近傍) で通っているだけで、暦が進むか朝早く走ると落ちる。
  **自分のテストでは collect 経路のクロックを凍結して回避した**: AST 抽出の名前空間の
  `"datetime"` を SimpleNamespace shim に差し替える (freeze_clock 参照)。
  この発見自体は仕様外なので curriculum 拾い候補
- shim 差し替えの罠: `datetime.datetime.now(datetime.timezone.utc)` の引数式
  `datetime.timezone.utc` はグローバル名 `datetime` (shim の外側) から解決されるため、
  外側にも `.timezone` を生やさないと AttributeError になる (実測 2 回躓いた)
- **ローカル環境に `kustomize` バイナリが無い**ので `ops/check_manifest_deletions.py`
  は worker 環境で検証不可 (FileNotFoundError。main checkout でも同一エラー =
  既存の環境制限)。`kubectl kustomize` なら同等 render の検証ができる
- CI 相当を全て実測: ops/tests 467 本 (440+27) / ops/heart/tests / ops/runner/tests
  全部 OK。check_version_sync / check_health_reporter_target / check_doc_commands も
  通過。check_app_list_sync の drift は前セッション実測の 6 件から増減なし

## verify 自己実測 (セッション 2)

| # | コマンド | 結果 |
|---|---------|------|
| 1 | `kubectl kustomize apps \| grep -q 'name: recovery-canary'` | **green (rc=0)** 再実測 |
| 2 | `python3 -m unittest ops.tests.test_recovery_probe_parse -v` | **green (27 tests OK)** |
| 3 | `git show origin/ops-health-report:ops/health/latest.json ...` | red (merge 後の初回夜間 run 待ち。実装側ではこれ以上動かせない) |

追加実施: py_compile (report.py / recovery_probe.py) / root render 17 docs YAML parse /
reporter 単体 render で RBAC・script keys 実物確認 (上記 3./4.) / 全テストスイート green

## 次のセッションへの一言

**実装は完了。残る red は verify 3 のみ**で、merge 後に ArgoCD が apps を sync し
(1) 初回夜間 run (03:43 JST, CronJob recovery-canary-probe) が recovery-probe
ConfigMap を書き (2) 次の reporter run (:00/:30 JST) がそれを読んで
latest.json の recovery_probe.last_recovery_seconds が int になるのを待つだけ。
人手での確認手順: `kubectl -n recovery-canary get cronjob,pods` → Job ログ →
`kubectl -n recovery-canary get cm recovery-probe -o jsonpath='{.data.report\.json}'`。
初回 run で ok=false になったら ConfigMap の phase/error で切り分け
(phase=delete + 403 なら reporter RBAC ではなく canary 自身の Role 問題。
wait-recreate なら ArgoCD が recovery-canary Application を sync できていない疑い —
Application の syncStatus を先に見ること)。stale 判定の実効確認は「26h 以上
run が無い」状態を作らないとできないので不要。テストを足すときは
collect 経路のクロック凍結 (freeze_clock) を踏襲すること —
test_report_dashboard_smoke.py 型の壁時計依存テストは潜在フラワ (上記実測参照)。
