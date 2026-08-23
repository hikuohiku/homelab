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

- worker セッション 2 (2026-08-23): **Secret 未適用をクラスタ上で機械確定し、人間への
  依頼を issue #56 に投稿した** (実測の手順は下記「実測で分かったこと」6・7)。
  demo run の予行演習として NP → jobs の apply 経路と撤収経路を実走りした (両方 OK、
  撤収後の残骸 0)。verify 3 は依然 failing — 人間が Secret を apply するのを待つのみ。
  依頼コメント: [#56 (issuecomment-5384629207)](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5384629207)
  投稿 2026-08-23T06:30:10Z、再読取得で着地確認済み

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
6. **Secret の有無は「get secrets 不可」でも Job 経由で観測できる** (セッション 2 実測)。
   NP → jobs を apply すると、Secret 未存在なら model Pod が ~15 秒で
   `FailedMount: secret "p0161-mail-fixture" not found` を出して ContainerCreating 滞留
   (x8 over ~2 分を観測。CreateContainerConfigError へ遷移しないこともあり、イベント
   見るのが確実)。存在すればそのまま走り出す。次セッションの存在確認はこのプローブ 1 本でよい
7. demo run の apply/撤収経路は実走りで検証済み: NP → PVC → 2 Jobs の create がすべて可、
   `kubectl delete -f` (job.yaml → networkpolicy.yaml) で綺麗に消える (残骸 grep 0)。
   残る未実測は「Secret 存在時の Pod 一式の挙動」(egress deny 効果・push・sentinel) のみ
8. 罠 (セッション 2): **このサンドボックスでは `/tmp/opencode` が書き込み不可**
   (Permission denied)。一時ファイルは mktemp (既存規約どおり)
9. 罠: writer SA だと `kubectl get all` が rc/hpa の Forbidden で全体エラーに見える。
   リソース型を明示して (`get jobs,pods,pvc,netpol`) 確認すること

## 次のセッションへ

- **まず issue #56 とクラスタを確認する**: 人間への依頼は投稿済み (経過参照)。返信が
  無くても、Secret が apply 済みかもしれないのでプローブする (実測 6 の方法):
  1. `kubectl delete -f ops/profiles/private-data/job.yaml --ignore-not-found` (PVC 再作成
     強制。罠は下記)
  2. NP → jobs を apply、model Pod を ~2 分監視 (`kubectl describe pod … | tail`)
  3. `FailedMount: secret not found` が続くなら未適用 → delete して静かに待つ
     (重複コメントしない。依頼は既に出ている)
  4. 走り出したら README「実行手順」どおり完走させる:
     wait → logs 収集 → ops-feedback 着地確認 (`git fetch origin ops-feedback &&
     git show origin/ops-feedback:ops/feedback/demo/P-0161/digest.md`) → 撤収
     (job.yaml / networkpolicy.yaml / secret) → demo.json 書き込み
- demo.json 形式: `{"run_at": "<UTC>", "egress_denied": true, "published_to_branch":
  "ops-feedback", "published_commit": "<sha>", "cleaned_up": true, "evidence": {...}}`
  - published_commit は publisher logs の `published_commit=` 行から取る
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
