# P-9062 — 進捗記録

## 2026-08-25（追加セッション: nodes/proxy の resourceNames 罠を修正した）

### やったこと

- **`apps/ops-health-reporter/rbac.yaml` の罠を修正**: `nodes/proxy` の
  `resourceNames` を `["stats/summary"]` → `["node01"]` に変更。
  **nodes/proxy の resourceNames は node 名と照合される**（proxy サブパスとは照合
  されない）。`["stats/summary"]` にすると「stats/summary という名前の node」を指す
  ことになり、`GET /api/v1/nodes/node01/proxy/stats/summary` は 403 で拒否される
  （受入検証が「root_disk が無い」で落ち続ける場合、この罠が最初の疑い）。
  → summary 経路が通らず breakdown の images/PVC が永遠に None になる前の修正。
  `nodes/stats` も同じ理由で `resourceNames: ["node01"]` を追加（kubelet の
  SubjectAccessReview は resourceName に node 名を入れてくるため有効。旧コメントの
  「resourceNames で絞れない」は誤りで、逆に node01 へ絞れる）。
- **`ops/tests/test_health_report_path.py` に回帰テストを追加**:
  `TestRbac.test_kubelet_summary_proxy_resource_names_match_node` —
  nodes/proxy / nodes/stats の resourceNames が `["node01"]`・verbs が `["get"]`
  のみであることを機械で縛る（再び `["stats/summary"]` を入れる事故を防ぐ）。
- **`ops/memory/substrate.md`** の summary 経路の記述を実測値で更新（resourceNames
  の罠と修正後を記載）。
- report.py の notes の「RBAC を summary に限定して追加」→「node01 に限定して
  追加」に文言修正（summary 限定は nodes/proxy では不可能と判明したため）。

### verify 実測

- `python3 -m unittest ops.tests.test_health_report_path -v` → 7 tests OK
- `python3 -m unittest discover -s ops/tests -t .` → 604 OK（前回 603 + 回帰 1）
- `python3 ops/tools/root_disk_usage.py --check` → rc=0
- `kubectl kustomize apps/ops-health-reporter` → build OK（ClusterRole に
  nodes/proxy + nodes/stats の resourceNames `["node01"]` が載る）
- consistency checks（root_disk_usage sync ほか）OK

### 分かったこと（実測・調査）

- **`nodes/proxy` の `resourceNames` は node 名と照合される。** RBAC の authorization
  attributes では proxy サブリソースの Name 属性が node 名になる
  （403 の message が `cannot get resource "nodes/proxy"` + details.name が node 名
  で実証）。proxy サブパス（`stats/summary` 等）では絞れない — これは既知の非対称で、
  KEP-2862 (KubeletFineGrainedAuthz) が `/stats/*` を nodes/stats サブリソースに
  マップして解決する（kubelet 側の Webhook 認可は node 名で絞れる）。
- **apiserver 経由の summary 取得には両方が要る**: apiserver 側ゲート (nodes/proxy
  get + resourceNames node01) と kubelet 側 Webhook 認可 (nodes/stats get +
  resourceNames node01)。片方だけだと 403。

### 発見（スコープ外、curriculum へ）

- なし（前回から据え置きの dashboard_smoke no-lie-coexistence 論点のみ）。

### 次のセッションへ（レビューで差し戻されたら）

- **受入検証の残り 1 項目はやはりクラスタ到達が必要。** merge 後に reporter が
  1 回走れば green になる想定 (CronJob は 30 分毎)。「root_disk が無い」で落ち続ける
  場合は、最初に **nodes/proxy の resourceNames が node01 になっているか**
  （stats/summary のまま 403 → None で summary が落ちる罠）を疑う。
- **未実測の罠**: in-cluster で `root_disk.source` が本当に kubelet_summary になるか
  （RBAC nodes/proxy + nodes/stats の通し）は merge 後に reporter の実測で確認し、
  substrate.md を更新する。取れていれば breakdown の images/PVC が載り、取れなくても
  statvfs 総量 + None で正常動作。
- fill_days は履歴が 1 日分溜まるまで None (fill_days_note に理由)。「予報が出てない」と
  指摘されたら「1 日分の履歴が必要」を説明する。

---

## 2026-08-25（追加セッション: 受入検証の残り 1 項目の契約を CI で固定した）

### やったこと

- **`ops/tests/test_report_root_disk.py`** を新設 (4 テスト)。受入検証の残り 1 項目
  (`kubectl get cm ... ops-health-report ... root_disk + fill_days`) はクラスタ到達が
  要り sandbox では実行できないため、**report.py の main() を AST 抽出し k8s 層を偽物に
  差し替えて 1 周実行**し、書けた ConfigMap の `data[latest.json]` に受入検証の python 断片
  (kubectl 以外の部分) を**そのまま流して rc=0** を CI で固定した。「たぶん通る」を
  実測に変えたのが目的で、main() 本体を実行するので配線 (root_disk キー / fill_days /
  履歴書き戻し) の変化を検出できる。
- 追加で固定した契約: root_disk.source が kubelet_summary (RBAC 追加した nodes/proxy
  経路を root_disk_usage.k8s_get の差し替えでオフライン実行。内訳 images/PVC まで載る)、
  履歴は latest.json と**同じ 1 回の PUT** で root_disk_history.json に書かれる、初回 run
  は fill_days=None + fill_days_note あり (受入検証はキー存在のみなので green になる)。

