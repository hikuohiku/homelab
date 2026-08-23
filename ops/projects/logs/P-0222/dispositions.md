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
| coder | ConfigMap `coder/download-budget` | git target `data: {}` ↔ live `data.report.json` 有 (generated_at 16:25:08Z, runs 8, unknown_jobs=[download-ledger, pvc-usage-reporter]) | [ii] | application.yaml へ `ignoreDifferences` 追加: group "" / kind ConfigMap / name download-budget / namespace 各ns / jsonPointers `/data` |
| syncthing | ConfigMap `syncthing/download-budget` | 同構図 (runs 4, unknown_jobs=[download-ledger]) | [ii] | 同上 (`apps/syncthing/application.yaml`) |
| vaultwarden | ConfigMap `vaultwarden/download-budget` | 同構図 (runs 4, unknown_jobs=[download-ledger, pvc-usage-reporter]) | [ii] | 同上 (`apps/vaultwarden/application.yaml`) |

### 処置の範囲について (重要な実測知見)

当初は spec の想定どおり `.data["report.json"]` キー単位での無視を試みたが、**ArgoCD v3.2.1 のクラスタ実測で
両機構とも効かなかった**:

| 試行 | 結果 |
|------|------|
| `jqPathExpressions: ['.data["report.json"]']` | OutOfSync のまま (構文は通る、cmpErr 無し) |
| `jqPathExpressions: ['.data.report\.json']` | ComparisonError `unexpected token "\\"` で比較全体が Unknown に |
| `jsonPointers: ['/data/report.json']` | OutOfSync のまま (cmpErr 無し) |
| **`jsonPointers: ['/data']`** | **3 アプリ全部 Synced (2026-08-23T17:35Z 実測)** |

原因の説明: 正規化器は指定パスを live 側から削除するが、Git 側 target には `data: {}` (空 map) が宣言され
残り続ける。「report.json だけ消えた live」と「空 map が残る Git」の非対称自体が差分として検出される。
argocd #25157 (kiali signing_key) と同型の既知挙動で、メンテナも `/data` 全体指定を推奨している。
`/data` 全体でも対象は name+namespace+kind で download-budget 1 オブジェクトにピン留めされており、
この ConfigMap は帳簿の書き戻し先として設計上 Git が他のキーを持たないため、実害の範囲拡大は無い。
(Git 側の `data: {}` 宣言を外す選択肢は「Git 側宣言を一切変えない」という PROJECT.md 方針で排除した)

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
各 Application に ignoreDifferences が載る → 差分比較から当該 ConfigMap の data が除外され Synced 化。
CronJob・RBAC・スクリプト・Git 側宣言 (`data: {}`) は一切変えていない。

## ライブ検証の記録 (2026-08-23 17:16–17:40Z)

merge 前の実効性確認として、justfile の preview と同種の一時的な live spec 変更で検証した
(ローカルコミットは ArgoCD から見えないためブランチ向き先変更ではなく spec 直接適用):

1. root app `apps` の syncPolicy.automated を一時削除 (justfile preview と同一手順)
2. 子 3 アプリへ ignoreDifferences を kubectl patch で適用 + refresh annotation
3. **3 アプリすべて Synced を実測 (17:35:50Z)**
4. 復元: root の automated {prune:true, selfHeal:true} を再設定し、selfHeal で子の spec を
   Git (main) 状態へ戻した (preview-reset 相当)。子は merge まで OutOfSync に戻るのが正

ConfigMap の uid と data (report.json / runs) は検証前後で不変 — 帳簿データには一切触れていない。
