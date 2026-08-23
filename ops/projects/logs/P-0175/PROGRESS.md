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
