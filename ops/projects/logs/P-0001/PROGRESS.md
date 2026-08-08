# P-0001 — 進捗

<!--
worker が毎セッション追記する。次のセッションのあなたはこれと PROJECT.md と git log しか読まない。
何をやったか / 分かったこと / 未解決の罠 / 次への一言 を残すこと。
-->

## セッション記録

### 2026-08-08 / セッション 1（commit ea6e4b2）

**やったこと**: 受入 2 項目とも実装して green にした。`ops/dashboard/build.py` だけを触っている。

- `load_projects()` — `load_health()` と同型。`("origin/ops-state", "ops-state")` の順に
  `git show <ref>:projects.json` を試し、全部失敗したら `None`
- `load_project_specs()` — `archive.jsonl` を id → 立案（why / cell）に畳む。同 id は最後の行が勝つ
- `PROJECT_STATE_META`（9 状態 → 日本語ラベル + 既存 tone）と `PROJECT_ORDER`（人間が見るべき順:
  stalled → announced → in_review → merging → active → soaking → proposed → delivered → vetoed）
- `render_projects()` — `None` なら空文字を返して節ごと出さない。行は
  state チップ / タイトル / `P-xxxx` / cell / 拒否権の期限 / PR リンク / 予算バー
- TEMPLATE に `{projects}` を追加し、`build()` の `TEMPLATE.format(...)` にも引数を足した（片方だけだと KeyError）
- CSS は `.col` と `.pj*` を追加。`{` `}` の倍化は済み

**実測（自分で回した結果）**:

- `python3 ops/dashboard/build.py && grep -q 'id="heart-projects"' …` → **exit 0**
- `python3 ops/dashboard/build.py && grep -q 'P-0001' …` → **exit 0**
- CI 相当の環境（`mktemp -d` に `git clone --single-branch` して `origin/ops-state` を持たせず、
  `AUTOPILOT_GITHUB_TOKEN` も外す）→ **exit 0・節は出ない**。DoD の「取れない環境では節を出さず正常終了」を満たす
- 生成 HTML をパースしてタグの入れ子が閉じていることも確認済み
- 欠損データ（`budget` も `veto_deadline` も `title` も無い / 未知の state）で `render_projects()` を
  直接叩いても落ちない。`soft_cap: 0` のゼロ除算は `meter()` が面倒を見ている（PROJECT.md のとおり）

**分かったこと / 罠**:

- **`.grid` の直下に節を増やしてはいけない。** `.grid` は広い画面（≥940px）で
  `grid-template-columns` 2 列、子は `section.q-sec` と `div.side` の 2 個ちょうど。3 個目を足すと
  3 セル目に折り返して版面が崩れる。主列を `<div class="col">` でまとめて子を 2 個に保った。
  狭い画面では `.grid` が flex column、`.side { display:contents }` ＋ `.grid .note { order:-1 }` で
  書き置きを先頭に引き上げている。`.col` を挟んでも order は flex の子の間で効くのでこの挙動は壊れない
- **`chip--idle` という CSS クラスは存在しない**（`.chip` のベースがそのまま idle 色）。
  `render_archive()` も同じ書き方をしているので流儀としては正しい。増やす必要は無い
- 実測データでは全プロジェクトが `used_tokens: 0`。予算バーは幅 0% で描かれる（見た目は空のトラック）。
  `meter()` の既定 tone は 0% でも `ok` なので緑の 0 幅、実質見えない。想定どおり
- `veto_deadline` は終端状態（delivered / stalled / vetoed）では出さないようにした。過ぎた期限は雑音になる

**次のセッションへの一言**: 受入 2 項目は実測で green。あとはレビュー指摘が来たらそれを潰すだけ。
新しい機能（chores、棄却案の一覧、backlog との統合）はスコープ外なので足さないこと。

## 発見（仕様外。後で curriculum が拾う）

- **`ops/dashboard/prs.json` がセッション開始時点で既に dirty だった**（initializer の `build.py` 実行が
  PR キャッシュを更新したもの）。今回の論点と無関係なので commit していない。`build.py` を回すたびに
  作業ツリーが汚れるので、キャッシュを追跡対象から外すか、書き戻し先を変えるかを検討する余地がある
- **`P-0011` は `state: "announced"` のまま `veto_deadline` が既に過去**（画面上「拒否権の期限 到来済み」と出る）。
  拒否権の窓が閉じたのに次の状態へ進んでいない ＝ heart 側が進めていない可能性。
  この節はそれが見えるようになった、という意味では期待どおりだが、heart 側の挙動として確認の価値がある
- `build.py` はローカル実行でも `AUTOPILOT_GITHUB_TOKEN` があると `ops-dashboard` ブランチへ push する。
  verify を回すたびに実際に公開される。今回は生成物が正しいので問題無いが、壊れた HTML を作った状態で
  verify を回すと壊れたまま公開される。dry-run の口が無いのは器の弱点
