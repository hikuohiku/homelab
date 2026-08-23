# P-0143 — 誰も数えたことのない隣人 — coder workspace の実消費を測り、アイドル状態で node01 からいくら奪われているかを数字で出す

## 目的

node01 は 4 vCPU / 11.7 GiB allocatable の単一ノードで、capacity こそ homelab 最大の制約。
その上で coder workspace は常時動き、大容量の動的 PVC を複数持ちながら実消費は誰にも測られていない。
各 workspace の CPU/メモリ/PVC 実使用量と idle 分を止めた場合の解放量を数字で出し、
「memory limits を付けない規則」「immich の resources 判断」など凍ったままの観測に初めて裏付けを与える。
器自身も coder 上で動くため、これは自分の足場の家賃の初精算でもある。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0143` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `bash -n ops/tools/coder_idle_audit.sh`
  — 収集スクリプトが存在し、bash として構文が通ること (DoD (4) 再実行可能の最低保証)。
  実測 rc=127 (`No such file or directory`)。
- [ ] `python3 -c "import json; d=json.load(open('ops/projects/logs/P-0143/idle-audit.json')); assert d.get('workspaces') and all(('cpu_usage' in w and 'classification' in w) for w in d['workspaces']) and 'reclaimable' in d"`
  — 実測結果が `ops/projects/logs/P-0143/idle-audit.json` に畳まれ、workspace ごとの
  `cpu_usage` と `classification`、全体の `reclaimable` を持つこと (DoD (1)(2))。
  実測 rc=1 (`FileNotFoundError: idle-audit.json`)。
- [ ] `test -f docs/coder-idle-policy.md && grep -qE 'autostop|idle' docs/coder-idle-policy.md`
  — ポリシー文書が存在し autostop/idle に触れていること (DoD (3))。
  実測 rc=1 (ファイル未存在)。

verify は DoD の下限であって DoD そのものではない。分類 (active/idle) の根拠、
解放量合計の計算過程、autostop 推奨閾値の理由づけ、器の足場の除外条件は
verify が見張らない — JSON の中身と docs/ 文章、PROGRESS.md の証跡で示すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測した。調べ直さなくてよい)

- workspace 動的 PVC の源泉は `apps/coder/templates/personal/main.tf` の
  `coder_parameter home_disk_size` (L84-87) → `storage = "${...}Gi"` (L232)。
  idle autostop 対処案はこのテンプレート側の話であり、**テンプレート改変自体は別プロジェクト**。
- `apps/coder/pvc-usage-cronjob.yaml` の pvc-usage-reporter は
  `PVC_MOUNTS=coder-postgres-data` (postgres 用) のみを計測しており、
  **workspace の動的 PVC は監視外** (health notes の記載どおり)。workspace PVC の実使用量は本件が初計測になる。
- `apps/coder/deployment.yaml` の制御プレーン Pod は requests 100m/256Mi・limits 1000m/1Gi で、
  「workspace は別 Pod として起動する」と注記がある — 測定対象は制御プレーンではなく workspace Pod。
- substrate.md: node01 は 4 vCPU / 11.7 GiB allocatable、requests 合計約 1.2 CPU / 2.6 GiB。
  「memory limits は実測の裏付けなしに付けない」(T-0055) — 本件の数字が以後の resources 判断の裏付け候補になる。
- **この initializer checkout には kubeconfig が無い**: `kubectl get pods -n coder` は
  localhost:8080 接続拒否 (実測)。収集はクラスタに到達できる環境 (runner Job 内など) で実施する前提。
  spec dod (1) が kubectl CLI read を明示指定しているので参照経路は CLI でよい (read のみ)。
- 既存シェルツールの見本: `ops/tools/check_pve_tls.sh` (ヘッダコメントに使い方と終了コード、`set -u`)。

### 作り方

1. **収集スクリプト** `ops/tools/coder_idle_audit.sh`: kubectl (read のみ) で
   coder namespace の workspace Pod 一覧・requests/limits 実値・`kubectl top` 実使用量・
   動的 PVC の要求サイズと実使用量 (exec による du 相当または metrics) を集め、
   `ops/projects/logs/P-0143/idle-audit.json` に畳む。失敗した収集は捏造せず正直に欠損として残す。
2. **分類**: 活動の有無 (agent の last_seen/exec 履歴・CPU 時系列) で各 workspace を
   active / idle に分類し、判定根拠を JSON の各要素に残す。
3. **解放量**: idle 分の CPU/メモリ/PVC GiB を `reclaimable` として合算する。
   requests ベースと top 実使用ベースの両方を出す (前者が capacity 差分、後者が実解放量)。
4. **ポリシー文書** `docs/coder-idle-policy.md`: (a) 実測表 (b) Coder の idle autostop
   テンプレートパラメータでの対処案と推奨閾値 (c) 器自身の足場となる workspace を止めない
   除外条件、を書く。

## やらないこと

- **テンプレートへの autostop 実装** (main.tf 変更)。spec 明記の別プロジェクト
- **pvc-usage-reporter / ops-health-reporter の監視範囲拡張**。workspace PVC の常設監視は別論点
- **memory limits の新規付与や既存 resources の変更**。本件は裏付けを取るだけで、
  変更判断は数字が出てからの別プロジェクト
- **workspace Pod の停止・削除など書き込み操作**。read-only 調査 (irreversible: false を維持)
- **ops/backlog.json / ops/state.json / ops/journal/ の編集**。autopilot 直接 push 領域で
  コンフリクトする (CLAUDE.md)
