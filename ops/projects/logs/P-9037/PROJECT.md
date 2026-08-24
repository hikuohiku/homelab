# P-9037 — ノードが沈む直前に何も言わない家を直す (改) — CPU 飽和前兆の常設計器を load 取得源まで閉じて実装する

## 目的

2026-08-24 18:18 JST、runner×2 + curriculum + heart の requests 合計 3761m/4000m でホスト load 25 になり
kube-apiserver も sshd も応答不能になった (ops/rules.json の `_max_concurrent_comment` 実測記録)。
スケジューラは超過分を Pending にするが、その前に「もうすぐ沈む」を告げる計器が無い。
容量の逆算は rules.json に書かれたが、実時系列で予兆を観測する経路が無いため、CPU 飽和前兆の
常設計器を作る。P-9029 (同題・metrics.k8s.io 前提) は審査で「load の取得源は metrics.k8s.io に直接
無いため、kubelet summary API か node の /proc 経由と明記すると spec が閉じる」と improve_hint され
却下された。本プロジェクトはその教師信号への応答で、load 取得源を kubelet stats/summary に固定する。

## 受入チェックリスト

initializer が 2026-08-24 に `project/p-9037` checkout のリポジトリルートから実行した結果、
**2 項目とも現時点で failing**。

- [ ] `python3 ops/tools/node_saturation.py --check`
  — 常設計器本体が実在し、`--check` (自己検査) が rc=0 で終わること。
  実測 rc=2 (ファイル未存在)。
- [ ] `python3 -m unittest ops.tests.test_node_saturation`
  — 08-24 の実測値 fixture (3761m/4000m・load 25) で「この値なら警告が出る」ことを
  単体テストが固定していること。
  実測 rc=1 (モジュール未存在 / ImportError)。

**verify は DoD の下限であって DoD そのものではない。** spec の dod どおり、
(1) kubelet stats/summary API (fallback: /proc/loadavg) から CPU requests/allocatable と load を実測し、
閾値超過で exit 1、 (2) 08-24 実測値 fixture で警告を固定、 (3) 結果を health レポート /
dashboard の warnings へ配線、 (4) load 取得源の実測結果を substrate.md に追記、までが本体。

## 設計方針

### 前提 (initializer が 2026-08-24 に実読・実測した。調べ直さなくてよい)

- **08-24 の実測記録**: ops/rules.json `_max_concurrent_comment`。runner×2 (1012m+959m) +
  curriculum (956m) + heart (834m) = 3761m/4000m で load 25。容量の逆算もここにあり
  (4000m - 常駐約 350m - 他 ns + k3s 約 1350m ≒ 2300m → 1 コア Job は 2 本)。
- **node01**: 単一ノード 4 vCPU / 11.7 GiB allocatable (ops/memory/substrate.md, verified 2026-08-06)。
- **load の取得源**: metrics.k8s.io は CPU usage を返すがホスト load は無い (P-9029 の審査指摘)。
  kubelet stats/summary は `GET /api/v1/nodes/<name>/proxy/stats/summary` (RBAC は get/proxy、
  P-9002 が同 API の node.startTime を読む実績)。load 自体は summary にも直接無いため
  node の /proc/loadavg を fallback にする (spec dod (1) の既定。実測してどちらが取れるか
  substrate.md に書くのが dod (4))。
- **CPU requests / allocatable**: requests は pod spec の `spec.containers[].resources.requests.cpu`
  の合計、allocatable は node status の `status.allocatable.cpu`。どちらもコア API で取れ、
  metrics.k8s.io は要らない。
- **配線先**: 健全性レポートは apps/ops-health-reporter/report.py が ConfigMap
  `autopilot/ops-health-report` の latest.json に書く (既存の collect_nodes / collect_node_metrics /
  collect_pod_metrics と並ぶ collect 関数を足す形が既存パターン。P-0128 の budget 警告は
  latest.json の異常フィールド → heart の briefing の 2 段階)。dashboard 側は
  apps/ops-dashboard/app/src/lib/snapshot.ts:76 の warnings 配列 (state.warning / kube.warning の結合)
  が page.tsx:404 で描画される。ここまで通すのが dod (3)。

### 決めてあること

- **ツールは単体で動く標準ライブラリ構成** (report.py と同じく pip install 不要)。
  クラスタ到達に ServiceAccount トークンを使い、`--check` は同梱 fixture / 引数検証のみで
  ネットワーク非依存にする (P-9002 の restart_wave.py --selftest と同じ思想)。
- **計算は純関数化**し、08-24 実測値 (3761m/4000m・load 25) を ops/tests の fixture にした
  単体テストで「この値なら警告が出る」を固定する。閾値は rules.json の逆算を根拠に
  allocatable の 90% 超 または load > vCPU 数 (P-9029 の dod 踏襲)。
- **substrate.md への追記 (dod (4))**: kubelet stats/summary で load が取れたか /
  /proc/loadavg に倒したかを verified_at 付きで実測記録する。この節は P-0015 / P-0026 / P-0101 と
  同様、spec の DoD が名指しで要求する「書き手は consolidation の PR のみ」の例外。

## やらないこと

- **P-9029 の全消しではないが、retention・スケジューリング変更はしない**。本プロジェクトは
  「観測して告げる」計器の追加だけ。max_concurrent の変更・Job 数の調整・node01 の vCPU 増設は
  rules.json / インフラ側の論点で、ここでは触らない。
- **Pending Pod の自動ドレイン・eviction などの「対処」は実装しない**。告げるだけ (spec は
  常設計器を要求している)。
- **apps/ の manifest 変更は配線に必要な最小限に閉じる** (report.py + dashboard の読み手側のみ。
  新規 CronJob / 新規 Deployment を足すかどうかは worker の実測判断で、足す場合は 1 PR 1 論点)。
- **閾値を rules.json の逆算以外から再導出しない**。根拠は 08-24 実測 + rules.json の逆算に固定。
- **他の node (node02 以降) への展開・multi-node 対応はしない**。node01 単一ノード前提
  (substrate.md の実測どおり)。
- **ops/backlog.json / ops/state.json / ops/journal/ の更新**。heart が直接 push する領域で
  コンフリクトする (CLAUDE.md)。