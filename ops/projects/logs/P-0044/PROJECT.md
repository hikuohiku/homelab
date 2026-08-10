# P-0044 — ダッシュボードのプロジェクトビューから「済んだ失敗」のノイズを消す (3 層表示)

## 目的

人間の実指摘 (2026-08-10)「プロジェクトにゴミがいっぱい溜まってて見づらい」。仕切り直しで置き換え済みの
stalled/vetoed (P-0002 / P-0012 / P-0013 / P-0023 / P-0025) が、現役・納品と同じ密度で `heart-projects` 節に
並び続けている。実測すると 17 件中 5 件が「もう誰も何もしない終端」で、しかも `stalled` は
`PROJECT_ORDER` で **0 番 = 最上段**に来るため、画面を開いた人が最初に読むのが済んだ失敗になっている。
終端は増える一方なので、行を減らすのではなく**層を分ける**構造の変更が要る。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**
(2026-08-10、`project/p-0044` の checkout で、リポジトリルートから実行)。

- [ ] `python3 ops/dashboard/build.py && grep -q 'id="heart-projects-closed"' ops/dashboard/index.html`
  — 折り畳み層が **その id ちょうど**で存在すること。実測 rc=1 (build 自体は rc=0 で通り、
    生成された index.html に `heart-projects-closed` が 1 文字も無い)。
- [ ] `python3 ops/dashboard/build.py && python3 -c "h=open('ops/dashboard/index.html').read(); c=h.find('id=\"heart-projects-closed\"'); assert c>=0; assert h.find('P-0012')>c and h.find('P-0002')>c, '仕切り直し済みの終端が折り畳みの外にある'"`
  — 仕切り直し済みの終端 (P-0012 = stalled/spec_error、P-0002 = vetoed) が、
    **HTML 中の最初の出現位置まで含めて**折り畳みの内側に入っていること。実測 rc=1
    (AssertionError。現状 P-0012 は offset 14682 = 節の先頭付近、P-0002 は 18390)。

**2 項目目は文字列の位置しか見ていないので、DoD の下限ですらない。** とくに DoD が要求している
「stalled のうち question 系は現役層に残す」は **どちらの verify も一切見張っていない**。
今の projects.json に question 系の stalled が 1 件も無いため、`stalled` を全部畳むだけでも 2 項目は green になる。
そうしないこと。この分岐は下の「進め方 3」の単体テストで自分で担保する。

### 罠: verify 2 は P-0044 自身の `why` に引っかかる (実測済み。必ず読むこと)

`h.find('P-0012')` は **HTML 全体の最初の出現**を探す。ところが P-0044 自身の spec の `why` に
`(P-0002/P-0012/P-0013/P-0023/P-0025 等)` という文字列が入っており、これが現役層の
`<p class="pj__why">` にそのまま出る (現状 offset 18390-18424 で実測)。P-0044 は納品されるまで現役層に居るので、
**層を正しく分けただけでは verify 2 は永久に通らない。**

実測値:

- `why` は 164 文字。`P-0002` は **75 文字目 (0-based)** から始まる (`P-0012` は 82、以下 7 文字刻み)
- 現状の描画は `clip(why, 130)` (`render_projects()` 内)。130 > 75 なので素通しする

したがって **現役層の `why` の扱いを変えるのが必須**で、選べる手は実質 2 つ:

1. **`clip(why, 130)` を 72 文字前後に縮める** (推奨)。`.pj__why` は `font-size:.8rem`、主列は
   `minmax(0,1.8fr)` なので 130 字は 2〜3 行に折り返す。72 字なら 1 行強に収まり、
   「1 行の要旨」という扱いが層の密度差 (現役=厚い / 納品=薄い / 終端=1 行) と揃う。
   72 なら `stalled/vetoed` の直後で切れて `…` が付き、読み物として壊れない
2. 現役層で `why` を出さない

**どちらを採るかは worker が決めてよいが、選んだ理由を PROGRESS.md に必ず書くこと。**
「verify を通すために縮めた」だけで済ませない — レビュー役はここを必ず突く。

## 設計方針

### 前提 (initializer が実読・実測した。調べ直さなくてよい)

- **対象は `ops/dashboard/build.py` の `render_projects()` (現 470-536 行) ただ 1 箇所。**
  ここが `<section class="sec" id="heart-projects">` を丸ごと組み立てている。
  `PROJECT_STATE_META` (60) と `PROJECT_ORDER` (73) が状態語彙と並び順を持つ。
  **状態語彙の単一の情報源は `ops/heart/statefiles.py` の `PROJECT_STATES`**（build.py の
  コメントがそう宣言している）。9 状態を増やさない
