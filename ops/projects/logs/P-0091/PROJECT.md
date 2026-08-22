# P-0091 — タスク依頼 (kind: task-request) を立案の最優先原料にする器側配線

## 目的

OpenClaw (P-0090) やダッシュボードの書き置きから届く構造化タスク依頼を、curriculum
(立案) が最優先の原料として読めるようにする。今の器では依頼は triage の fall-through で
人間への briefing に積まれるだけで立案に流れない。「やって」と言われたことが消えるのは、
秘書としての本分の逆。VISION の自発立案と人間の依頼を接続する配線である。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing** (2026-08-22、`project/p-0091`
の checkout、リポジトリルートから実行)。

- [ ] `grep -q 'task-request' ops/prompts/curriculum-generate.md`
  — 立案生成プロンプトがタスク依頼の扱いを明記していること。実測 rc=1 (該当なし)。
- [ ] `grep -rq 'task-request' ops/heart/`
  — heart 側に task-request の識別・キュー管理のコードまたはテストがあること。
    実測 rc=1 (ops/heart/ 配下に該当なし)。

## 設計方針

### 前提 (initializer が 2026-08-22 に実読した事実)

- feedback note の取り込み口は `ops/heart/facts.py` の `collect_feedback()` (:129)。
  ops-feedback ブランチの `ops/feedback/inbox/` の JSON note を `json.loads` して
  **`body` だけ**取り出して triage している (:194-198)。トップレベルの `kind` フィールドは
  今は捨てられている。P-0090 とのインターフェイスはこの `kind == "task-request"` のみ
- 現状、task-request 相当は `triage.classify()` (決定論キーワードのみ) で何にも引っかからず
  `review_needed` になり、`heart.py:380` で briefing-queue.jsonl に積まれて終わり。
  curriculum には一切流れない
- キューの置き場所は ops-state ブランチ (単一書き手 = heart)。`cursors.json`
  (取り込み位置) と `briefing-queue.jsonl` (append ログ) という前例が
  `statefiles.py` にある (`append_jsonl` / `read_jsonl` / `rewrite_jsonl`, :133-160)
- curriculum への注入点は `runner.mode_curriculum()` (`ops/runner/runner.py:789`) が
  `prompt_text()` (:440) で行う `{{プレースホルダ}}` 置換。spawn 時の extra_env
  (`spawn.create(..., extra_env=...)`) から渡せる
- 採択・棄却の全案は `fix_to_archive()` (`runner.py:840`) が**案 JSON のフィールドを
  そのまま** archive.jsonl に追記する (`rec = dict(p)`)。案に `proposed_by` を持たせれば
  既存の流儀を壊さず記録できる (既存値: human-pilot / human-feedback)
- テストは遷移表 `ops/heart/tests/test_reconcile.py` + unittest。**pytest は Job イメージに
  無い** (P-0076/P-0078 の経緯)。CI も `python3 -m unittest discover -s ops/heart/tests -t .`

### 方針

1. **識別**: `collect_feedback()` で JSON note のトップレベル `kind == "task-request"`
   を見たら review_needed に落とさず、未処理キュー (ops-state 上の新 statefile。
   {id, source, body, received_at, status: pending}) に積む。id は source
   (ファイルパス / コメント id) から決定論的に導く
2. **注入**: heart が curriculum を spawn するとき未処理依頼を extra_env 経由で渡し、
   `curriculum-generate.md` に「人間の依頼は VISION 差分より優先して案に含める」ことを
   明記する。案のスキーマに `proposed_by: "human-request"` (任意。依頼由来のみ) を追加
3. **処理済み化**: 採択結果の取り込み (reconcile の consume_curriculum 周辺) で、採択された
   human-request 案と対応する依頼を processed にする。対応づけは決定論で (案に依頼 id を
   埋めさせる等)。**同じ依頼を毎回立案しない**ことが要件の中核で、遷移表テストに落とす
4. **テスト**: 識別 (triage/facts 系) と処理済み化 (reconcile 系) を unittest で追加

## やらないこと

- **OpenClaw / Telegram 側の実装** (P-0090)。こちらは note の `kind` フィールドを受け取る
  側だけを作る。並行作業なので相手の実装に触れない
- **issue #56 コメントの自由文を解釈してタスク依頼にする**こと。構造化は送り手の責務で、
  heart は決定論のまま (LLM を心臓に入れない原則、heart README「原則」節)
- **ダッシュボードへの未処理依頼表示・briefing の流路変更**。既存の review_needed 経路は
  壊さない (task-request はそこへ落ちる前の段階で分流するだけ)
- **依頼の優先度推定・分解・スケジューリング**の高度化。curriculum (LLM セッション) に任せて
  heart 側は運ばないものを作らない
