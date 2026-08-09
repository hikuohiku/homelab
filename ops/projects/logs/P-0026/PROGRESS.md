# P-0026 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## 現況

DoD (1)〜(6) を実装済み。**レビュー指摘 [1][2][3] は s2 で全部潰した**。
**受入 5 項目とも自分で実測して green** (2026-08-09。rc は 5 項目とも 0)。
heart 104 tests / runner 28 tests / ops job の他スクリプト全部 rc=0 も実測済み。
残りは wrapper の再実測と再レビュー。**このブランチは `ops/runner/` と `.github/` を
触るので ruleset の人間レビュー必須パスに当たり、auto-merge されない。**

## セッション記録

### s1 (2026-08-09) — 実装 3 コミット、受入 5/5 green

やったこと (PROJECT.md の設計方針をほぼそのまま実装した。設計との差分は下の「発見」に書いた):

1. `65a8784` runner 本体 (DoD 1〜4)
   - `Session.run()` の `stderr=subprocess.DEVNULL` → `subprocess.PIPE`。専用デーモン
     スレッドで読み切り、`collections.deque(maxlen=400)` に末尾行だけ保持。
     **stderr の到着では `last_event` を更新していない** (活動の定義は stdout のまま)。
   - 純関数 3 本をモジュール直下に追加: `classify_session_failure` /
     `parse_usage_limit_reset` / `mask_secrets(text, env)`。パターンは
     モジュール定数 `FAILURE_PATTERNS` (順序 = 判定順)。
   - `Session.run()` の戻りを `(outcome, usage, info)` に拡張、`run_session()` が
     `self.last_session = info` に置く (呼び出し側の署名は不変)。異常終了系の
     `write_result` に `failure_kind` / `stderr_tail` を載せた。
   - `mode_worker()` の while に usage_limit 枝。連続 error に数えず `time.sleep` で
     待って再開、待機予算 (`session_max_seconds`=7200) を超えるなら `waiting_quota` +
     `resume_after` を書いて **rc=0** で終える。
   - 上限で即死した回に 50,000 トークンの概算を付けない。
   - `ops/runner/tests/test_exit_reason.py` (+ `__init__.py`) — 18 tests。
2. `968f1e9` heart 側の最小分岐 (`ops/heart/reconcile.py` の `active` 枝) と
   `ops/heart/tests/test_reconcile.py` に `TestQuotaWait` (6 本)。全 101 tests green。
3. (このコミット) `ops/memory/substrate.md` に「claude セッション / 利用上限」節、
   `.github/workflows/ci.yml` の ops job に runner の unittest ステップ。

### 分かったこと / 実測した罠

- **`result` イベントの本文を無条件に分類へ混ぜてはいけない。** 成功した回の
  `result` フィールドは最終アシスタント本文であり、本文が上限の話題に触れているだけで
  `usage_limit` に誤分類する (まさにこのプロジェクトの worker が該当する)。
  `is_error` か `subtype != "success"` の回だけ拾うようにした。
- **偽 `claude` を PATH に置いた実機スモークを 2 本回した** (`mktemp -d` にスクリプトを置く)。
  (a) stderr に `ghp_…` + `Claude AI usage limit reached|1754697600` を吐いて rc=1 →
  `failure_kind=usage_limit` / `stderr_tail` は `***` にマスク済み / `reset_at` 復元 /
  tokens は 0 のまま (概算なし) を確認。(b) stderr 200KB を吐いて正常終了 →
  デッドロックせず `completed` / tokens 150。**stderr を読み捨てない実装の回帰検査に
  そのまま使えるので、次に触るときも同じ手が使える。**
- `parse_usage_limit_reset` の epoch `1754697600` は 2025-08-09 (2026 ではない)。
  テストの期待値もそれで固定してある。

### 設計 (PROJECT.md) からの意図的な差分 — レビューで見るならここ

- **`quota_wait_until` の再開判定で `running >= max_concurrent` を見ていない。**
  PROJECT.md は「既存の spawn 抑制と同じ扱い」で両方見ろと書いているが、
  `rules.runner.max_concurrent` は **1** で、待機中のプロジェクト自身が `active` として
  `running` に数えられている。見ると `running(1) >= 1` が常に真になり **永久に再開できない**。
  スロットは既に自分が占めているので再開しても同時実行数は増えない。breaker 抑制だけ残した。
- **待機中は job 梯子より先に `quota_wait_until` を評価している。** `waiting_quota` は
  rc=0 終了なので Job は succeeded で残る (active でも failed でもない)。梯子に落とすと
  TTL で Job が消えた時点で `job is None` → drift → 3 ビートで stalled になり、
  上限対策そのものがループを止める。テスト `test_waits_silently_until_the_deadline` が これを固定する。
