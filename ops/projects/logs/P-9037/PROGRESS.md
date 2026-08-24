# P-9037 — 進捗記録

## 2026-08-24（レビュー指摘の解消: 終端 pod の requests 水増しを直す）

### やったこと

- **`sum_cpu_requests()` に終端 pod 除外を実装** (レビュー指摘の解消)。
  status.phase が `Succeeded`/`Failed` の pod はスケジューラが容量に数えないため
  集計から除外する (クライアント側フィルタ。`TERMINAL_PHASES` 定数を新設)。
  k3s は terminated-pod-gc まで終端 pod を残し続けるため、数えると水増しになる。
  - `ops/tools/node_saturation.py` (canonical) と
    `apps/ops-health-reporter/node_saturation.py` (クラスタ内コピー) を**同一 PR で
    両方修正**し、byte 一致を維持 (sync check OK)。
  - report.py は `node_saturation.sum_cpu_requests(pods)` を呼ぶだけなので変更不要 —
    collect_node_saturation の実測値も自動的に終端 pod 除外後の値になる。
- **`ops/tests/test_node_saturation.py` に終端 pod fixture を追加**:
  - レビュー時の実測値 (Running 42 / Succeeded 92 / Failed 25) を踏襲した
    fixture — Running のみ 3924m/4000m に対し終端 pod 込みだと 43594m (ratio 10.90)。
    「3924 が返り 43594 にならない」ことを固定。
  - phase を持たない pod (手作り fixture) は従来どおり数える、の互換テストも追加。
  - `test_review_time_cluster_state_fires_warn` — 終端除外後の現状態
    (3924m/4000m = 98%) で正しく warn が鳴ることを judge レベルで固定
    (計器の役割どおり、レビュー文言で確認済み)。
- `_selfcheck()` (`--check`) にも同じ終端 pod fixture を追加 (selfcheck と
  単体テストが同じロジックを二重に固定する)。

### verify 実測

- `python3 ops/tools/node_saturation.py --check` → rc=0
- `python3 -m unittest ops.tests.test_node_saturation` → **21 tests OK** (18 → 21)
- CI 相当: `python3 -m unittest discover -s ops/tests -t .` → 562 OK、
  `ops/heart/tests` → 448 OK、consistency checks (`check_node_saturation_script_sync`
  含む 5 本) 全 ok。

### 分かったこと (実測)

- 既存の `sum_cpu_requests` fixture は status.phase を持たないため、phase 無しを
  「数える」にしないと既存テスト (selfcheck の 1750 等) が壊れる。除外条件は
  「phase が Succeeded/Failed のときだけ」に限定した。実 API 応答は必ず phase を
  持つため、実測値に影響しない。

### 発見（スコープ外、curriculum へ）

- (前セッションの dashboard_smoke no-lie-coexistence 論点は据え置き)

### 次のセッションへ（レビューで差し戻されたら）

- verify は green (2 項目)。wrapper が PR を出し、レビューと CI が判断する。
- 前セッションの「次のセッションへ」は据え置き:
  - dashboard の変更 (kubernetes.ts) は反映に 2-stage を要する (build → digest pin
    follow-up PR)。「動いていない」と指摘されたらこの運用を説明する。
  - **未実測の罠**: kubelet summary proxy 経路。in-cluster で
    `node_saturation.py --node node01` を動かせる環境ができたら load_source を確認し
    substrate.md を更新する。
  - merge 後の最初の reporter run で node_saturation キーが error になっていても
    ArgoCD が configMapGenerator を sync するまで数回で自愈する。

---

## 2026-08-24（実装完了・verify 2 項目 green）

### やったこと

- **`ops/tools/node_saturation.py`** (canonical) を作成。標準ライブラリのみ。
  純関数 (`parse_cpu_millicores` / `sum_cpu_requests` / `allocatable_cpu_millicores` /
  `vcpus` / `read_loadavg` / `load_from_summary` / `judge` / `exit_code`) と、
  ServiceAccount トークンでクラスタ到達する `run()`、オフライン判定用 CLI オプション
  (`--requests-m` 等)、`--check` (ネットワーク非依存の自己検査) を持つ。
  load は kubelet stats/summary → `/proc/loadavg` の順に試し、実効的には /proc に倒れる
  (summary に host load が無い — P-9029 の審査指摘どおり)。
  閾値は rules.json の逆算を根拠に P-9029 の dod 踏襲: allocatable の 90% 超 または
  load > vCPU 数 → status=warn / exit 1。
