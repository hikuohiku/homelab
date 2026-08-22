# P-0101 — opencode 移行当日の FAILURE_PATTERNS を実測フィクスチャで再校正する

## 目的

`ops/models.json` は 2026-08-22 に全役を claude → opencode-go へ切り替えたが、
`ops/runner/runner.py` の FAILURE_PATTERNS は claude CLI の既知の出力形を根拠にした**実測ゼロの候補**
のままで、opencode CLI の stderr 形は 1 文字も観測されていない (substrate.md に opencode の記述 0 件)。
移行直後に上限死が `failure_kind=usage_limit` ではなく unknown に落ちれば、3 連続 error 判定から
stalled 化し、2026-08-08 の「26 セッション空費」が opencode の出力形で再演される。
VISION 最優先「ループが止まらないこと」への直撃であり、移行の締めとして今しかできない仕事。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing** (2026-08-22、`project/p-0101` の
checkout で、リポジトリルートから実行。rc は順に 1/1/1)。

- [ ] `python3 -c "import glob; assert len(glob.glob('ops/tests/fixtures/engine_stderr/*.txt'))>=4"`
  — opencode CLI の死因別 stderr **実測**フィクスチャが 4 本以上あること。現在ディレクトリごと無く AssertionError。
- [ ] `python3 -m unittest ops.tests.test_failure_patterns`
  — fixture を読んで分類を検証する unittest が存在して green であること。現在 ModuleNotFoundError。
- [ ] `grep -q 'opencode' ops/memory/substrate.md`
  — 実測結果が意味記憶に記録済みであること。現在 0 件。

3 項目ともクラスタ・Discord・ネットワークに依存せず、ローカル checkout だけで判定できる。

## 設計方針

前提 (調べて分かったこと):

- runner は既に二重エンジン対応済み。`build_session_cmd()` (runner.py:196) が provider/model 形式なら
  `opencode run --format json` を選び、`consume_stream_event()` (runner.py:215) は opencode の
  `type=error` から `error.data.message` を拾って分類入力に混ぜる (2026-08-22 v1.18.21 実測)。
  **worker が触るのは FAILURE_PATTERNS (runner.py:48) とそのテスト・記録だけで、起動・収集経路は触らない。**
- 判定順 `usage_limit > auth > network > unknown` は仕様 (runner.py:46)。opencode 版もこの順序を保つ。
  分類 (`classify_session_failure`) と reset 時刻抽出 (`parse_usage_limit_reset`) は混ぜない (同 114)。
- CI は既に `python3 -m unittest discover -s ops/tests -t .` を回している (.github/workflows/ci.yml:58)。
  新テスト (`ops/tests/test_failure_patterns.py`) の配線作業は要らない。
- fixture には秘密が混ざりうる。`SECRET_ENV_KEYS` / `SECRET_PATTERNS` (runner.py:80-92) の流儀でマスクする。
  既存テストの文体は `ops/tests/test_backup_coverage.py` のように冒頭 docstring に「なぜ要るか」を書く流儀。

どう作るか:

1. **実測が本体。** opencode CLI を実際に壊して死因別 stderr を最低 4 本収集する
   (認証失敗 / レート制限 / ネットワーク断 / 正常系)。正常系は「成功出力を failure 誤分類しない」否対照。
   収集物は `ops/tests/fixtures/engine_stderr/<死因>.txt` に置く。観測できなかった死因は
   「未観測」と明記し、推測でパターンを埋めて偽りの完全性を作らない (spec DoD)。
2. 実測文言だけを根拠に FAILURE_PATTERNS を修正し、fixture ファイルを読んで `classify_session_failure()`
   に流す unittest で各死因が正しい failure_kind に分類されることを証明する。
   `parse_usage_limit_reset()` は現状 claude 形式専用 — opencode の reset 時刻形式を実測できた場合のみ対処する。
3. substrate.md の「claude セッション / 利用上限」節に opencode 版を実測値で追記する。
   memory の書き手は consolidation 原則だが、P-0015/P-0026 に次ぐ **spec の DoD が名指しで要求する例外**
   なのでその旨を 1 行添える。

## やらないこと

- **initializer が実装に入ること。** initializer の仕事は PROJECT.md / PROGRESS.md の作成まで。
  fixture 収集・表修正・テスト・substrate 追記は worker セッションが遂行する。
- **`auth` / `network` 死因に基づく制御変更** (即時打ち切り・バックオフ等)。実測が揃ってからの別論点
  (1 PR 1 論点、CHARTER §4)。
- **rules.json / models.json / spawn.py / runner の起動・stderr 収集経路の変更。**
  単一情報源と P-0026 が作った器は触らない。
- **claude CLI 用パターンの削除。** models.json は PR 経由で戻せるので、ロールバック経路を残す。
