# P-0044 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## セッション記録

<!-- 1 セッション 1 ブロック。何をやったか / 分かったこと (実ログ・実測値は必ず) / 次への一言 を書く -->

### セッション 1 (2026-08-10) — 3 層化を実装。verify 2 項目とも green を実測

**やったこと**

- `ops/dashboard/build.py`:
  - 定数を 3 つ追加 (`TERMINAL_STATES` / `QUESTION_STALL_REASONS` + `QUESTION_STALL_PREFIX` /
    `DELIVERED_HEAD = 5`)。`PROJECT_ORDER` / `PROJECT_STATE_META` のキーは 1 つも削っていない
  - `is_question_stall(p)` / `split_projects(projects)` を新設。**層の定義を持つのはこの 1 関数だけ**で、
    見出しの数字も描画もここが返した 3 本のリストから出す (旧 `n_open` の独自集計は削除。
    question 系 stalled を現役に上げると定義が食い違うため)
  - `render_projects()` を分解: 厚い行 `_project_row()` / 1 行 `_project_slim()` / `_slim_list()`
  - 出力構造は `<p class="sub">進行中 N 件</p>` + 厚い `.pj-list` →
    `<p class="sub">納品済み N 件</p>` + 直近 5 件 + `<details class="fold">それ以前の N 件` →
    `<details class="fold" id="heart-projects-closed">終わった案 N 件（停止・拒否。もう誰も動かしません）`
  - CSS は 4 行だけ追加 (`.pj--slim` / `.pj--slim .pj__title` / `.pj__note` /
    `.pj-list--slim .pj:first-child`)。`.pj__id` に mono/.71rem を明示 (厚い行では
    `.pj__meta` から継承していたが、slim 行には `.pj__meta` が無い)
- `ops/tests/test_dashboard_projects.py` を新設 (16 テスト)。辞書を直接組んで呼ぶので実データに依存しない

**実測**

- `python3 ops/dashboard/build.py && grep -q 'id="heart-projects-closed"' …` → **rc=0**
- verify 2 (`h.find('P-0012')>c and h.find('P-0002')>c`) → **rc=0**。実測オフセット:
  closed=18363、P-0012=18554、P-0013=18768、P-0023=18982、P-0025=19204、P-0002=19426。
  **各 id の出現は HTML 全体で 1 箇所だけ**に減った (前は P-0012 が 14596/18311/22121 の 3 箇所)
- `python3 -m unittest discover -s ops/tests -t .` → **51 tests OK**、
  `ops/heart/tests` 107 OK、`ops/runner/tests` 28 OK、`ops/validate.py` 0 error / 1 warning
  (warning は backlog の todo 0 件で、この変更とは無関係)
- 生成 HTML は 43527 → 34207 bytes

**判断とその理由 (レビューはここを突く。必ず読むこと)**

1. **`why` の clip を 130 → 72 に縮めた** (PROJECT.md の「罠」節の選択肢 1)。
   理由は verify ではなく**層の密度勾配**: 納品層・終端層を 1 行に落とした以上、現役層の 1 案が
   主列を 3 行占めると「上が厚い / 下が薄い」という層の差が消える。72 字なら `.pj__why`
   (`font-size:.8rem`、主列 `minmax(0,1.8fr)`) で 1 行強に収まり、「1 行の要旨」という扱いに揃う。
   全文は spec と PROJECT.md にあるので情報は失われない。
   **74 字ではなく 72 字にしたのは意図的**: 74 は P-0044 の `why` の `P-0002` の直前 (index 75) で
   ちょうど切れる値で、単一の実データに合わせた数字になってしまう。72 は 1 データ点に
   吸着していない (切り口は `stalled/vetoe…` と語の途中になるが、`clip()` は
   `.pj__title` の 78 字でも同じことをしており、この画面の既存の流儀の範囲内)
2. **question 系 stalled の判定は `QUESTION_STALL_REASONS` の 4 語 + 接頭辞 `adopt_gate_`**。
   `adopt_gate_unmeasurable` は reconcile.py 上は ntype=incident だが、人間の手が要る点は
   question 系と同じなので接頭辞で一括して現役に残す (spec の文言「`adopt_gate_*` 等の question 系」に素直)。
   `human_stop` は「人間が止めた＝もう動かさない」なので終端層
3. **未知の state は現役層に出す。** `TERMINAL_STATES` に挙げた 3 つ以外は全部現役扱いにした。
   statefiles.py に状態が増えたとき、黙って折り畳みに消えるより見えるところで浮くほうが直せる
   (`test_unknown_state_stays_visible` で固定)
4. **`.pj:first-child` の太い罫線は最上段の 1 本だけに残した。** リストが 3 本に増えて太線が
   3 本になると層の区切りが二重になる。層の見出しは既存の `.sub` (mono/小/大文字) が持つ。
   **`.sub` は既存クラスの再利用で、新しい見出しの型は作っていない**

**罠 / 未解決**

- `ops/dashboard/prs.json` はセッション開始時点で既に dirty だった (build.py が書き戻す
  PR キャッシュ)。この案の差分ではないので **commit していない**。触らないこと
- `ops/dashboard/index.html` は `.gitignore` 済み。commit に入らないのが正しい
- 検証中は `env -u AUTOPILOT_GITHUB_TOKEN python3 ops/dashboard/build.py` を使い、
  最後に 1 度だけ素で回した (= `ops-dashboard` ブランチへ published)。次のセッションも同じにすること
- **見た目は実機ブラウザで一度も見ていない** (HTML 文字列の検査のみ)。
  レビューで崩れが出るなら `.pj--slim` の `flex-wrap` と `.pj__title { min-width:12rem }` の
  組み合わせが最初の容疑者 (幅が狭いと `.pj__note` が次行に落ちる)

**次のセッションへ**: 受入 2 項目・全テストとも green。追加実装は無い想定なので、
レビュー指摘が来たらそれだけを直す。仕様を先回りして広げないこと。

## 発見 (スコープ外。curriculum が後で拾う)

<!-- 仕様外で気づいたことを 1 行ずつ。ここに書くだけで、手は出さない -->

- `stalled_reason` が「人間待ちか否か」を持たないので、読む側 (build.py) が語彙を再実装している。
  本来は `ops/heart/reconcile.py` の `_stall()` が ntype を台帳に書けば二重管理が消える (今回は heart の領分なので触らない)
- 台帳に `delivered_at` が無く、納品の時刻は `merging_since` で代用している (取り込み開始 ≠ 納品)
- `ops/dashboard/build.py` は CI が一度も実行しない。今回 `ops/tests/` に単体テストを足したが、
  `build.py` を端から端まで走らせる smoke は依然として無い
- P-0012/P-0013 と P-0023/P-0025 は題が完全に同じ終端が 2 件ずつ並ぶ (仕切り直しの複製)。
  畳んだので実害は減ったが、台帳側の重複そのものは残っている
