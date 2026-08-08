# P-0023 — 実績台帳が空っぽ: 納品・失敗・実消費を archive.jsonl と projects.json に本当に書く

## 目的

`ops/projects/README.md` は「結果は heart が `result` フィールド付きの行として追記する」と宣言しているが、
`archive.jsonl` 14 行のどこにも `result` キーが無い。delivered した P-0001/P-0004/P-0011/P-0014 も
spec_error で死んだ P-0012/P-0013 も、恒久記録の上では「提案されただけ」に見える。
curriculum が読む唯一の全記録がこれなので、何が成功し何が失敗したかを学習できず、事実
P-0012→P-0013→P-0014 の三重提案が起きた。同じ穴が予算にもあり、`projects.json` の
`budget.used_tokens` は全プロジェクトで 0 のまま（runner は内部で数えているが状態に書き戻していない）。
VISION の「自分の失敗を仕組みに変える」の教師信号そのものを回復させる。

## 受入チェックリスト

すべて 2026-08-08 に initializer が実測し、**4 項目とも failing** であることを確認済み。

- [ ] `python3 -c "import json,sys; rows=[json.loads(l) for l in open('ops/projects/archive.jsonl') if l.strip()]; done={r['id'] for r in rows if 'result' in r}; sys.exit(0 if {'P-0001','P-0004','P-0011','P-0012','P-0013','P-0014'} <= done else 1)"`
  — archive.jsonl に既存 6 プロジェクトの `result` 行が backfill されている（現状 rc=1: `result` キーを持つ行が 1 行も無い）
- [ ] `grep -q 'archive_result' ops/heart/reconcile.py`
  — 終端到達時に result 行を書く判断が、状態機械（純関数）の側に action として存在する（現状 rc=1）
- [ ] `grep -q 'def check_archive_results' ops/validate.py`
  — 「終端なのに result 行が無い」を CI が機械検査する（現状 rc=1）
- [ ] `python3 -m unittest ops.heart.tests.test_archive_result`
  — 上記の遷移を単体テストが固定している（現状 rc=1: モジュールが存在せず ImportError）

## 設計方針

調べて分かった前提と、それに沿った作り方:

- **archive.jsonl は main にあり、main への直 push は ruleset が拒否する**（`ops/memory/substrate.md`）。
  `ops/heart/gh.py` に PR 作成は無い。既存の同型パターンは `ops/runner/runner.py` の
  `fix_to_archive()`（`git checkout -B <branch> origin/main` → append → push → `POST /repos/.../pulls`）。
  heart も同じ形で「result 行を追記する PR」を作り、CI green になったら既存の `merge_pr` action で merge する。
- **判断は `reconcile.decide()` に `archive_result` action として出し、I/O は `heart.execute()` が行う**
  （reconcile.py は純関数のみ、という冒頭の規約）。**冪等性が要**: 終端エントリは projects.json に残り続けるので、
  「main の archive に既に result 行がある id」（`facts` で収集）と「発行済み PR の記録」（`p["archive_pr"]` 等）の
  両方で再発行を止める。止めないと 120 秒ごとに PR が生えて main が埋まる。
- **result 行に `adopted: true` を付けない。** `facts.load_adopted_specs()` と `runner.load_spec()` は
  `adopted` 行だけを spec として拾うので、これで誤読は起きない。`validate.py` の追記専用（先頭一致）検査も
  append なので壊れない。ただし **`ops/dashboard/build.py` の `load_project_specs()` は `adopted` を見ずに
  同 id の最後の行を採る**ので、result 行を足すとダッシュボードから why/dod が消える。ここは同時に直す（退行防止の最小修正）。
- **`check_archive_results` は終端プロジェクトの一覧を `origin/ops-state:projects.json` から読む。**
  CI の `ops state validate` job は現状 `origin/main` しか fetch していないので、同 job に ops-state の fetch を足し、
  取れない環境ではスキップする（既存の origin/main スキップと同じ規約）。result 行が `adopted: true` を
  持っていないことも併せて検査する。
- **backfill の対象は「その時点で終端の全プロジェクト」**: spec 名指しの
  P-0001/P-0002/P-0004/P-0011/P-0012/P-0013/P-0014 に **P-0015（delivered）も足す**。
  足さないと DoD(4) の新検査が自分で CI を落とす。
  データ源は `git show origin/ops-state:projects.json`（state / prs / spawn_count / stalled_reason）と
  `origin/ops-state:audit.jsonl`（59 行。deliver / merge_pr / notify の時刻）。
  **audit.jsonl にトークン数は無い**ので、`used_tokens` / `used_cost_usd` は 0 と書かず `null`（不明）にする。
  0 は「使っていない」という嘘になり、この spec が潰そうとしている穴そのものである。
- **budget 書き戻しは単一書き手を壊さない形で行う。** `projects.json` の書き手は heart だけ
  （`ops/heart/statefiles.py` 冒頭）。runner は session ごとに PVC へ消費量を書き（`result.json` は
  consume（退避）の意味論を持つので**別ファイル**にする）、heart の `facts` が回収して `reconcile` が
  `p["budget"]["used_tokens"]` に反映する。`validate_projects()` は `used_tokens` が int であることを
  要求するので型を守ること。
- テストは `ops/heart/tests/test_reconcile.py` の遷移表スタイルに合わせ、`ops/heart/tests/test_archive_result.py` に置く。

## やらないこと

1 PR 1 論点（CHARTER の流儀）。以下はこのプロジェクトの外:

- ダッシュボードに実績（result）を見せる UI の作り込み。`build.py` の変更は
  「result 行で spec が上書きされる退行を防ぐ」最小限に留める
- soft_cap 判定・breaker・コスト計算のロジック変更。`used_tokens` が実値で伸びるようにするだけで、
  判定式そのものは触らない
- archive.jsonl のスキーマ全面改訂、および**過去行の書き換え・削除**（追記のみは絶対。
  backfill も新しい行の追記として行う）
- 既存の journal / memory の再編、consolidation の配線
- 終端プロジェクトを projects.json から間引く掃除（`reconcile.decide()` のコメントが将来課題として言及しているが別件）
