# P-0231 PROGRESS

作業ログ。セッションごとに追記する。

## initializer (2026-08-23)

- PROJECT.md を作成。受入チェックリスト 5 項目は開始時に全項目 failing を実測済み (all-fail ゲート通過)。
- 実装はまだ。以降の worker セッションは PROJECT.md のチェックリストを埋めること。

## worker セッション 1 (2026-08-23) — 実装一式。残りは merge + heart 初回ビートのみ

### やったこと (コミット 3 本)

1. `444297ba5` — 台帳 `ops/reminders.json` (仮置き 3 件: ゴミ収集 none / 防災の日 year /
   年末締め year。個人情報なし一般語) + `ops/validate.py` に `check_reminders()` 新設
   (repeat は {year, none}、date は YYYY-MM-DD 厳密、date+title 重複禁止、>100 件で warning)
   + レンダラ `ops/life/reminders.py` + `ops/tests/test_reminders.py` (24 テスト)
   + render-sample.txt (実時刻・実台帳から生成。「今日 8/24 …」が出た — JST 起点が効いている)
2. `7d5685c1f` — heart ビートへの配線。`Heart.publish_reminders(now)` が repo_dir
   (sync_main 済み) の台帳から断片を作り state_dir/briefing/reminders.txt へ。
   既存 `commit_and_push_state()` の add -A に乗るだけ。beat 内は try/except で包み、
   台帳が壊れても本業の状態機械を落とさない。結合テスト `test_reminders_beat.py`
3. `7335acb20` — Mission Control。`lib/reminders.ts` (toRemindersView: 空判定だけ。
   due 計算は TS 側で複製しない) + ops-state.ts が briefing/reminders.txt を取得
   (**404 時は catch で空文字**。欠損で取得全体を失敗させない) + page.tsx に
   「次の予定」常設節 (タブによらず status-line 直下。空でも節は消さない) +
   「暦の種を募集」一文 + tests/reminders.test.ts

### 自己実測した verify の現状

- verify(1)(2)(4)(5): GREEN 実測 (unittest 24 本・dashboard npm test 8 本・tsc・next build も通し)
- **verify(3) だけ RED のまま = 想定内**: 公開は「merge → heart が次ビートで exec し直し →
  publish_reminders が書く」の順でしか起きない。単一書き手 = heart の原則なので、
  worker が ops-state に直接 push して先に green にはしないこと。
  **レビュー/merge 後、最初のビート (~120s) で green 化する。merge 直後の再検証では
  1 ビート分待つこと**

### 分かったこと・罠

- `ops/validate.py` は `import ledger` をスクリプト実行前提で書いているため、
  テストから `from ops import validate` するときは sys.path に ops/ を足す必要がある
  (test_reminders.py 冒頭に前例として書いた)
- heart の beat 結合テストでは、beat() 内部の now は実時刻になる。断片の中身まで
  固定 now で比較すると日付が変わった瞬間に落ちるので、「中身の一致」は
  publish_reminders() 直接呼びで見て、「ビートが運ぶこと」は存在確認だけで見る
  (test_reminders_beat.py の docstring 参照)
- dashboard の npm test/lint には node_modules が無い。`npm ci` を先に (`--no-audit`)
- 「今日/明日」は JST 起点で計算している (heart pod は UTC)。UTC のまま判定すると
  人間の夜 9 時以降に「明日」が入れ替わる。境界テストは 15:30Z → JST 翌日 で固定済み

### 次のセッションへの一言

実装は完了している。レビュー指摘が来たらその解消が最優先。verify(3) は merge 後の
heart ビート待ちであり、それ以外にやることはないはず (やることが出たらこの欄に追記)。

## worker セッション 2 (2026-08-23) — verify(3) の事前監査。merge 後 green 化を実証済み・コード変更なし

### やったこと

レビュー指摘は無し。唯一の failing 項目 verify(3) は「merge → heart 初回ビート」でしか
green にならないため、**push を伴わずにその通過を事前実証するリハーサル**をした。
コード変更は不要と判断 (変更ゼロがこのセッションの結論)。

1. `git worktree add --detach <tmp> origin/ops-state` で ops-state を /tmp に展開
2. 本物の `Heart.publish_reminders()` を**実台帳 × 実時刻 (beat と同一条件: UTC now)** で
   実行。state_dir だけ worktree へ差し替え (`h.state_dir = worktree`)
3. worktree 内で add -A + commit (**push はしない**) → `git show HEAD:briefing/reminders.txt`
   が通る = verify(3) と同じ判定式が通る形
4. 断片はレンダラ CLI 出力 (`python3 -m ops.life.reminders`) と一致:
   「今日 8/24 ゴミ収集 (仮置き)…」の 1 行。render-sample.txt とも整合

### リハーサルで確認できたこと

- publish_reminders が書くのは `briefing/reminders.txt` のみ (git status はそれだけ)。
  commit_and_push_state() の add -A で確実に載る
- beat の順序は sync_main → sync_state_branch → … → publish_reminders →
  commit_and_push_state (heart.py:502-507)。merge 後最初のビート (~120s) で載る
- state_dir のルート = ブランチルートなのでパス対応は正しい (ops-state 側に既存
  briefing/ は無いが mkdir で作られる)
- 台帳欠損時は黙ってスキップ。unittest 24 本 + test_reminders_beat.py 再実測 OK、
  validate.py も通過

### 分かったこと・罠

