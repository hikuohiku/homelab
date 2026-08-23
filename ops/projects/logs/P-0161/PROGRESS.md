# P-0161 — PROGRESS

## 経過

- initializer (2026-08-23): PROJECT.md を作成。受入 3 項目すべて failing を実測 (rc 1/1/1)。
  実装は未着手。
- worker セッション 1 (2026-08-23): プロファイル一式を作成。**verify 1・2 を green 自力実測**
  (`test -f … && grep -qE 'trifecta|三要素'` rc=0 / `python3 -m unittest
  ops.tests.test_private_data_profile` 22 tests OK)。CI discover 全体も 264 tests OK。
  作成物: `ops/profiles/private-data/{README.md, job.yaml, networkpolicy.yaml,
  secret-fixture.yaml}` + `ops/tests/test_private_data_profile.py`。
  **verify 3 (demo.json) は未着手** — 下記「次のセッションへ」参照。

## 実測で分かったこと

1. **NetworkPolicy は Pod 単位で、同一 Pod 内のコンテナを区別できない。** spec dod の
   「model コンテナが egress deny-all 配下・同 Pod の publisher コンテナが push」は
   原理的に両立不可能 (push を許した時点で model も同じ経路を使える)。ゆえに
   **2 Pod 構成 (model Job / publisher Job) に読み替えた**。理由と根拠は
   README.md「設計判断の記録」に文書化済み — 段階 3 審査でも同じ議論が出るので先出し
2. **emptyDir は Pod 内にしか存在できない** → 受け渡しは使い捨て PVC `p0161-handoff`
   (local-path 64Mi, 撤収で消す)。「publisher は emptyDir 上の成果物だけを push」の
   字面は維持: publisher は PVC を readOnly mount で読み、push 入力は自分の emptyDir
   `/publish` 上に組み直す
3. **writer SA は secrets を一切触れない実測** (can-i create/update/get secrets = すべて no;
   apps/autopilot/rbac.yaml「secrets と RBAC は含めない」どおり)。initializer 前提の
   「demo Job の apply/delete は CLI で行える」は Secret を除いて正しい — jobs /
   networkpolicies / pvcs の create と server dry-run はすべて可を実測。
   external-secrets.io の create は可だが、fixture 値を Doppler に登録するのは人間作業なので
   結局 Secret apply のみ人間の 1 コマンドが必要 (迂回しない。ConfigMap 代用は dod 違反)
4. push 用 credential は既存 `autopilot-credentials` Secret の secretKeyRef 参照だけで
   足りる (新規 credential 作成不要)。git 認証は spawn.py の credential helper 型を踏襲し
   URL への token 埋め込み・`set -x` トレースを避けた
5. autopilot イメージ (digest pin, node01 pull 済み) には curl/bash/git があり、
   model の egress probe も publisher の push もこれ 1 本で動く

## 次のセッションへ

- **verify 3 (demo run → demo.json) は人間の 1 コマンド待ち**: writer SA では
  `kubectl apply -f ops/profiles/private-data/secret-fixture.yaml` が不可 (実測 3)。
  人間にこの 1 行を依頼する必要がある (issue #56 コメントか review 経由)。
  Secret が存在すれば残りはエージェント権限だけで完走できる:
  README「実行手順」のとおり NP → jobs を apply、wait、logs 収集、ops-feedback 着地確認、
  撤収 (`kubectl delete -f job.yaml && kubectl delete -f networkpolicy.yaml &&
  kubectl delete secret p0161-mail-fixture -n autopilot`)、demo.json 書き込み
- demo.json 形式: `{"run_at": "<UTC>", "egress_denied": true, "published_to_branch":
  "ops-feedback", "published_commit": "<sha>", "cleaned_up": true, "evidence": {...}}`
  - `egress_denied` は model logs の DENIED 行群 (curl rc=6=DNS 断 / rc=7=接続拒否 /
    rc=28=タイムアウト、+ /dev/tcp プローブ) を証拠に判定。**ALLOWED が 1 本でも出たら
    false にして実験失敗として記録すること** (偽りの完全性を作らない)
  - k3s netpol 効果はこの demo run が初実測になる。効かなければ egress_denied=false の
    失敗記録が正しい結果
- 罠: **PVC を消さずに再実行すると前回 run の sentinel (DONE) で古い成果物を push しうる**
  (README 再実行時注意)。apply 前 `kubectl delete -f job.yaml --ignore-not-found` 必須。
  誤着地検出用に digest.md には generated_at が入っている
- 罠: publisher script に `set -x` を足さないこと (token 漏えい面)。認証は credential helper
- テンプレート変更時は `.spec.template` immutable (delete → apply) と
  `test_private_data_profile` 22 本の更新を忘れないこと
