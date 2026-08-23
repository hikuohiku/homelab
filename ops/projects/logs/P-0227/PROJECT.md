# P-0227 — curriculum Job が今日だけで 3 回 Failed — 立案の入口が黙って死ぬのを検死し、死因ごとに「書き出す前に死なない」構造を入れる

## 目的

health 実測 (2026-08-23T18:30Z) で curriculum Job の Pod 3 本 (curriculum-system-a791568 /
a791735 / a791795) が Failed。curriculum は全プロジェクトの源泉であり、ここが**誰にも気づかれずに**
死ぬのは VISION 第一原理「ループが止まらないこと」の直撃 (2026-08-22 には 34 分の調査が
1 案も残さず消えた前例がある)。まず検死で死因を実測で断定し、その死因に合った層に
構造的な歯止めを 1 つ入れる。prompt 改善は既に試みられており (curriculum-generate.md の
「締めの義務」, 2026-08-22 追記)、それでも死んだ — 死因が usage_limit や timeout なら
prompt 層では治らない。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23 19時台、`project/p-0227` の checkout でリポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -s ops/projects/logs/P-0227/failures.md && grep -q '^root_cause:' ops/projects/logs/P-0227/failures.md`
  — 検死報告 failures.md が存在し、`root_cause:` 行で死因を failure_kind 6 種
  (usage_limit / auth / network / budget / timeout / unknown) のいずれかに文言断定していること。
  実測 rc=1 (ファイル未存在)。
- [ ] `test -n "$(ls ops/projects/logs/P-0227/raw-* 2>/dev/null)"`
  — 直近 Failed Pod の生証跡 (pod log / result.json / transcript 断片) が raw-* として
  少なくとも 1 件保存されていること。加工せず生のまま。
  実測 rc=1 (該当ファイル無し)。
- [ ] `python3 -m unittest ops.tests.test_curriculum_resilience`
  — 死因分類と「歯止めの発火条件」が unittest で固定され green であること。
  実測 rc=1 (モジュール未存在)。

## 設計方針

### 前提 (initializer が 2026-08-23 にコード読解で確認。調べ直さなくてよい)

- **curriculum Job の解剖**: heart が `spawn.build_job(kind="curriculum")` で Job を出す
  (名前プレフィックス `curriculum-`, **backoffLimit: 0, activeDeadlineSeconds: 259200 = 72h**,
  ops/heart/spawn.py:130)。Job の中身は runner.py `mode_curriculum()`
  (ops/runner/runner.py:1013) の 2 段構え — generate セッションが `/work/proposals.json` を書き、
  judge セッションが `/work/adopted.json` を書き、PR を作って
  `/data/projects/system/result.json` に `curriculum_done` を残す
- **「黙って死ぬ」経路は 2 本ある**:
  (a) セッション異常終了なら result.json に state=error + failure_kind が残るが、
  **usage_limit 待機機構 (`quota_wait_or_yield`, runner.py:697) は `mode_worker` にしか配線されていない**
  (runner.py:854/914)。curriculum は上限に当たっても待たず即 error 死する (P-0026 の教訓が
  curriculum に未適用);
  (b) write_result 前にプロセスごと死んだ場合は result.json 自体が無く、
  `facts.collect_curriculum()` (ops/heart/facts.py:368) が None を返して
  **reconcile は何も通知しない**。backoffLimit=0 なので再試行も無い。
  72h の activeDeadlineSeconds は検知の歯止めとしては機能しない
- **死因分類の現状**: `classify_session_failure()` (runner.py:61 の FAILURE_PATTERNS) は
  usage_limit / auth / network / unknown の 4 種のみ。timeout は outcome `session_timeout`,
  budget は state `budget_exhausted` として別層で表現される — DoD の 6 分類への対応は
  この層跨ぎのマッピングを含む。**opencode の HTTP 429 は UnknownError に潰されて
  usage_limit に分類不能な実測がある** (ops/memory/substrate.md「claude セッション / 利用上限」節)。
  歯止めの発火条件は正確な死因分類に依存させてはいけない
- **既存テストの型**: ops/tests/test_failure_patterns.py (fixture → 純関数の分類を固定) が
  DoD(4) の雛形。純関数 + fixture で発火条件も固定する

### 作り方

1. **証跡収集 (DoD 1)**: Failed Pod の `kubectl get pod -o json` / logs / events、
   残っていれば result.json と transcript 断片を取得し、`ops/projects/logs/P-0227/raw-*`
   に生のまま保存
2. **検死 (DoD 2)**: substrate の死因表と突き合わせて failure_kind を断定し、
   failures.md に `root_cause:` 行 + 証拠への参照を書く。断定できないなら unknown と
   書く (捏造しない)
3. **歯止め (DoD 3)**: 断定された死因に対し最も安い層で再発条件 1 つを潰す。候補と目算:
   - runner 層 (本命): `mode_curriculum` に `quota_wait_or_yield` を配線する
     (worker パスの mirror)。usage_limit 系の死に効く
   - heart 層: result.json 未写出の curriculum Job を時刻で検知して早期に潰し直す
     (走行中 Job の観測は reconcile.py:569 附近に既にある)
   - prompt 層: 既に「締めの義務」があるので原則追加しない
   選んだ層と他の層を避けた根拠を **PROGRESS.md** に書くこと (spec の要求)
4. **テスト (DoD 4)**: 死因分類マッピング (6 種) と歯止めの発火条件を純関数に切り出して unittest 固定

## やらないこと

- **実装の先行着手はこのセッションではしない** (initializer の仕事はこの文書まで)
- **prompt 文言の追加改善を主対症にしない** — 死因が構造系 (usage_limit/timeout/budget) なら
  効かないことが 2026-08-22→23 の実績で示済み。証跡が明確に prompt 層を指すときのみ最小限検討
- **worker / reviewer 経路への手入れ、quota 機構の一般化リファクタリング** — 対象は curriculum の
  再発条件 1 つに留める (1 PR 1 論点)
- **FAILURE_PATTERNS への未観測パターンの追記** — substrate の規則。実測した stderr_tail /
  error メッセージを証拠に足すこと。捏造禁止
- **監視スタックの新設・ops-health-reporter の改修** — 別論点
- **judge / adopt ロジックや立案品質への介入** — 今回の論点は「死なないこと」であり「良くなること」ではない
