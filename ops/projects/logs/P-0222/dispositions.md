# P-0222 処置表 — coder / syncthing / vaultwarden の OutOfSync 修繕

実測日時: 2026-08-23T17:13Z (cluster 実物、`project/p-0222` checkout から kubectl CLI で取得)。
immich は本表の対象外 (P-0092 の作業域、一切接触していない)。

## 原因型の定義 (spec より)

| 型 | 意味 | 本件への当てはまり |
|----|------|------------------|
| [i] | Git 未反映の手動変更 | 該当せず。人手による変更は無く、report.json は CronJob の自動書き戻し |
| [ii] | テンプレートの非決定性 (生成フィールドの毎回差分) | **該当**。生成元は helm ではなく download-ledger CronJob (毎時 :25) だが、「実行時に産出されるフィールドが Git と永遠に一致しない」構図は同型 |
| [iii] | Git 側 manifest の誤り | 該当せず。`data: {}` は意図的 (update 専権 RBAC のための事前作成。download-ledger-cronjob.yaml 冒頭コメント「中身は download-ledger だけが書く」)。Git に report.json を取り込むと次の時刻でまたずれ、しかも古くなるので誤り |

## 処置表

| アプリ | ドリフト対象 | 根拠 diff (2026-08-23T17:13Z 実測) | 原因型 | 処置 |
|--------|-------------|-----------------------------------|--------|------|
| coder | ConfigMap `coder/download-budget` | git target `data: {}` ↔ live `data.report.json` 有 (generated_at 16:25:08Z, runs 8, unknown_jobs=[download-ledger, pvc-usage-reporter]) | [ii] | application.yaml へ `ignoreDifferences` 追加: group "" / kind ConfigMap / name download-budget / jqPathExpressions `.data["report.json"]` |
| syncthing | ConfigMap `syncthing/download-budget` | 同構図 (runs 4, unknown_jobs=[download-ledger]) | [ii] | 同上 (`apps/syncthing/application.yaml`) |
| vaultwarden | ConfigMap `vaultwarden/download-budget` | 同構図 (runs 4, unknown_jobs=[download-ledger, pvc-usage-reporter]) | [ii] | 同上 (`apps/vaultwarden/application.yaml`) |

### 根拠 diff の取得方法

argocd CLI 無し環境のため、Application status と live manifest の突合で代替:

```
kubectl get applications -n argocd coder syncthing vaultwarden -o json   # → status.resources で OutOfSync は各アプリ download-budget のみ
kubectl get configmap download-budget -n <ns> -o json                    # → live data keys = ["report.json"]
apps/<app>/download-ledger-cronjob.yaml                                  # → git target data = {}
```

3 アプリとも差分は `report.json` キー 1 点のみで、他フィールド (metadata 等) の差分は無い。

## データ保護 (DoD(3))

- report.json は B2 download cap の帳簿であり、ConfigMap 側が唯一の長期記憶
  (Job 履歴は successfulJobsHistoryLimit: 3 で消える)
- 本プロジェクトで live オブジェクトへの削除・作り直し・data 書き換えは**一切実施していない**。
  ignoreDifferences は ArgoCD の差分比較からの除外のみで、クラスタ内の実データには触れない
- 検証 (preview) 前後で ConfigMap uid 不変を確認した (PROGRESS.md 参照)

## 恒久解消の経路

merge 後: root app `apps` (path apps/, selfHeal) が本ブランチの application.yaml を適用 →
各 Application に ignoreDifferences が載る → 差分比較から `.data["report.json"]` が除外され Synced 化。
CronJob・RBAC・スクリプト・Git 側宣言 (`data: {}`) は一切変えていない。