- **入力は `origin/ops-state` ブランチの `projects.json`** (`load_projects()`, 100-116)。
  `doc is None` なら節ごと出さずに `""` を返す作りで、CI の ops job (`git fetch --depth=1 origin main` しかしない)
  はこの経路に入る。**この分岐を壊すと CI が赤くなる。** 逆に、手元で verify を回すには
  `origin/ops-state` が生えている必要がある。`--depth=1` clone は `--single-branch` を含むため
  静かに生えない (`ops/memory/substrate.md` の git 節)。生えていなければ
  `git fetch origin '+refs/heads/*:refs/remotes/origin/*'` を打つ
- **CI は `build.py` を一度も実行しない** (`.github/workflows/ci.yml` の ops job は validate.py と
  各 `check_*.py`、`ops/heart/tests` / `ops/runner/tests` / `ops/tests` の unittest discover のみ)。
  つまりこの変更の回帰を見張るものは、いま何も無い
- **実データ (2026-08-10 時点、17 件)**:
  - 現役 (proposed〜soaking) 2 件 — P-0039 (announced) / P-0044 (active、これ自身)
  - delivered 10 件 — P-0004 / P-0001 / P-0011 / P-0014 / P-0015 / P-0026 / P-0027 / P-0028 / P-0029 / P-0035
  - 終端の失敗 5 件 — P-0002 (vetoed) / P-0012・P-0013 (stalled, `spec_error`) / P-0023・P-0025 (stalled, `error`)
  - **question 系の stalled は現在 0 件。** だから「実データで見た目が正しい」ことは
    question 分岐の正しさの証拠にならない
- **`stalled_reason` の全語彙と、それが question かどうかの単一の情報源は
  `ops/heart/reconcile.py` の `_stall(p, actions, reason, ntype, text)` の第 4 引数**。
  実読した対応 (2026-08-10):

  | ntype | stalled_reason |
  |---|---|
  | `question` (=人間の回答待ち) | `budget_exhausted` / `quota_wait_exhausted` / `merge_timeout` / `pr_closed` / `adopt_gate_<verdict>` (reconcile.py 190 行) |
  | `incident` | `adopt_gate_unmeasurable` / `no_pr_reported` / `no_pr_to_merge` / `job_missing` / `runner_crash_loop` / `review_timeout` / `soak_failed` / runner の result state そのもの (`error` / `spec_error` 等、313 行) |
  | `review` | `review_rejected` |
  | (なし) | `human_stop` (143 行。`_stall()` を経由せず直接代入) |

  spec の文言は「`adopt_gate_*` 等の question 系」。**接頭辞 `adopt_gate_` で一括して現役に残す**のが
  spec に素直で、`adopt_gate_unmeasurable` (実体は incident) も巻き込むが、
  あれも人間の手が要る点は同じなので実害がない。この判断を PROGRESS に 1 行書く
- **`delivered` の「直近」は `merging_since` で決める。** delivered 10 件全部に付いており
  (`2026-08-08T06:28:00Z` 〜 `2026-08-10T06:10:41Z`)、id 順とは一致しない (P-0028 は P-0029 より後)。
  `created` は日付だけで粒度が足りず、`delivered_at` は存在しない。欠けている場合に備えて
  `p.get("merging_since") or ""` で降順に倒す (欠けたものが末尾に落ちる)
- **見た目の流儀** (build.py 冒頭の設計方針、および CSS のコメント):
  - 既存の折り畳みは `<details class="fold"><summary>…</summary>…</details>` の 1 種類だけ
    (732 行、`.fold` / `.fold > summary` が CSS 1048 行)。**新しい折り畳みの型を作らない**
  - 色は「誰待ちか」「正常/注意/異常」にしか使わない。識別色を増やさない
  - 数はこのファイルが数える。文章側に数字を書かない (`sec__n` は既に `len(live)` から出している)
  - 「済んだこと・クラスタの細部は畳む」— この案はその方針の適用であって、新方針の導入ではない
  - `grid-row: 1 / span 2` のような行またぎ配置は使わない (1280px で 440px の空白が空いた実測、936 行)

### 進め方

1. **層を分ける。** `render_projects()` の中で `live` を 3 つに仕分ける。上から順に:
   - **現役層** — `state` が `proposed`/`announced`/`active`/`in_review`/`merging`/`soaking`、
     **＋ `stalled` かつ `stalled_reason` が question 系のもの**。全件、今の行の形のまま
     (chip / title / meta / why / 予算バー)。並び順は `PROJECT_ORDER` を流用し、`stalled` が最上段のままでよい
     — 人間の回答待ちこそ最初に読ませたい
   - **納品層** — `delivered` を `merging_since` 降順で先頭 5 件。6 件目以降は
     `<details class="fold">` に畳み、`summary` に残りの件数を出す。見出し側には総数を出す
   - **終端層** — 残り (`vetoed` / question 系でない `stalled`) を
     **`<details class="fold" id="heart-projects-closed">`** に既定で畳む。`summary` は件数だけ