- **`apps/ops-health-reporter/node_saturation.py`**: canonical の同一内容コピー
  (configMapGenerator が /scripts に載せ、report.py から `import node_saturation`)。
  drift は新設の **`ops/check_node_saturation_script_sync.py`** (dashboard_smoke と同じ
  canonical/copy 検知パターン) が CI で検出。ci.yml の consistency checks に追加。
- **`apps/ops-health-reporter/report.py`**: `collect_node_saturation()` を追加 —
  全 namespace pod の requests 合計と node01 の allocatable、/proc/loadavg を実測し
  `judge()` の結果を latest.json の `node_saturation` キーに書く (既存の
  collect_nodes / collect_node_metrics と並ぶ collect 関数)。main() の report dict と
  notes にも追記。kustomization.yaml の configMapGenerator に node_saturation.py を追加。
- **heart 配線 (P-0128 budget と同じ 2 段階)**: `ops/heart/facts.py` に
  `node_saturation_alert(doc)` — latest.json の status=warn だけを抽出し、reasons を
  人間向け文面 (実測値入り) に展開。`ops/heart/heart.py` で budget/smoke と同じ
  cursors 抑制 (`node_saturation_alert` cursor キー) + briefing-queue.jsonl + incident
  通知、metrics.jsonl に `node_saturation_status`。
- **dashboard 配線**: `apps/ops-dashboard/rbac.yaml` に configmaps get
  (`ops-health-report` のみ) を追加。`kubernetes.ts` に純関数 `saturationWarning()` /
  `parseReportDoc()` を追加し、`getKubeSnapshot()` が latest.json の node_saturation
  warn を KubeSnapshot.warning (→ snapshot.ts:76 の warnings 配列 → page.tsx:404 の
  global-warning) に載せる。dashboard のコアテスト (core.test.ts) に 08-24 fixture の
  文面テストを追加。
- **`ops/memory/substrate.md`** (dod (4)): 「CPU 飽和前兆の load 取得源」節を追記。

### verify 実測

- `python3 ops/tools/node_saturation.py --check` → rc=0
- `python3 -m unittest ops.tests.test_node_saturation` → 18 tests OK

CI 相当も local で確認: ops/heart/tests 448 / ops/runner/tests 53 / ops/tests 559 全 OK、
dashboard `npm run test` 22 OK + `npm run lint` (tsc --noEmit) OK、consistency checks 全
ok、`kubectl kustomize` で ops-health-reporter / ops-dashboard 両方 build OK。

### 分かったこと (実測)

- **pod 内から `/proc/loadavg` が読め、node01 の host load 全体を返す**。loadavg は PID
  namespace で仮想化されない。この sandbox (runner pod) から 1 分平均と 4 vCPU を実測
  (substrate.md に verified_at 付きで記録)。
- **kubelet stats/summary には host load が無い** (P-9029 の審査指摘どおり)。取得源は
  /proc に倒すのが正。summary proxy 経路 (nodes/proxy RBAC) は**未実測** — reporter には
  nodes/proxy を付けなかった (summary に load が無いので不要。最小限の配線)。
- report.py の import が成り立つよう configMapGenerator の変更だけで /scripts に
  node_saturation.py が載る (report.py と同じ 2 ファイル構成の download_budget.py と同型)。

### 発見（スコープ外、curriculum へ）

- **dashboard_smoke (P-0193) の no-lie-coexistence 検査が、node_saturation warn 表示
  (global-warning) + 正常 heart chip の共存を矛盾として鳴らす可能性**。既存の
  state.warning / kube.warning とも同じ相互作用だが、node_saturation が鳴るのは node01 が
  飽和したときだけなので許容した。文面は「正常チップと異常表示が共存: global-warning:
  CPU 飽和前兆 …」で実際に起きていることを言い当てるためミスリーディングではない。
  ただし毎回必ず併発するので、もし dashboard_smoke 側で警告種別を区別したいなら
  curriculum の論点。

### 次のセッションへ（レビューで差し戻されたら）

- verify は green。wrapper が PR を出し、レビューと CI が判断する。
- **dashboard の変更 (kubernetes.ts) は反映に 2-stage を要する**: build-dashboard-image.yml
  が main push で build → digest pin の follow-up PR が必要。レビューで「動いていない」と
  指摘されたらこの運用を説明する (deployment.yaml の pin 更新は P-9037 の外)。
- **未実測の罠**: kubelet summary proxy 経路。in-cluster で `node_saturation.py --node node01`
  を動かせる環境ができたら load_source を確認し、substrate.md を更新する。
- report.py の node_saturation キーは、ArgoCD が configMapGenerator の変更を sync するまで
  (既存 ConfigMap に node_saturation.py が無いと import エラーになる) — merge 後の最初の
  reporter run で `node_saturation: {"error": "..."}` になっていても数回で自愈する。