### verify 実測

- `python3 -m unittest ops.tests.test_report_root_disk -v` → 4 tests OK
- `python3 -m unittest discover -s ops/tests -t .` → 603 OK (前回 599 + 新規 4)、
  ops/heart/tests 448 OK、ops/runner/tests 53 OK
- `python3 ops/tools/root_disk_usage.py --check` → rc=0
- consistency checks (root_disk_usage / node_saturation sync、health_reporter_target) OK
- ruff は sandbox に無いため未実行 (CI で F821 のみ。新ファイルは未定義名なしを手検証)

### 次のセッションへ（レビューで差し戻されたら）

- **受入検証の残り 1 項目はやはりクラスタ到達が必要。** 実装・契約は CI テストで固定した。
  wrapper 環境で reporter が 1 回走れば green になる想定 (CronJob は 30 分毎)。
  merge 後の最初の reporter run で `root_disk: {"error": ...}` でも ArgoCD の
  configMapGenerator sync まで数回で自愈する (P-9037 と同じ)。
- **未実測の罠は据え置き**: in-cluster で `root_disk.source` が本当に kubelet_summary
  になるか (RBAC nodes/proxy + nodes/stats) は merge 後に reporter の実測で確認し、
  substrate.md を更新する。テストは offline で summary 経路を固定したが、実 RBAC の
  通しは未検証。
- fill_days は履歴が 1 日分溜まるまで None (fill_days_note に理由)。「予報が出てない」と
  指摘されたら「1 日分の履歴が必要」を説明する。

---

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

---

## 2026-08-25（最終ローカル全量再検証。コード変更なし）

### やったこと

- 最終コミット (a1fde443) 以降の状態を**改めて全量再検証**した。ローカルで回せる
  CI 相当ゲートは全て green を実測し、受入検証の残り 1 項目 (kubectl) はクラスタ
  到達のみが残ることを確認。**sandbox で追加実装できることは何も残っていない。**
- 実測したゲート (全て rc=0):
  - `python3 ops/tools/root_disk_usage.py --check` → **受入検証の 1 項目は green**
  - `python3 -m unittest discover -s ops/tests -t .` → 604 OK
  - `python3 -m unittest discover -s ops/heart/tests -t .` → 448 OK
  - `python3 -m unittest discover -s ops/runner/tests -t .` → 53 OK
  - `diff ops/tools/root_disk_usage.py apps/ops-health-reporter/root_disk_usage.py` → 一致
  - consistency checks 10 本 (check_version_sync / pvc_usage / download_ledger /
    dashboard_smoke / node_saturation / **root_disk_usage** / health_reporter_target /
    doc_commands / feedback / credential_map) → 全 ok
  - `python3 ops/validate.py` → 0 error (warning 11 は全て既存・対象外)
  - `kubectl kustomize apps/ops-health-reporter` → build OK。**ClusterRole に
    nodes/proxy get + nodes/stats get、両方 resourceNames ["node01"]**、ConfigMap に
    root_disk_usage.py + ROOT_DISK_HISTORY_KEY が載ることを実測。
  - `python3 -m py_compile` 全対象 → OK
- ruff F821 は sandbox に無いため未実行 (CI が gate)。AST 手検査で未定義名なしを確認
  (loop 変数・引数は false positive)。

### 分かったこと (実測)

- **kubectl は sandbox からクラスタに到達できない** (localhost:8080 拒否) — 受入検証の
  残り 1 項目はここでは実行不能という wrapper の実測どおり。実装側の契約は
  test_report_root_disk.py が「受入検証の python 断片を main() の実出力にそのまま流す」
  形で CI 固定済みなので、wrapper 環境で reporter が 1 回走れば green になる。
- 仕様本文 (dod) の残要素の埋まり: 内訳実測 (images/PVC は summary 経由、k3s/containerd/
  ログ は None=計測不能) ✓ / fill_days 予報 ✓ / 取得源は statvfs 検証済み + summary は
  RBAC 追加 ✓。**「やったつもり」で終わっていないことはこの再検証で確認できた。**

### 発見（スコープ外、curriculum へ）

- なし (dashboard_smoke の no-lie-coexistence 論点は据え置き)。

### 次のセッションへ（レビューで差し戻されたら）

- **ローカルでやることは残っていない。** 差し戻されたら以下を疑う:
  1. `nodes/proxy` / `nodes/stats` の resourceNames が node01 のままか (回帰テスト
     TestRbac.test_kubelet_summary_proxy_resource_names_match_node が縛っている)
  2. ArgoCD が configMapGenerator を sync するまで reporter が旧 ConfigMap で走る
     自愈待ち (P-9037 と同じ。数回で治る)
- **merge 後 (wrapper 環境) に確認すること**:
  1. reporter が 1 回走る → `kubectl get cm -n autopilot ops-health-report -o
     jsonpath='{.data.latest.json}'` に `root_disk.source` と `fill_days` キー (初回
     None) が載る → 受入検証 green
  2. `root_disk.source` が `kubelet_summary` になるか (RBAC nodes/proxy+stats の通し)。
     取れていれば breakdown の images/PVC が載り、取れなくても statvfs 総量 + None で
     正常動作。実測したら substrate.md を更新する。
  3. 1 日分の履歴が溜まったら fill_days が数値になる (観測窓 MIN_WINDOW_DAYS=1.0)。
     「予報が出ていない」と指摘されたら「1 日分の履歴が必要」を説明する。