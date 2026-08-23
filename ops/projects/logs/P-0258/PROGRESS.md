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
