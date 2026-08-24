# P-0361 — 写真の実データが『腐っていない』を毎週測る — immich のアセット整合性検証を常設する

## 目的

immich は「写真の実データを持つ唯一のサービス」(P-0035 の why が名指し) で、ホスト側のファイルが
黙って壊れる (bit rot・誤書き込み) と人間の唯一の原本が静かに死ぬ。P-0187 が restic 側
(バックアップの整合) を `--read-data-subset` で検証したが、ライブ側 (PVC 上の原本が immich の
記録したチェックサムと一致するか) は誰も測っていない。
バックアップが「取れる」だけでなく「原本が今も読める」ことを毎週測る常設装置を置く —
P-0005 / issue #56「試したことのないバックアップはバックアップではない」のライブ側版。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-24、`project/p-0361` の checkout で、リポジトリルートから実行)。

- [ ] `test -f apps/immich/checksum-cronjob.yaml && grep -q 'checksum-cronjob.yaml' apps/immich/kustomization.yaml`
  — 週次 checksum CronJob のマニフェストが存在し、`apps/immich/kustomization.yaml` の `resources` に
    載っていること。実測 rc=1 (`apps/immich/` に `checksum-cronjob.yaml` は存在しない。`kustomization.yaml` の
    `resources` は `ingress` / `library-pvc` / `postgres` / `postgres-external-secret` / `pvc-usage-cronjob` /
    `restic-external-secret` / `restic-backup-cronjob` / `download-ledger-cronjob` の 8 本のみ)。
- [ ] `test -f ops/tools/immich_checksum_check.py && python3 ops/tools/immich_checksum_check.py --selftest`
  — 検証ロジックが `--selftest` で同梱 fixture と自己検証できること。実測 rc=1 (`ops/tools/` に
    `immich_checksum_check.py` は存在しない。現存は `dashboard_smoke.py` / `version_watch.py` /
    `syncthing_acceptance.py` / `sops_dependency_map.py` / `check_pve_tls.sh`)。
- [ ] `test -f docs/immich-checksum.md`
  — 実機で確定した API 形・手順・根拠が `docs/` に残ること。実測 rc=1 (`docs/` に `immich-checksum.md` は
    存在しない)。
- [ ] `grep -q 'checksum' apps/ops-health-reporter/report.py`
  — `latest.json` の checksum 節への集約が実装されていること。実測 rc=1 (`report.py` に `checksum` という
    文字列は現時点で存在しない)。

**この 4 項目は DoD の下限であって、DoD そのものではない。** とくに 1 項目目は「マニフェストがあること」を
見ているだけで、API 形が正しいことは見ていない。API 形・所要時間・対象アセット数の裏付けは
`docs/immich-checksum.md` と PROGRESS.md の実測記録 (DoD 5) が担う。

## 設計方針

### 前提 (initializer が実読・実測・上流確認して分かったこと。調べ直さなくてよい)

- **現状の immich**: `apps/immich/values.yaml` の image.tag は `v3.1.0`。library は既存 PVC
  `immich-library` (`library-pvc.yaml`)。namespace `immich`。
