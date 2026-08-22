# P-0101 — PROGRESS

## 経過

- initializer (2026-08-22): PROJECT.md を作成。受入 3 項目すべて failing を実測 (rc 1/1/1)。
  実装は未着手。
- worker #1 (2026-08-22): opencode CLI v1.18.21 を実際に壊して死因別出力を実測し、
  fixture 6 本 (`ops/tests/fixtures/engine_stderr/`)・テスト 13 件
  (`ops/tests/test_failure_patterns.py`)・FAILURE_PATTERNS へ network 1 パターン追加・
  substrate.md へ「opencode CLI の死因出力」項を追記。**verify 3 項目とも自前実行で
  green** (glob>=4 OK / unittest 13 件 OK / grep OK)。ops/tests 全 81 件・
  ops/runner/tests 全 36 件も green。

## 実測で分かったこと (v1.18.21, model opencode-go/ox-alpha-free)

1. **死因は stderr に出ない。stderr は常に空。** 失敗時は stdout に `type=error`
   JSON イベントが流れ、runner は `consume_stream_event()` が拾った
   `error.data.message` で分類する (既存配線で正しかった)。
2. 鍵が**誤っている**: `APIError` / `Invalid API key.` / statusCode 401 → 既存
   `invalid api key` パターンで auth 分類可能。
3. **鍵が無い (env 未設定)**: `UnknownError` / `Unexpected server error. Check server
   logs for details.` → auth に分類できない (spawn.py の secret 消滅時は unknown 落ち)。
4. **ネットワーク断**: 接続拒否 (proxy を塞ぐ) も DNS 失敗も同一文言
   `Cannot connect to API: Unable to connect. Is the computer able to access the url?`
   → 既存パターンでは一切マッチしないので `cannot connect to api` を追加した。
5. **HTTP 429 は UnknownError に潰れる** (ローカルモックで openai-compatible /
   anthropic 両 SDK 経路・OpenAI 形 / Anthropic 形両レスポンスで再現)。上限情報は
   完全消失し usage_limit に分類できない = **spec が恐れた「上限死が unknown に落ちる」
   経路が実在する**。鍵未設定と同一出力なので auth との区別も不可能。
6. 正常系: rc=0、stderr 空、stdout に step_start/text/step_finish。

## 発見 (scope 外。curriculum が拾うこと)

- 上限死が unknown に落ちる問題は残る (#5)。対処には CLI 側の出力形変化待ちか、
  runner が UnknownError 回を「3 連続 error」に数えない等の方針決めが必要 (CHARTER §4)。
- `opencode run` のエラー時リトライ挙動 (AI SDK の maxRetries 相当) は未調査。
  ネットワーク断イベントが isRetryable=true なのに即 rc=1 で落ちたのは観測済み。
- ローカル環境に pip/ruff が無く CI の `ruff check --select F821` を再現できなかった
  (新規コードは import 全使用を手動確認 + ast パース済み)。

## 次のセッションへ

- verify は全部 green のはず。wrapper 実測で red が出たら真っ先に疑うのは fixture
  ヘッダコメントの語彙 (「rate limit」等の英語フレーズをヘッダに足すと分類を汚染する —
  テストはヘッダを除外する設計だが表が壊れる)。fixture の expect_failure_kind は
  別行に書くこと (同一行だとパースが壊れていた実害あり)。
- **本物の zen API の 429 出力形はまだ未観測。** 上限で死んだ回が出たら result.json の
  `stderr_tail` を証拠に fixture 差し替え + 表追記 (substrate.md 同項にも記載済み)。
- 一時実測データは mktemp 作業ディレクトリに残置 (commit 対象外)。

