# P-0118 — 口は開いたが号令は通ったことがない — Telegram 受信からの停止/再開キーワード判定を、実輸送と fixture 判定の両輪で疎通させる

## 目的

OpenClaw gateway (P-0090) と決定論パススルー (P-0107) で人間の声が feedback ブランチへ流れ込む
経路は今日出来たばかりだが、CHARTER が拒否権の中核に置く「止めて」「再開」キーワードが
telegram 由来の note に対して正しく効くことは一度も確かめられていない。拒否権は人間が持つ唯一の
即時手段であり、口が新しくなった日に疎通しないことは、緊急時に初めて気づく型の欠陥。
veto 疎通実績 (2026-08-07, issue 経由) はあるが transport が違い、telegram 経路は未証明。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0118` の checkout、リポジトリルートから実行)。

- [ ] `python3 -m unittest ops.tests.test_telegram_veto`
  — telegram 由来の note ({source: "telegram", body}) 4 系統の分類を fixture で固定するテストが
    存在し green であること。とくに stop_keywords が telegram source でも参照されることを証明する。
    実測 FAILED (errors=1、`ops/tests/test_telegram_veto.py` 自体が未作成による import error)。
- [ ] `python3 ops/drills/telegram_veto_drill.py --check`
  — dry-run (--input でファイルを与えるモード) を含むドリルが存在し、期待分類との突合に成功すること。
    実際の全停止 (stop_engaged) を踏まずに stop 判定を証明する側輪。
    実測 rc=1 (`ops/drills/` ディレクトリごと存在しない)。
- [ ] `test -s ops/projects/logs/P-0118/evidence.json`
  — evidence.json が空でないこと。feedback note の URL (実輸送の疎通証跡) と dry-run 判定出力を含む。
    実測 rc=1 (ファイル未作成)。

**verify は DoD の下限であって DoD そのものではない。** spec の本文どおり、(2) の実輸送の疎通 —
OpenClaw 稼働後に実メッセージが 1 通も届いていないなら question 型通知で人間に疎通用 1 行の送信を
依頼し、feedback ブランチに source:telegram の note として現れるところまで実測する — が本体。
その証跡は evidence.json と PROGRESS.md に残す。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読した。調べ直さなくてよい)

- **triage.classify() は source 非依存**: `ops/heart/triage.py:40` は body 文字列と rules のみを受け、
  source を見ない。つまり telegram note が inbox 形式で保存されていれば停止経路は理論上乗る —
  本プロジェクトはそれを**テストとドリルで証明**するのが左輪。
- **source を渡している側は facts 側**: `ops/heart/facts.py` の `collect_feedback()` → `handle(body, source)`
  (:150) が issue コメントには `issue-comment <id>`、inbox JSON note には note の `source`
  フィールドを渡す。stop/veto 判定は task-request より先に行われる (P-0090 の絶対条件、コメント維持)。
  fixture テストは classify() 単体ではなく **collect_feedback レベル (telegram note → stop_all 等)
  で固定するのが spec の意図** (「telegram source でも参照されること」の証明になる)。
- **既存テストとの差分**: `ops/heart/tests/test_triage.py` はキーワード判定自体は固めているが、
  telegram 由来 note を通すテストはない。`ops/tests/test_openclaw_bridge.py` は保存形式のみ。
  「telegram note → 分類」の中間が誰にも固定されていない = 今回埋める穴。
- **キーワードは rules.json 単一情報源**: `veto.stop_keywords` = ["止めて","止まって","やめて","中止",
  "stop","abort","veto"]、`resume_keywords` = ["再開","resume"]。fixture はこの値を読んで使う
  (ハードコードしない)。test_triage.py と同じ読み込みパターン。
- **dry-run の材料**: bridge.py の純関数群 (`build_note` / `select_events`) と
  `ops/tests/fixtures/` 配下の fixture 慣習がある。ドリルは triage.classify の純関数呼び出しだけで
  停止状態を変えない (副作用ゼロで証明するのが dry-run の意味)。
- **question 型通知の器は既存**: `ops/heart/notify.py` の即時送信型に "question" 含む
  (IMMEDIATE_TYPES :25)。人間への送信依頼はこれを使う (daily budget 内)。
- **実輸送の現状確認の仕方**: ops-feedback ブランチの `ops/feedback/inbox/` に
  `source: "telegram"` の note が既にあるかを最初に見る (P-0107 完了後、bridge が稼働中のはず)。
  あれば新規依頼は不要で、それを実輸送の証跠に使ってよい。

### 方針

1. **fixture テスト** (`ops/tests/test_telegram_veto.py`): {source:"telegram"} 付き note 4 系統 —
   「止めて」→ stop_all、「再開」→ resume_all、kind: task-request の実装依頼 → task_requests、
   雑談 → review_needed (noise) — を collect_feedback 経由で固定する。
   unittest (pytest は Job イメージに無い)、リポジトリルートから実行する慣習に従う。
2. **dry-run ドリル** (`ops/drills/telegram_veto_drill.py`): `--input <file>` で本文 1 行 1 メッセージの
   ファイルを受け取り各行の分類を出力、`--check` で期待分類との突合をする。グローバル停止を
   実地に踏ませない — 判定は純関数呼び出しのみで、heart への書き込み等の副作用は持たない。
   `--check` 単独実行は同梱の既定ケースで自己検証する (受入コマンドが引数無しで通る形)。
3. **実輸送の疎通**: inbox に telegram note がまだ 1 通も無ければ、notify の question 型で
   人間に疎通用 1 行の送信を依頼する (「何か 1 行 Telegram から送ってほしい」程度の具体文)。
   feedback ブランチに source:telegram の note が現れたら、その URL を evidence.json に残す。
   待ちの間に fixture とドリルを完成させておく (P-0107 と同じ進め方)。
4. **evidence.json**: feedback note の URL と dry-run (--check) の判定出力を残す。
   形式は自由だが、verify コマンド 3 本それぞれの根拠が 1 目で辿れるようにする。
5. **docs 追記**: 人間向けの 1 節「Telegram での止め方・再開の仕方と、届かなかったときの見分け方」
   を docs/ に足す (配置は worker が判断。apps/openclaw まわりの文書と読者が同じ場所が望ましい)。
   「届かなかったとき」の見分け方には、dashboard / feedback ブランチのどこを見ればよいかを書く。

## やらないこと

- **実際の全停止 (stop_engaged) の発火** — spec 明記。「止めて」の実送信は行わず、fixture と
  dry-run だけで stop 判定を証明する。
- **triage / rules.json のキーワード・判定ロジック変更** — 既存の決定論パスはそのまま使い、
  通ることを証明するだけ。キーワード追加も本プロジェクトの論点ではない。
- **bridge / gateway 側の変更** — P-0107 / P-0090 の領域。受信→inbox 保存は完成済みを前提にする。
- **LLM 分類の導入** — triage 冒頭の設計判断 (決定論で拾う) を守る。
- **memory limits 等の縛る変更** — 該当なしのはず。触るなら substrate の規則に従う。
