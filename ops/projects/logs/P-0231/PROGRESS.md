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