- **上流に整合性検証機能が実在する** (v3.0.0 で導入、GitHub issue #29487 で挙動を確認):
  全アセットのチェックサム再検証をジョブで実行し、失敗を ChecksumFail 型の integrity report に記録する。
  時間制限 (timeLimit) と checkpoint (`system_metadata` の `integrity-checksum-checkpoint`) を持つ —
  大規模ライブラリ (33k assets / 750GB) では 1 回の run が 1 時間超になる実例が上流にある。
  **ただし正確な API 形 (トリガーエンドポイント・認証・状態確認・結果の読み方) は spec が明示する通り
  実機で確定する**。v3.0.1 には checkpoint 再開が 0 件処理のまま「全カバー」と嘘をつく既知バグがあり
  (issue #29487、後続 patch で修正)、実機のバージョンでどの挙動になるかは推測せず
  `docs/immich-checksum.md` に実測で書く。API キーは immich の管理 API キー (admin が発行)。
- **既存の「産出 CronJob → 専用 ConfigMap → ops-health-reporter が集約」パターン** (これを写す):
  - 産出側: `apps/immich/pvc-usage-cronjob.yaml` (専用 ConfigMap `pvc-usage-report`) /
    `apps/immich/download-ledger-cronjob.yaml` (専用 ConfigMap `download-budget`)。ServiceAccount +
    自 namespace の専用 ConfigMap 1 個のみの Role + スクリプト ConfigMap + `python:3.14-alpine`
    (stdlib のみ。immich API 呼び出しは urllib)。ConfigMap 書き込みは GET → resourceVersion 付き PUT、
    無ければ POST (`put_configmap` の流儀)。
  - 集約側: `apps/ops-health-reporter/report.py` の `collect_pvc_usage()` / `collect_download_budget()` が
    他 namespace の専用 ConfigMap を get して latest.json のキーへ畳む。RBAC
    (`apps/ops-health-reporter/rbac.yaml`) は configmaps の `resourceNames: ["pvc-usage-report",
    "download-budget", "dashboard-smoke"]` を get のみ。**checksum 用 ConfigMap はこの resourceNames に
    1 行足す** (産出側が未稼働でも error エントリに落とし、他 namespace の収集を止めない既存思想)。
    `download-budget` / `dashboard-smoke` のように「既存 writer が PUT で data 全体を置換する
    pvc-usage-report への追加キーにしない」— checksum も**専用 ConfigMap 名**にする。
- **rules.json に checksum 関連の閾値は無い** (現時点。`ops/rules.json` は veto/soak/notify/runner/chore/
  review/heartbeat/transcripts/curriculum の 7 節のみ)。DoD (3) の「不一致検出時の incident 経路を 1 か所に
  宣言」は、latest.json の checksum 節を読む読み手の不一致閾値を rules.json に足す実装になる。
- **CronJob schedule は JST で評価される** (node01 `spec.timeZone` 未指定。`ops/memory/substrate.md`)。
  週次スケジュールも JST 基準で書く。
- **`--selftest` はサンドボックス/CI でも回る** (イメージに python3 あり、`ops/memory/substrate.md`)。

### 進め方

1. **実機で API 形を確定する (DoD 1)**。管理 API キーで immich の整合性検証ジョブをトリガーし、
   対象アセット数・所要時間・結果 (ChecksumFail) の読み方を実機で確認。手順と根拠を
   `docs/immich-checksum.md` に書く。API キーの入手・保管方法 (Secret / ExternalSecret 経由か) も
   ここで決める。
2. **`ops/tools/immich_checksum_check.py` を作る (DoD 4)**。API レスポンスの解釈・不一致判定を
   純関数に分け、同梱 fixture で `--selftest` を通す。実機のレスポンス形が確定したら fixture を
   実測値で更新する。
3. **`apps/immich/checksum-cronjob.yaml` を追加する (DoD 1)**。週次 CronJob。ServiceAccount + Role
   (専用 ConfigMap 1 個のみ) + スクリプト ConfigMap + `python:3.14-alpine`。ジョブをトリガーし、
   完了を待ち、結果 (対象アセット数・不一致数・所要時間) を専用 ConfigMap
   (`immich-checksum-report` 相当) の `report.json` に書く。`kustomization.yaml` の `resources` に追加。
   所要時間は実測して schedule と `activeDeadlineSeconds` を決める (4 コアの node01 を食い過ぎない。
   `ops/rules.json` runner の容量コメント参照)。
4. **集約側 (DoD 2)**。`report.py` に `collect_checksum()` を追加して latest.json の `checksum` 節に入れ、
   `rbac.yaml` の `resourceNames` に 1 行追加する。
5. **rules.json (DoD 3)**。不一致検出時の incident 閾値を 1 か所に宣言する。
6. **実機で 1 回走らせ、対象アセット数・所要時間・結果を PROGRESS.md に残す (DoD 5)**。

### 実装上の罠 (踏むと 1 セッション無駄になる)

- 変更を伴う Job/CronJob の `.spec.template` は immutable。CronJob のマニフェストを差し替えるときは
  `argocd.argoproj.io/sync-options: Force=true,Replace=true` が要る (`ops/memory/substrate.md`。
  既存の immich CronJob には付いていないので、揃えるか否かは実装時に判断)。
- checksum 検証は重い処理 (上流実例で 33k assets が 1 回 run で 1 時間超)。**timeLimit / checkpoint 再開の
  実機挙動を必ず確認する** (v3.0.1 の既知バグ: checkpoint 再開が 0 件処理のまま完了と嘘をつく)。
  大ライブラリを 1 回の run で完走させられない場合は、docs にその事実と対処 (checkpoint 削除 +
  timeLimit 増) を実測で書く。
- 一時ファイルは `mktemp`。固定パス `/tmp/…` は前セッションの残骸を拾う (`ops/memory/substrate.md`)。
- ConfigMap は ArgoCD 管理外にする (宣言すると selfHeal が毎回書き戻し、CronJob の更新と綱引きになる。
  `dashboard-smoke` / `pvc-usage-reporter` と同じ形)。
- API 形・挙動は**実測でしか書かない**。推測を docs に書いて verify の 3 項目目を通さない (P-0035 の流儀)。
- セッション終了時 HEAD は `project/p-0361` のまま。wrapper が push する。別ブランチに移らない。

## やらないこと

- **不一致の修復・再アップロード・削除・データ復元**。検出して報告するまで (DoD (1)「不一致を報告する」)。
  修復方針 (どの原本を正とするか・restic からの復元手順) は別プロジェクト。検出した事実は
  PROGRESS の「発見」に書くだけ。
- **restic / バックアップ側の整合検証**。P-0187 の領分 (`--read-data-subset` の回転読み) は触らない。
- **storage 本体の構成変更**。このプロジェクトがもたらす差分は観測装置 (常設の整合性検証) であって、
  ストレージの再構成・マイグレーションではない。
- **immich 本体 / chart / server / machine-learning のバージョン更新**。
- **他 namespace (vaultwarden/coder/syncthing) への同型 CronJob の展開**。対象は immich のみ。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の更新**。heart の領分。