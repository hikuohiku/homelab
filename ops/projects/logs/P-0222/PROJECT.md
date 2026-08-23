# P-0222 — 「ずれているのが普通」の座を奪還する — coder・syncthing・vaultwarden の OutOfSync を今日ゼロにし、immich は P-0092 に譲って二度と手を出さない

## 目的

最新 health 実測で coder / syncthing / vaultwarden が OutOfSync。何週間も「Synced じゃないのが当たり前」になり、
P-0153 / P-0189 が指摘して終わった状態が続いている (両案とも不採択で、修繕には誰も手を付けていない)。Git → ArgoCD という
唯一のデプロイ経路において「本番は Git と一致している」は不変条件のはずで、常時ドリフトはそれが静かに破られている証拠。
今度は報告ではなく**修繕そのもの**が成果。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**
(2026-08-23 17:05Z 頃、`project/p-0222` の checkout でリポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `git fetch origin ops-health-report -q && git show origin/ops-health-report:ops/health/latest.json | python3 -c "import json,sys; d=json.load(sys.stdin); bad={a['name'] for a in d['applications'] if a.get('sync')!='Synced'}; sys.exit(0 if not (bad & {'coder','syncthing','vaultwarden'}) else 1)"`
  — 当該 3 アプリの sync が health レポート上すべて Synced になったことの機械確認。
  実測 rc=1 (bad = coder, immich, syncthing, vaultwarden。immich はスコープ外だが判定式に含まれるのは 3 アプリのみなので影響しない)。
- [ ] `test -s ops/projects/logs/P-0222/dispositions.md && python3 -c "import sys; t=open('ops/projects/logs/P-0222/dispositions.md').read(); apps=['coder','syncthing','vaultwarden']; kinds=['[i]','[ii]','[iii]']; sys.exit(0 if all(a in t for a in apps) and any(k in t for k in kinds) else 1)"`
  — 処置表 (アプリ×原因型×処置×根拠diffへの参照) が存在し、3 アプリと原因型の分類語を含むこと。
  実測 rc=1 (ファイル未存在)。

**verify は DoD の下限であって DoD そのものではない。** DoD 本体は「diff の実内容取得 → 3 型分類 → 分類ごとの処置 →
データを持つオブジェクトの保護 → 処置表」という手順の遂行であり、最後の Synced 化はその結果として届く。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測。調べ直さなくてよい)

- **ドリフトは各アプリ 1 オブジェクトだけ**: `kubectl get applications -n argocd coder syncthing vaultwarden -o json` の
  `status.resources` 実測で、OutOfSync なのはいずれも `ConfigMap <ns>/download-budget` 1 点のみ (health は全員 Healthy)
- **原因の構図**: `download-budget` は Git 上 `data: {}` で宣言され (apps/{coder,syncthing,vaultwarden}/download-ledger-cronjob.yaml)、
  実行時に download-ledger CronJob (P-0128, 2026-08-23 merge) が `report.json` を書き戻す産出先である。
  live には `report.json` があり Git は空 — 恒久的な見かけ上のドリフトになる
- **selfHeal でも消えない実測**: 3 アプリとも `automated.selfHeal: true` なのに lastOp Succeeded (2026-08-23T16:58-16:59Z) の後も
  `report.json` (generated_at 16:25Z) が生存。apply の last-applied-configuration に `report.json` は一度も載っていないため
  クライアントサイド apply はこれを削除しない (この機構の説明は推測。「sync が成功し続けるのに差分が消えない」こと自体は実測)。
  つまり原因型は **[ii] (生成フィールドの毎回差分) に近いが、最終分類は worker が diff 実内容を取って判断する (DoD(1))**
- **この ConfigMap はデータそのもの**: `report.json` は B2 download cap の帳簿で、「ConfigMap 側が唯一の長期記憶」
  (Job 履歴は successfulJobsHistoryLimit: 3 で消える)。KEEP_DAYS=14 の runs を失うと帳簿が巻き戻る — **DoD(3) が最重要適用対象**
- リポジトリに `ignoreDifferences` の既存使用例は無い (全 apps/ 実測)。初導入なら最小最小範囲に絞る

### 作り方

1. 各アプリの ArgoCD diff の実内容を取得し (argocd CLI 無し環境。Application status / live manifest との突合で代替)、
   原因を [i]/[ii]/[iii] に分類して根拠 diff と共に台帳へ
2. 処置は application.yaml への `ignoreDifferences` 追加を基本形とする (対象は当該 ConfigMap の `.data.report.json`
   程度まで絞る)。Git 側宣言・CronJob・RBAC・帳簿データは一切変えない — 変更は 3 ファイル × 数行のはず
3. merge → ArgoCD 同期後、次回の health レポート (毎時 :00/:30 産出) で 3 アプリが Synced になったことを verify(1) で確認。
   跨ぎ時間があるので証跡は PROGRESS.md へ
4. dispositions.md に処置表を残す (DoD(4))

## やらないこと

- **immich への一切の接触** — 同じ `download-budget` ドリフトが immich にもあるが、稼働中の P-0092 (immich postgres 更新)
  の作業域なので触らない。PR 本文にも明記する (DoD)
- **帳簿データの削除・ConfigMap の作り直し・report.json スキーマの変更** — 「直した」がデータ消失を意味しない (DoD(3))。
  download-ledger スクリプト本体も触らない (4 アプリ分が同一であることを CI が機械検査しており、触れると同期作業が広がる)
- **health レポーターや heart への監視追加** (P-0189 案にあった「N 日継続で briefing に乗る」等) — 別論点 (1 PR 1 論点)
- **argocd-cm 等のクラスタ全体設定の変更、他アプリへの横展開** — 対象は 3 アプリの application.yaml に留める
