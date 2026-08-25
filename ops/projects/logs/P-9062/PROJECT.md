# P-9062 — ディスクは CPU と違い『静かに満杯』になる — node01 ルートディスクの内訳実測と残り日数予報を初めて出す

## 目的

node01 の root disk は 256GiB（2026-08-04 拡張）で、`local-path` の PVC は要求容量を実ディスクに予約しない。
`pvc-usage-reporter` は 3 namespace の PVC 使用量しか見ず、「ルートディスク全体が何に食われ、いつ満杯になるか」を測る装置が無い。
ディスク満杯は CPU 飽和（P-9037）と違い k8s が書込み不能になる致命的な死因で、しかも徐々に進行するため静観しやすい。
P-9037（CPU 前兆）のディスク版として、内訳実測と fill 予報を初めて出す。

## 受入チェックリスト

initializer が 2026-08-25 に `project/p-9062` checkout のリポジトリルートから実行した結果、
**2 項目とも現時点で failing**。

- [ ] `kubectl get cm -n autopilot ops-health-report -o jsonpath='{.data.latest.json}' 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("root_disk") and "fill_days" in d["root_disk"]'`
  — health レポート (ConfigMap `autopilot/ops-health-report` の `latest.json`) に `root_disk` 節があり、
    `fill_days`（残り日数）を持つことを確認している。
    実測 rc=1（kubectl がクラスタに到達できず空入力 → JSONDecodeError。また現在の report.py は
    `root_disk` キーを一切書かないため、到達できても assert が通らない）。
    **2026-08-25 追記（mock apiserver + 実 kubectl で実測）**: クラスタ到達が解決してもこのコマンドは
    green にならない。実 kubectl の jsonpath は `.` を入れ子区切りと解釈するため `{.data.latest.json}` は
    空出力になり、JSONDecodeError は消えない。リテラルのドットキーは `{.data.latest\.json}` とエスケープする
    必要がある（ops/CHARTER.md §5.5 の実測済み読み方）。**spec の verify[0] 自体の修正（エスケープ）が
    必要**で、これは worker の立場では直せない（詳細は PROGRESS.md 2026-08-25 のセッション記録を参照）。
    この事実は **実 kubectl + mock apiserver** のテストで CI 固定済み
    （test_report_root_disk.py の test_real_kubectl_spec_verify_verbatim_unsatisfiable /
    test_real_kubectl_escaped_verify_passes）。
- [ ] `python3 ops/tools/root_disk_usage.py --check`
  — 内訳実測ツールが実在し、`--check`（ネットワーク非依存の自己検査）が rc=0 で終わることを確認している。
    実測 rc=2（ファイル未存在）。

**verify は DoD の下限であって DoD そのものではない。** spec の dod どおり、
(1) ルートディスクの使用内訳（k3s/containerd/イメージ/local-path PVC/ログ）を実測し、
(2) 日次増加量からの満杯予報（残り日数）を latest.json の `root_disk` 節に載せる、までが本体。
取得源は kubelet stats/summary の node.fs（RBAC 追加）か node01 /proc 実測のうち検証できた方。

## 設計方針

- **既存パターン（P-9037 が直近の同型）**: `ops/tools/<tool>.py`（canonical）を書き、
  `apps/ops-health-reporter/<tool>.py` に同一コピー（configMapGenerator が /scripts へ載せ report.py が import）、
  drift は `ops/check_<tool>_sync.py`（CI）で検出、`report.py` に collect 関数を足して
  latest.json へ書く、の 4 点セットが確立済み（apps/ops-health-reporter/kustomization.yaml, rbac.yaml）。
- **総使用量の取得源の二択**: kubelet stats/summary `node.fs`（availableBytes/capacityBytes/usedBytes）は
  `nodes/proxy` RBAC が要る（reporter には未付与、P-9037 の substrate.md で「未実測」と記録済み）。
  fallback は node01 の /proc・df 実測。**どちらが取れるか実測で決める**（P-9037 の load と同じ流儀）。
- **既知の罠**: `nodes[].allocatable/capacity` の ephemeral-storage は実ルートディスクと別物
  （約 252GiB 実容量に対し約 48.9GiB しか出ない — report.py notes の T-0079 注記）。これを使ってはならない。
- **内訳（k3s/containerd/イメージ/local-path PVC/ログ）** は /var/lib/rancher/k3s 等の per-directory 実測が必要。
  非特権の reporter から何が読めるか（hostPath/du の可否）を実測し、読めない内訳は「計測不能」を正直に載せる。
- **fill 予報には履歴が要る**が、latest.json は最新 1 点のみで上書きされる（report.py 設計）。
  日次増加量の算出元（ConfigMap 内に前回値を保持する等）は worker が決める。
- ツールは標準ライブラリのみ（report.py と同じく pip install 不要）。`--check` は同梱 fixture で
  ネットワーク非依存に自己検査（node_saturation.py と同思想）。
- DoD は latest.json の `root_disk` 節まで。heart/dashboard への警告配線は DoD 外で、入れるなら別論点。

## やらないこと

- **満杯への「対処」は実装しない**（自動 eviction・image prune・容量拡張）。測って告げる計器の追加だけ
  （P-9037 と同じ。拡張手順は docs/node01-storage.md に既存）。
- **rules.json の max_concurrent 変更・node01 のディスク増設・terraform 変更はしない**。インフラ側の論点。
- **pvc-usage-reporter / 既存の per-namespace PVC 計測は触らない**。本プロジェクトはルートディスク全体の計器。
- **latest.json の履歴保持スキーマを既存キーに広げない**。履歴は root_disk の増加量計算に必要な最小限に閉じる。
- **apps/ の manifest 変更は配線に必要な最小限に閉じる**（1 PR 1 論点、CHARTER §3）。
- **ops/backlog.json / ops/state.json / ops/journal/ の更新**。heart が直接 push する領域でコンフリクトする（CLAUDE.md）。