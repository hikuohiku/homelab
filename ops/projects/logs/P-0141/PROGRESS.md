# P-0141 — PROGRESS

## セッション記録

### セッション 1 (2026-08-23)

**やったこと: DoD (1)〜(4) をすべて実装し、verify 3 項目を自分で回して全 green 実測。**

- `ops/runner/runner.py`:
  - プローブ 3 関数を新設。`probe_endpoint()` (model 文字列の provider 部 → URL 導出)、
    `probe_failure_kind()` (401→auth / 429→usage_limit / その他→None)、
    `probe_inference_api()` (urllib POST 1 リクエスト・リトライ無し・URLError/OSError→network)。
    HTTP 層は `urlopen` 引数で注入可能 (テストは network フリー)
  - `build_failure_info()` を Session.run から抽出し、分類 unknown & outcome=="error" の
    死だけプローブを打って failure_kind を差し替える。**プローブが不確定のときだけ
    unknown を維持**。`probe_status` / `probe_http_status` はプローブを打った回だけ
    result.json に載る (キー無し = 未実施)。`failure_fields()` 経由で全 write_result 経路に乗る
  - `parse_usage_limit_reset()` に opencode 形 best-effort を追加:
    reset/retry 語から 24 文字以内の ISO 8601 or epoch。claude 形 (`limit reached | <epoch>`) は最優先で無変更
  - mode_worker ループに `consecutive_unknown` 別カウンタ。閾値は `rules.runner.unknown_error_max_rounds`
- `ops/rules.json`: `runner.unknown_error_max_rounds: 3` を追加 (validate.py のスキーマ検査も通過を実測)
- `ops/tests/test_unknown_death_probe.py` 新設 (27 テスト)。substrate 実測文言 4 種
  (UnknownError / Cannot connect / Invalid API key / 429) を fixture 経由で網羅
- 既存テスト 213 (ops/tests) + 36 (ops/runner/tests) 全 green 実測

**設計決定 (次のセッションはここを疑う前に読むこと):**

1. **endpoint の導出元**: `PROVIDER_PROBE_ENDPOINTS = {"opencode-go": "https://opencode.ai/zen/go/v1/chat/completions"}`。
   出典は fixture `network_refused.txt` 等の `error.data.metadata.url` 実測値で、テストが fixture との一致まで見る。
   知らない provider・claude 形 model は None → プローブせず unknown 維持 (捏造しない)
2. **incident 型通知は heart 側を触らずに実現**: 新しい result state は作らない。
   state `"error"` で書くと heart reconcile の既存 tuple (`spec_error`/`error`/`stalled_inactive`)
   が `_stall(..., "incident")` してくれる。「stalled ではなく incident」の実体は
   「メッセージにプローブ不確定の事実と『上限か実装詰まりか不明』を明示して incident として届ける」こと。
   heart 改修は spec の やらないこと
3. **プローブは outcome=="error" の死のみ**: session_timeout / inactive_killed はエンジンの
   報告した死ではないので数えない (数えると待機や stalled 判定を誤らせる)
4. **test_quota_flow.py の `test_real_errors_still_stall_after_three` を unknown→auth に変更**。
   旧テストは「unknown = 汎用の実質エラー」という旧契約の固定だった。P-0141 がまさにそれを変えるので
   仕様に沿った改修であり、退行ではない

**分かったこと / 罠:**

- `runner_blob()` (test_failure_patterns 側) は `error.data.message` しか分類入力に載せない。
  だから blob に "UnknownError" という**名前**は出ない ("Unexpected server error." の本文のみ)。
  名前を見たいときは生イベント (`stdout_lines[0]`) を見ること。初手でこれで 1 回ハマった
- FakeRunner (test_quota_flow) の rules 辞書は本物の rules.json を模写する必要がある。
  mode_worker が新キーを直接参照するので、FakeRunner 側に足し忘れると KeyError になる (足した)
- verify_seq の形は「ラウンドごとの結果リスト」のリスト ([FAIL] や [FAIL, PASS])。
  結果 dict のリストを直接渡すと文字列インデックスエラーになる
- プローブ検出の usage_limit 死は実消費ゼロ扱いのまま (build_failure_info が分類差し替えを
  先に済ませ、トークン概算の除外条件 `!= "usage_limit"` に自然合流する)。待機中に予算が溶けない

**次のセッションへの一言:**

verify は 3/3 green のはず (wrapper 実測が正)。もしレビューで差し戻されたら、まず
`python3 -m unittest discover -s ops/tests -t .` と `-s ops/runner/tests -t .` を回して
現在地を確認すること。未解決の仮説は 2 つ: (a) 本物の zen API が空 messages [] の POST に
400 を返す可能性 — その場合プローブは不確定 (None) になり unknown 維持という正しい挙動だが、
上限死の復元力は落ちる。観測されたら probe リクエストボディの再考を curriculum に投げること。
(b) initializer ループは最初の非 quota 死で即 error 書き込みする既存挙動のまま (スコープ外)。
unknown 連続の別カウンタは worker ループのみに効く。

### 発見 (スコープ外・curriculum の原料)

- heart 側で将来 unknown-stall を独立の state (例 `stalled_unknown`) にしたければ
  reconcile.py L356 の tuple に足すだけでよい。今回は やらないこと に従い触らなかった