- **breaker 中は `quota_wait_until` を消さずに保持する。** 消してから何もしないと次のビートで
  梯子に落ちて即 respawn してしまう。

### 発見 (スコープ外。curriculum が拾う想定)

- `FAILURE_PATTERNS` は**まだ実測されていない候補**。本 PR が merge されて初めて実際の
  文言が `stderr_tail` に残る。**merge 後、最初の異常終了回の result.json を見て
  表を実文字列で置き換える**フォローが要る (substrate.md にもその旨を書いた)。
- `auth` / `network` は記録するだけで制御を変えていない (spec の「やらないこと」通り)。
  認証失敗の即時打ち切りは、実 `stderr_tail` が集まってから別プロジェクトで。
- `ops/validate.py` が backlog について warning を 2 件出す (T-0035 の refs 切れ / todo 0 件)。
  P-0026 とは無関係の既存 warning で、rc は 0。

### 次のセッションへの一言

**実装はもう無い。** 受入 5/5 は自分で実測して green、heart 101 tests / runner 18 tests /
`ops/validate.py` / ops job の他スクリプト (version_sync, pvc_usage, health_reporter_target,
doc_commands, feedback, dashboard build) も全部 rc=0 を確認済み。
次のセッションが起きたなら、それは wrapper の再実測が落ちたかレビューの差し戻しがあったとき。
**まず `VERIFY_STATUS` と `REVIEW_FINDINGS` を読むこと**。何も指摘が無いのに起きた場合は、
上の「設計からの意図的な差分」3 点が争点になっている可能性が高いので、そこを説明する形で直す。

### s2 (2026-08-09) — レビュー指摘 [1][2][3] を 1 コミットで解消

**争点は s1 が予想した「設計からの意図的な差分 3 点」ではなかった。** 指摘はどれも
「上限を停滞と読み違えない」を **1 箇所しか直していない**という指摘だった。
上限が制御に効く経路は **initializer / worker ループ / reviewer の 3 つ**あり、
s1 は真ん中しか直していなかった。加えて、待ちを無限にしてしまっていた。

- **[1] 上限待ちが恒久的に黙って待つ状態になっていた** (`ops/heart/reconcile.py`)
  - `QUOTA_WAIT_MAX_ROUNDS = 6` を他の見張り時限 (REVIEW_TIMEOUT_HOURS 等) の隣に置き、
    `waiting_quota` を消費するたび `p["quota_wait_count"]` を進める。超えたら
    `quota_wait_exhausted` + `notify=question` で人間に渡す (budget_exhausted と同じ流儀)。
  - **リセットは「waiting_quota 以外の result が来たら」**。result 分岐の手前に 1 行置いた
    (各枝に散らすと漏れる)。
  - **stall する時は `quota_wait_count` も `quota_wait_until` も落とす。** 残すと人間が
    active に戻した次の `waiting_quota` で即また stalled になる = 再開できない停止になる。
    テスト `test_repeated_quota_waits_are_bounded` がこれを固定。
  - ダッシュボード (`ops/dashboard/build.py`): `state == "active"` かつ `quota_wait_until`
    があれば chip を「上限待ち」/ tone=warn に差し替え、「再開 ○○後」を meta に出す。
    **PROJECT_STATE_META の語彙は増やしていない** (state は active のままが正しいので、
    active の中の表示分岐にした)。
- **[2] initializer が上限で死ぬと従来通り stalled + incident だった** (`ops/runner/runner.py`)
  - 待機ロジックを `Runner.quota_wait_or_yield(waited, budget, **result_kw)` に括り出し、
    initializer と worker ループの**両方**から呼ぶ。戻りは `(累積待機秒, rc)` で、
    rc が None なら再試行、int なら `waiting_quota` 書き込み済みでその rc で終える。
  - initializer を `while True` にして上限なら待って**同じ initializer を出し直す**。
    待機予算は worker ループと同じ財布 (`quota_waited` を先に初期化して共有)。
  - **`max_sessions_per_project` の歯止めは外さない。** 初期化ループの先頭で
    `budget.exhausted()` を見て `budget_exhausted` に落とす (待機予算とは別軸の上限)。
  - initializer / curriculum generate / curriculum judge の `error` 文字列に
    `failure_kind=...` を入れた (incident 通知の本文に出るのは error フィールドだけ)。
- **[3] mode_review が上限で死んだ回を「レビュー不合格」に読み替えていた**
  - 純関数 `should_withhold_review(failure_kind, review_exists)` を追加。
    `usage_limit` かつ review.json が**無い**ときだけ、**何も書かずに rc=1 で終える**。
    heart 側の既存の見張り (`REVIEW_TIMEOUT_HOURS` 2h × `REVIEW_MAX_RETRIES` 2) が再試行する。
  - **reviewer が verdict を書き切ってから上限で死んだ回は、その verdict を活かす**
    (review.json が在るなら有効な結果)。`test_verdict_written_before_the_limit_is_kept` で固定。
  - result.json は書かない。in_review の heart は review.json しか見ないので、
    書くと誰も consume しないゴミが残る。

