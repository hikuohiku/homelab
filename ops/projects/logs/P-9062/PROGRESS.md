# P-9062 — 進捗記録

## 2026-08-25（実装完了。verify 2 項目のうちローカル実行可能な方 (--check) は green）

### やったこと

- **`ops/tools/root_disk_usage.py`** (canonical) を作成。標準ライブラリのみ。
  純関数 (`sample_from_summary` / `sample_from_statvfs` / `append_sample` /
  `daily_increase_bytes` / `forecast` / `build_section` / `build_report`) と、
  ServiceAccount トークンでクラスタ到達する `measure()`、オフライン注入用 CLI
  (`--summary` / `--history`)、`--check` (ネットワーク非依存の自己検査) を持つ。
  - **総使用量**: kubelet stats/summary `node.fs` → pod 内 statvfs (`shutil.disk_usage`)
    の順に試し、後者で確実に取れる (下記の実測)。
  - **内訳**: イメージ (`node.runtime.imageFs.usedBytes`) と local-path PVC 相当
    (`node.pods[].volume[].fs.usedBytes` 合計。kubelet summary は SC を返さないため
    近似) は summary が取れたときだけ載る。k3s/containerd/ログ は非特権 pod から
    hostPath 無しでは読めないため **None (計測不能)** を正直に載せる。
  - **fill_days**: 履歴サンプル列 (ts + used_bytes) に最小二乗で日次増加量を当て、
    `free_bytes / 増加量` で残り日数を出す。観測窓が 1 日未満・増加が非正・履歴 2 点
    未満は None (予報不能) + `fill_days_note` に理由。
- **`apps/ops-health-reporter/root_disk_usage.py`**: canonical の同一内容コピー
  (configMapGenerator が /scripts に載せ、report.py から import)。drift は新設の
  **`ops/check_root_disk_usage_script_sync.py`** が CI で検出。ci.yml の consistency
  checks に追加。
- **`apps/ops-health-reporter/report.py`**: `collect_root_disk()` を追加 — latest.json の
  `root_disk` 節に内訳実測 + fill_days 予報を書く。履歴は同一 ConfigMap の別キー
  `root_disk_history.json` に保持し (PROJECT.md「履歴は root_disk の増加量計算に必要な
  最小限に閉じる」)、latest.json と**同じ 1 回の PUT** で書き戻す (別 PUT だと
  resourceVersion 競合の 409 になる)。main() の report dict・notes・kustomization.yaml
  の configMapGenerator にも追記。
- **`apps/ops-health-reporter/rbac.yaml`**: kubelet stats/summary 用に read-only RBAC を
  追加 — `nodes/proxy` get + resourceNames `["stats/summary"]` (apiserver 側ゲート)、
  `nodes/stats` get (kubelet の Webhook 認可用。kubelet は resourceName に node 名を
  入れて検査するため resourceNames で絞れない — stats サブリソース自体 read-only なので
  get のみで足りる)。
- **`ops/tests/test_root_disk_usage.py`**: 15 テスト。summary fixture のパース /
  計測不能の None / 履歴の追記と切詰め / 1 GiB/day fixture の予報 / 予報不能の理由
  note / build_report の section と履歴返却を固定。
- **`ops/memory/substrate.md`**: 「ルートディスクの計測経路 (P-9062)」節を追記。

### verify 実測

- `python3 ops/tools/root_disk_usage.py --check` → rc=0 (**受入検証の 1 項目は green**)
- `python3 -m unittest ops.tests.test_root_disk_usage -v` → 15 tests OK
- CI 相当: `python3 -m unittest discover -s ops/tests -t .` → 599 OK、ops/heart/tests
  448 OK、ops/runner/tests 53 OK、consistency checks 10 本全 ok、
  `kubectl kustomize apps/ops-health-reporter` build OK (ConfigMap に root_disk_usage.py、
  ClusterRole に nodes/proxy + nodes/stats が載ることを確認)。
- **受入検証の残り 1 項目 (`kubectl get cm ... ops-health-report ...`) はこの sandbox では
  実行不可** (kubectl がクラスタに到達できない、initializer と同じ)。実装は report.py の
  CronJob が 1 回走れば `root_disk` 節 + `fill_days` キー (初回は None だが存在) を
  ops-health-report に書く形で、クラスタ到達できる wrapper 環境で green になる想定。

### 分かったこと (実測)

- **pod 内の `df /` / `shutil.disk_usage("/")` は node01 のホストルートディスク全体を返す。**
  overlay の statfs は下層 (ホスト root ディスク) の値を透過する。この runner pod から
  overlay 251.65 GiB / used 74.07 GiB / free 167.28 GiB を実測 (node01 の 256GiB に整合)。
  → **statvfs が検証済みの総量取得源**。kubelet summary は RBAC 追加したが本セッションで
  は実測不可 (SA token 無し)。
- **非特権 pod から `/var/lib/rancher` は見えない** (`ls` で No such file)。k3s / containerd
  / ログの内訳は pod 内からは計測不能 → None を載せる設計が正しい。
- **summary 経路 (nodes/proxy + nodes/stats) は取れたら 1 回のフィクスチャ検証で良い**: 内訳
  は statvfs の総量とは独立で、summary が取れない間は images/PVC が None になるだけ
  (総量予報は止まらない)。merge 後に reporter の実測で source と breakdown を確認する。
- report.py の import (download_budget / node_saturation / root_disk_usage の 3 モジュール)
  は configMapGenerator が同じ /scripts に載せるので解決する。P-9037 と同型。

### 発見（スコープ外、curriculum へ）

- なし (dashboard_smoke の no-lie-coexistence 論点は P-9037 から据え置き)。

### 次のセッションへ（レビューで差し戻されたら）

- **受入検証の残り 1 項目はクラスタ到達が必要。** この sandbox では実行不可。wrapper 環境
  で reporter が 1 回走った後に green になる想定。もし「root_disk が無い」で落ち続けるなら
  merge 後の最初の reporter run 待ち (CronJob は 30 分毎) を確認する。
- **未実測の罠**: kubelet summary 経路 (nodes/proxy + nodes/stats の RBAC)。in-cluster で
  report の `root_disk.source` が `kubelet_summary` になるか確認し、substrate.md を更新する。
  取れていれば breakdown の images/PVC が載り、取れなくても statvfs 総量 + None で正常動作。
- **fill_days は履歴が 1 日分溜まるまで None** (fill_days_note に理由)。仕様の verify は
  キー存在のみなので初回 run で green になるが、「予報が出ていない」と指摘されたら
  「1 日分の履歴が必要」を説明する。
- merge 後、最初の reporter run で `root_disk: {"error": ...}` になっていても、ArgoCD が
  configMapGenerator を sync するまで数回で自愈する (P-9037 と同じ)。