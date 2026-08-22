# P-0091 — 進捗ログ

## session1 (initializer)

PROJECT.md を作成。受入チェックリストの 2 項目とも現時点で failing を実測済み。
実装は後続の worker セッションで開始すること (initializer は実装しない)。

## session2 (worker)

**受入 2 項目とも自分で実行して green を確認済み** (2026-08-22、このブランチの
checkout で)。verify 全 green を wrapper が実測したらレビューに進んでよいはず。
単体テストは `python3 -m unittest discover -s ops/heart/tests -t .` で 181 件全 OK
(新規 test_tasks.py + test_facts.py、test_reconcile.py に遷移 4 件追加)。

### やったこと

- **ops/heart/tasks.py (新規)**: キュー管理の純関数のみ。`make_id` (source の
  sha256[:16] — 決定論)、`merge_new` (id 重複を落とす冪等取り込み)、`pending` /
  `for_env` (古い順に最大 20 件・本文 1000 字切り。空でも `"[]"` を返して置換を常に成立)、
  `mark_processed` (冪等。processed_at を刻む)、`done_ids` (採択 spec 群から request_id 収集)
- **facts.collect_feedback()**: JSON note のトップレベル `kind == "task-request"` を
  識別し、review_needed に落ちる直前で第 5 戻り値 task_requests に分流。
  **戻り値が 5 要素から 6 要素になった** (呼び出し元は heart.beat のみ)
- **heart.py**: 新着依頼を ops-state の新 statefile `task-requests.jsonl` に積む
  (briefing-queue と同じ位置・同じ流儀) / spawn_curriculum 時に未処理依頼を
  `TASK_REQUESTS` env で注入 / 新 action `mark_task_requests_done` を execute で処理
  (shadow ではログのみ — consume_* と同じ扱い)
- **reconcile.decide()**: curriculum PR merge 実測ビートで、採択 spec に request_id が
  あれば `mark_task_requests_done` action を出す (consume_curriculum と同じ分岐)。
  判断は純関数、書き込みは heart.execute — 既存の分業を守った
- **runner.mode_curriculum()**: `TASK_REQUESTS` env を prompt 置換に渡す
- **curriculum-generate.md**: 「人間のタスク依頼」節を追加 (`{{TASK_REQUESTS}}`
  プレースホルダ + VISION 差分より優先 + proposed_by/request_id のスキーマ追記)
- **curriculum-judge.md**: human-request 案を失格条件以外で優先採択する 1 節を追加

### 設計判断 (次セッションはここを読め)

1. **停止系キーワードは task-request より先に評価する**。依頼本文に「やめて」等が
   混ざっていても stop_all/veto が勝つ (P-0090 の絶対条件「停止/veto 判定は heart の
   triage に任せる」を heart 側でも担保)。test_facts.py で固定済み
2. **棄却された案の依頼は pending のまま** (spec 要件は「採択されたら処理済み」だけ)。
   同型案の出し直し抑止は (a) archive.jsonl の既出案が生成役に見えること、(b) judge の
   優先採択節、に任せる。judge.md への追記は spec の文言には無いが、「採択されなければ
   永遠に pending → 毎回再立案」のループ防止に必要と判断して入れた。スコープ過剰と
   レビューで言われたら外せるのはこの 1 節だけ
3. 処理済み化の対応づけは案に埋まった `request_id` の一致のみ (LLM の申告ではなく
   id の一致という決定論)。未知の id・既に processed の id は mark_processed が無視する
4. issue コメント (#56) は自由文なので kind を読まない。分流は ops-feedback ブランチの
   JSON note のみ。P-0090 とのインターフェイスは `kind` フィールド 1 つのまま

### 発見 (仕様外。curriculum が拾うなら拾って)

- **本ブランチの checkout で、クリーンな HEAD からも
  `ops.runner.tests.test_quota_flow.TestWorkerLoopQuota.test_three_usage_limits_are_not_three_consecutive_errors`
  が FAIL する** (git stash で実証済み。P-0091 の変更とは無関係)。slept 記録に
  DEFAULT_QUOTA_WAIT_SECONDS=900 でなく指数バックオフ風の列が混ざる。CI は
  unittest discover なので main でも落ちている可能性がある
- `for_env` の上限 (20 件 / 本文 1000 字) は安全側の決め打ち。運用で溢れたら
  rules.json 化を検討 (今は rules 触らず定数のまま)

### 次のセッションへの一言

レビュー指摘が来たら上記「設計判断」を先に読むこと。指摘がなければやることは無いはず
(実装完了・verify green 済)。エンドツーエンドの実走 (OpenClaw からの実 note → 立案 →
採択 → processed 化) は P-0090 の送り手側が上がってきてからの初回ビートで初めて観測できる。
その時は `task-requests.jsonl` が ops-state に生えているかと、audit.jsonl の
`mark_task_requests_done` を確認すること。