新規テスト `ops/runner/tests/test_quota_flow.py` (10 本)。`Runner.__init__` は git/rules/gh を
触るので、**`__init__` を通さない殻 (`FakeRunner`)** にセッション結果列 `(outcome, failure_kind)` と
verify 結果列を与えて分岐だけを動かす。`test_exit_reason.py` が「文字列 → 死因」なら、
こちらは「死因 → 何をするか」。`ops/heart/tests/test_reconcile.py` の `TestQuotaWait` に 3 本追加。

### s2 で踏んだ罠

- **`mode_worker` は `REVIEW_FINDINGS` を環境から読む。** runner Job の中でこのテストを
  走らせると、差し戻し中の findings を拾って `findings_pending` が真になり、
  verify が全 green でも 1 セッション余分に回る。**実際にこのセッションで踏んで**
  最初のテストが `IndexError: pop from empty list` で落ちた。
  `mock.patch.dict(R.os.environ, {"REVIEW_FINDINGS": ""})` で環境から切り離してある。
  **runner の中で runner のテストを書くときは環境変数を必ず固定すること。**
- `FakeRunner.repo_dir` は `R.REPO_ROOT` (本物) にしてある。`mode_worker` / `mode_review` が
  `git log` / `git diff` を実際に打つので、tmp を渡すと非 git ディレクトリで落ちる。
  書き込み系 (checkout_branch / push_if_committed / ensure_pr) は全部差し替えてあるので、
  リポジトリは読むだけ。**project_dir / doc_dir は tmp**。
- **`python3 ops/dashboard/build.py` を手で回すと `ops/dashboard/prs.json` が
  live の PR データで書き換わる。** これは dashboard CronJob の生成物であって
  この PR の変更ではないので、`git checkout -- ops/dashboard/prs.json` で戻すこと。
  2 回踏んだ (CI の再現で build.py を回すたびに出る)。

### 発見 (スコープ外。curriculum が拾う想定)

- s1 の発見は全部そのまま有効: **`FAILURE_PATTERNS` はまだ実測されていない候補**で、
  merge 後の最初の異常終了回の `result.json` を見て実文字列に置き換えるフォローが要る。
  誤検知した場合の受け皿が `QUOTA_WAIT_MAX_ROUNDS` (人間に渡る) になったので、
  s1 の時より安全側にはなった。
- `mode_oneshot` (consolidation / critic / chore) の error は `error` フィールドを持たず
  `outcome` だけ。まだ spawn 配線されていないモードなので今回は触っていない。
  配線するときに failure_kind と上限待ちを一緒に入れること。
- `QUOTA_WAIT_MAX_ROUNDS` の数え方は「連続」の定義が粗い: 間で runner が実際に仕事を
  していても、result が `waiting_quota` 以外を出さない限り数え直されない。上限が長く続く
  期間に productive なプロジェクトが 6 回で人間に回る可能性がある (害は question 1 通)。
  実運用のデータが出てから調整する話。
- `ops/validate.py` の backlog warning 2 件 (T-0035 の refs 切れ / todo 0 件) は既存。rc は 0。

### 次のセッションへの一言

**実装はもう無い。指摘 3 件は全部コードとテストになっている。**
次に起きたなら、まず `VERIFY_STATUS` と `REVIEW_FINDINGS` を読むこと。
再指摘が無いのに起きたなら、争点になりうるのは以下 (どれも意図的な判断):

1. `QUOTA_WAIT_MAX_ROUNDS = 6` という数字の根拠 — 実測ではなく「review 2h×2 /
   adopt_gate 3 回」と同じ桁感で置いた。上限のリセット周期 (5h) × 6 ≒ 丸 1 日待って
   駄目なら人間、という読み。
2. 上限超過で **stalled にする**判断 (question 通知だけにしなかった) — `max_concurrent=1`
   でスロットを塞ぎ続ける方が害が大きく、reconcile.py 冒頭の不変条件も
   「他の待ちは全部有界」を要求しているため。stalled にすればスロットが解放される。
3. reviewer の上限死で **review.json を書かず rc=1** にした判断 — heart 側に既に
   reviewer の見張りがあるので、新しい待ち状態を作らずに既存の再試行へ寄せた。
   s1 の「設計からの意図的な差分」3 点 (quota_wait_until を job 梯子より先に見る /
   max_concurrent を見ない / breaker 中も札を保持) も**まだ有効で、そのまま残っている**。