- **このサンドボックスでは /tmp/opencode が root 所有 755 で書けない**
  (autopilot uid 10001)。一時ファイルは `mktemp -d /tmp/xxx.XXXXXX` (/tmp 直下) を使う
- リハーサル手法は再利用可: merge 後に verify(3) が 2 ビート待っても green にならないとき、
  同じ手順を再実行すれば「レンダラ/配線が壊れた」か「heart pod 側 (sync_main, 再起動) か」を
  即切り分けできる
- 断片の「今日/明日」ラベルは描画時に確定する値。heart が止まれば古い表示のまま残るが、
  heart 止滞は既存の HEART チップが面するので二重の面張りはしなかった

### 次のセッションへの一言

コードは完成・変更不要。verify(3) は merge → heart 初回ビートで green 化する**ことを
リハーサルで実証済み**。レビュー指摘があればその解消が最優先。merge 後に red が続くようなら
上のリハーサルを再実行して heart pod 側を疑うこと。

## worker セッション 3 (2026-08-23) — ops-state 先端が進んだ後の再監査。リハーサル再通過・コード変更なし

### やったこと

レビュー指摘は無し。唯一の failing は verify(3) のまま。ブランチ未 merge を git で実測
(`7335acb20` は `origin/project/p-0231` のみに含まれ main 未到達)。一方 **origin/ops-state が
セッション 2 以降進んでいた** (00fafbc3f → 6b0776c32) ので、セッション 2 のリハーサルを
最新先端に対してやり直した。コード変更は不要 — 変更ゼロがこのセッションの結論。

1. `git worktree add --detach <mktemp> origin/ops-state` (先端 6b0776c32)
2. 実台帳 × 実時刻 (UTC now, beat と同一条件) で `Heart.publish_reminders()` を実行。
   state_dir を worktree に差し替え
3. add -A + commit (**push 無し**) → `git show HEAD:briefing/reminders.txt` 通過 =
   verify(3) と同じ判定式 OK。git status の差分は `briefing/` のみ
4. 断片はレンダラ CLI 出力 (`python3 -m ops.life.reminders`) と diff ゼロ一致:
   「今日 8/24 ゴミ収集 (仮置き): 実際の収集日に直す…」

### 分かったこと・罠

- ops-state 先端の commit メッセージが "heart: beat 20" — **heart pod は生きてビートを
  回している**。merge 後 green 化の前提 (ビートが流れている) を間接確認できた
- 先端が進んでも手順への影響なし。publish_reminders は既存状態ファイルに触らない
- このサンドボックスに `gh` CLI は無い。PR/merge 状態は
  `git branch -r --contains <sha>` で判定した
- テスト・validate を再実測: unittest 24 本 OK、validate.py OK (劣化なし)

### 次のセッションへの一言

コード完成・変更不要の結論は不変。verify(3) は merge → heart 初回ビート (~120s) で green。
merge 後も red が続くときの切り分け順: (a) ops-state 最新 commit メッセージでビートが
回っているか確認 → (b) セッション 2/3 のリハーサル (worktree + publish_reminders +
commit, push 無し) を再実行してレンダラ/配線側を切り分け → (c) heart pod 側を疑う
(sync_main 済み repo_dir が新 main になっているか / pod 再起動が必要か)。

## worker セッション 4 (2026-08-23) — main 側進行との衝突監査 + 最新 ops-state 先端でリハーサル再通過。コード変更なし

### やったこと

レビュー指摘は無し。唯一の failing は verify(3) のまま (ブランチ未 merge を実測:
`git branch -r --contains 7335acb20` は origin/project/p-0231 のみ)。このセッションの新規
価値は **main が分岐点から進んだ (5877f715e → 31a806191, PR #564 ほか)** ことへの対応:

1. **衝突監査**: 自分の触ったファイルと main 側の変更ファイルの積集合 = ゼロ。
   `git merge-tree` の conflict 数も 0。main は分岐点以後 `ops/heart/` も
   `apps/ops-dashboard/` も触れていない (意味的なドリフト無し)
2. **ops-state がさらに進んでいた** (6b0776c32 → 667b796ef, "heart: beat 23") ので
   リハーサルを最新先端に対して実施: worktree 展開 → 実台帳 × 実時刻 (UTC now) で
   `publish_reminders()` → add -A + commit (**push 無し**) →
   `git show HEAD:briefing/reminders.txt` 通過、レンダラ CLI 出力と diff ゼロ一致
3. テスト再実測: unittest 24 本 OK、validate.py OK (0 error)

### 分かったこと・罠

- heart pod 引き続き稼働中 (beat 23)。merge 後 green 化の前提は崩れていない
- main 側の進行は本ブランチに一切影響しないことを機械確認した — 以後のセッションで
  衝突監査を繰り返す必要は薄い (merge 直前に一回やれば十分)
- Heart の構築は test_reminders_beat.py 流儀 (`HEART_DATA_DIR` tmp 指定 +
  `HEART_MODE=shadow`) なら credential 無しで可能。リハーサル手順の詳細はここが唯一の
  ノウハス: `h.state_dir` だけ差し替え、now は `datetime.now(timezone.utc)` (beat 同一条件)

### 次のセッションへの一言

結論不変: コード完成・変更不要。verify(3) だけが merge 待ち (merge → 初回ビート ~120s で
green)。レビュー指摘があればその解消が最優先。merge 後 red 継続時の切り分け順は
セッション 3 末尾の (a)(b)(c) をそのまま使う。
