# P-0001 — ダッシュボードに heart-and-projects のプロジェクトビューを追加する

## 目的

人間がプロジェクトの現在地（予告中／実行中／レビュー中／納品済み・予算消費・拒否権期限）を
一目で見る場所が無い。Discord は push 型の断片で、pull 型の全体像はダッシュボードの仕事。
旧体制への最大の不満「ダッシュボードが更新されない・フローを映さない」への最初の回答でもある。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**（2026-08-08、`project/p-0001` の
checkout で実行。`python3 ops/dashboard/build.py` 自体は exit=0 で通るが、続く `grep` が
いずれも exit=1）。

- [ ] `python3 ops/dashboard/build.py && grep -q 'id="heart-projects"' ops/dashboard/index.html`
  — build.py がエラー無く走り切ったうえで、生成された HTML に `id="heart-projects"` を持つ節が
    存在すること。現在 `id=` 属性を持つ要素は `q__row` / `sent` / `sentid` / `acount` / `abody` の
    5 種だけで、既存の `<section class="sec">` には id が無い。新しい節に明示的に付ける必要がある
- [ ] `python3 ops/dashboard/build.py && grep -q 'P-0001' ops/dashboard/index.html`
  — その節に実際のプロジェクト行（少なくとも自分自身 P-0001）が描画されていること。
    節の枠だけ出して中身が空、を通さないための項目

**この 2 項目は `origin/ops-state` が取れる環境でのみ green になる。** runner Job の
checkout（＝この initializer と同じ環境）では `git show origin/ops-state:projects.json` が
実際に成功することを実測済みで、そこには P-0001 が `state: "active"` で載っている。
一方 CI（`ops` job）は `actions/checkout@v7` ＋ `git fetch --depth=1 origin main` だけで
`origin/ops-state` を持たない。そこでは節を出さずに exit 0 で終わるのが正しい挙動であり、
CI は build.py の exit code しか見ていない（`.github/workflows/ci.yml` の
"dashboard build.py runs without error"）。verify は runner 側で走らせて判定する。

## 設計方針

- **projects.json の読み方は `load_health()`（build.py:82-93）と同型にする。**
  `("origin/ops-state", "ops-state")` の順に `git show <ref>:projects.json` を試し、
  すべて失敗したら `None` を返す。例外は握り潰す（`subprocess` の失敗も JSON の破損も同じ扱い）。
  `None` のときは節そのものを組み立てず、空文字を TEMPLATE に流す。これで CI は 0 exit のまま。
- **実測した projects.json のスキーマ**（2026-08-08 時点、4 件）: トップレベルは
  `version` / `projects` / `chores` / `last_curriculum_at`。各プロジェクトは
  `id` `title` `state` `branch` `irreversible` `capabilities` `touches_apps` `verify`
  `confidence` `budget{used_tokens, soft_cap}` `created` `veto_deadline` を必ず持ち、
  進行に応じて `job` `spawn_count` `prs[]` `drift_count` `review_requested_at`
  `merging_since` `review_retries` が生える。**後から生える側は `.get()` で読むこと。**
- **状態の語彙は `ops/heart/statefiles.py` の `PROJECT_STATES` が単一の情報源**
  （`proposed` / `announced` / `active` / `in_review` / `merging` / `soaking` / `delivered` /
  `stalled` / `vetoed`、うち `delivered` / `stalled` / `vetoed` が `TERMINAL_STATES`）。
  build.py 側で 9 個を日本語ラベルと tone に写す辞書を持つ（`STATUS_META` と同じ流儀）。
  **色は「誰待ちか」「正常/注意/異常」にしか使わない**（build.py 冒頭の設計方針と CSS 冒頭コメント）。
  識別色を新設しない。tone は既存の `ok` / `warn` / `crit` / `sig` / `idle` から選ぶ。
- **archive.jsonl は spec 側（why / cell / dod）の供給元。** 追記専用で同じ id の行が複数ありうる
  （`ops/projects/README.md`: runner は同 id の**最後の行**を読む）。同じ規則で畳む。
  現在 11 行・11 id、うち `adopted: true` は 4 件。**projects.json に載っていない案（棄却案）は
  この節に出さない。** ここは「いま動いているもの」の画面であり、立案の全記録ではない。
- **予算消費は `meter()`（build.py:319）を使う。** `budget.used_tokens` / `budget.soft_cap` で、
  `soft_cap` が 0/欠損のときにゼロ除算しないことは meter() 側が既に面倒を見ている
  （実測値は現在すべて `used_tokens: 0`。0% のバーが出ることを前提に確認する）。
- **veto 期限は `until_time()`（build.py:239）。** 未来なら「〜後」、過ぎていれば「到来済み」を返す。
  `rel_time()` を使うと未来の時刻が「この後」に潰れて期限として読めない。窓は
  `ops/rules.json` の `veto.window_hours`（現行 24）だが、実際の期限は projects.json 側の
  `veto_deadline` に確定値があるのでそちらを表示する。
- **PR リンクは `prs: [406]` のような番号配列**（`html_url` は入っていない）。
  `https://github.com/{REPO}/pull/{n}` を組み立てる。`REPO` は build.py に既にある定数。
- **置き場所は主列（`.grid` の左、「順番待ち」の上）を推す。** プロジェクトは backlog タスクより
  粒度が大きく、「いま何が起きているか」の主役だから。`.rail` に置くと計器類に埋もれる。
  CSS の版面コメント（build.py:896-901）にあるとおり **`grid-row: 1 / span 2` のような
  行またぎ配置は使わない**（1280px で右側に 440px の空白が空いた実測がある）。
  既存の `<section class="sec">` ＋ `<div class="sec__h"><h2>…</h2><span class="sec__n">…</span></div>`
  の型に合わせ、section 要素に `id="heart-projects"` を付ける。
- **TEMPLATE への配線の罠**: `TEMPLATE` は `str.format` で流し込んでいるため、CSS の `{` `}` は
  すべて `{{` `}}` に倍化されている。新しい CSS を足すときは倍化を忘れない。プレースホルダを
  1 つ増やしたら `build()` の `TEMPLATE.format(...)` 呼び出し（build.py:754-767）にも引数を足す
  — 片方だけだと `KeyError` で CI が落ちる。
- **エスケープ**: 出力は必ず `E`（`html.escape`）を通す。タイトルは `clip()` で切る
  （切ったら `…` が付く）。

## やらないこと

- **projects.json を書き換えること。** 書き手は heart だけ（`ops/heart/statefiles.py` 冒頭の
  単一書き手の原則）。この節は読むだけ。
- **`ops/dashboard/index.html` を commit すること。** `.gitignore:55` で除外された生成物であり、
  CI も「エラー無く走る」ことだけを見ている（T-0035）。
- **`chores` / `last_curriculum_at` の描画。** 今回の DoD は projects のみ。器の別の面であり、
  出したくなったら別プロジェクトとして立てる（1 PR 1 論点、CHARTER §4）。
- **棄却された案（`adopted: false`、archive.jsonl に 7 件）の一覧化。** 「立案の全記録を見る」画面は
  別の論点。
- **backlog（`ops/backlog.json`）側の「順番待ち」の作り直し・統合。** プロジェクトとタスクは
  別の粒度で、片方の追加を口実にもう片方を触らない。
- **Discord 通知・`ops/heart/notify.py` への変更。** ここは pull 型の画面を足すだけで、
  push 型には触れない。
- **CI に `origin/ops-state` を fetch させる変更。** CI で節が出ないのは仕様
  （DoD が「取れない環境では節を出さず正常終了」と明記している）。