2. **層ごとに密度を変える。** 全層で同じ `.pj` を使うと、畳んでも開いた瞬間にまた雑音になる。
   納品層と終端層は 1 行 (chip + id + title、終端は `stalled_reason` を添える) に落とす。
   CSS を足すなら `.pj-list` / `.pj` の既存の罫線の流儀 (`border-top`、面で囲わない) に合わせる。
   `.pj:first-child` が太い罫線 (`var(--rule)`) を引く指定になっているので、リストが 3 本に増えると
   3 本とも太くなる。意図どおりか確認して、要るなら CSS を直す
3. **`ops/tests/test_dashboard_projects.py` を足して分岐を単体テストで固定する。**
   ops job の `python3 -m unittest discover -s ops/tests -t .` がそのまま拾う (`ops/tests/__init__.py` は既存)。
   最低限これを見張る: (a) question 系 `stalled` が現役層に出て終端層に出ない、
   (b) `spec_error` / `error` / `human_stop` の `stalled` と `vetoed` は終端層に入る、
   (c) delivered が 6 件以上あるとき先頭 5 件だけが折り畳みの外に出る、
   (d) `doc is None` で `""` が返る。**projects.json を読ませず、辞書を直接組んで `render_projects()` を呼ぶ。**
   実データに依存させると、明日 heart が状態を進めた瞬間にテストが落ちる
4. **`why` の扱いを決める** (上の「罠」節)。決めたら実際に verify 2 を回して green を実測する
5. **verify 2 項目を自分で実行して green を確認してから commit する。**
   `AUTOPILOT_GITHUB_TOKEN` が環境にあると `build.py` は `ops-dashboard` ブランチへ push する
   (1244 行)。**検証で回すときは `env -u AUTOPILOT_GITHUB_TOKEN python3 ops/dashboard/build.py` にして、
   途中経過を人間の見る画面へ配信しない。** 最後に一度だけ素で回すのは可

### 実装上の罠

- **`ops/dashboard/index.html` は `.gitignore` されている生成物** (`.gitignore:55`)。
  commit に含まれないし、含めようとしない。差分として残るのは `build.py` (と新しいテスト) だけ
- `id="heart-projects-closed"` は **`<details>` 自身に付ける。** 「節の先頭に空の
  `<span id="heart-projects-closed">` を置いて位置だけ先に作る」「DOM 順は畳まないまま CSS の
  `order` で見た目だけ下げる」は verify を騙しているだけで、DoD を満たしていない。やらない
- `PROJECT_ORDER` / `PROJECT_STATE_META` の **キーを削らない**。層に出さない状態が出てきても
  `.get(..., 9)` / `.get(..., (state, "idle"))` のフォールバックが効くようにしておく
  (statefiles.py に状態が増えたときに画面から静かに消えるのを防ぐ)
- `n_open` (531 行) は現在 `delivered/stalled/vetoed` 以外を数えている。question 系 stalled を
  現役に上げると、この定義と層の定義が食い違う。**数え方は 1 箇所にまとめ、見出しの数字を
  層の実体から出す**
- セッション終了時に HEAD は `project/p-0044` のまま。wrapper が
  `git push origin HEAD:project/p-0044` を無条件に打つ (`ops/runner/runner.py`)。別ブランチに移らない
- 一時ファイルは `mktemp`。固定パス `/tmp/…` は前セッションの残骸を拾う (実測済みの罠)

## やらないこと

- **`ops/heart/` の変更。** `stalled_reason` の語彙も `PROJECT_STATES` も heart の領分で、
  この案は**読む側だけ**を直す。reconcile.py に「question かどうか」のフラグを生やしたくなっても、
  今回はやらずに PROGRESS の「発見」に 1 行書く (1 PR 1 論点、CHARTER §4)
- **`projects.json` そのものの掃除** (済んだ終端を消す・アーカイブへ移す)。
  台帳から消すのは不可逆で、この案は「見せ方」の問題として採択されている
- **`heart-projects` 以外の節** (`人間の鍵作業` / `脈拍` / `クラスタ` / `記録` / `legacy-backlog` /
  書き置きフォーム) への手入れ。見て気になっても触らない
- **配色・タイポグラフィ・版面 (`.grid` / `.col` / `.rail`) の変更**。足すのは
  新しい層に要る最小限の CSS だけ
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` / `ops/inventory.json` の更新**、
  CHARTER・VISION・`ops/memory/` の改訂。heart の領分 (worker.md「ops/ の帳簿を触らない」)
- **`apps/ops-dashboard/` (配信側)** の変更。生成側だけで閉じる案
