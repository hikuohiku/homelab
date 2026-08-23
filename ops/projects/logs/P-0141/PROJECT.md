# P-0141 — opencode 移行が残した最後の穴 — unknown 死の直後に API プローブを打ち、「上限なのに stalled 化」経路を塞ぐ (P-0123 改良版)

## 目的

opencode は HTTP 429 を `UnknownError` に潰すため usage_limit に分類できず、
「上限死が unknown に落ちる」経路が実在する (substrate.md 2026-08-22 実測)。
unknown は既存実装では 3 回連続 error 判定に数えられるので、上限が明けていないだけで
 stalled 化し、2026-08-08 の 26 セッション空費が新エンジンの出力形で再演される。
unknown 死の直後に軽量プローブを打って真の死因を機械的に確定させ、
VISION 最優先の「ループが止まらないこと」へのこの穴を塞ぐ。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0141` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `grep -q 'probe_status' ops/runner/runner.py`
  — runner が死因プローブの結果を result.json の `probe_status` に記録する実装が存在すること。
  実測 rc=1 (`probe_status` という語が runner.py に無い)。
- [ ] `python3 -m unittest ops.tests.test_unknown_death_probe`
  — プローブ・unknown カウンタ・reset 時刻解析を fixture で固定する unit test が存在し通ること
  (substrate 実測文言 UnknownError / Cannot connect / Invalid API key / 429 をすべて覆う)。
  実測 rc=1 (モジュール未存在、`Ran 1 test ... FAILED (errors=1)` の import エラー)。
- [ ] `python3 -c "import json,sys; r=json.load(open('ops/rules.json')); sys.exit(0 if r.get('runner',{}).get('unknown_error_max_rounds') else 1)"`
  — unknown 連続の閾値が `rules.json` の `runner.unknown_error_max_rounds` に置かれていること
  (heart / runner の運用パラメータの単一情報源)。実測 rc=1 (キー未存在)。

verify は DoD の下限であって DoD そのものではない。spec dod (1)(2)(3) の挙動そのもの
(401→auth / 429→usage_limit / 接続不可→network の写像、プローブ失敗時のみ unknown 維持、
opencode 形 reset 解析の best-effort + 正直な None) は verify が見張らない —
テストと PROGRESS.md の証跡で示すこと。**ops/runner/ は spec 明記の人間レビュー必須パスなので
merge は人間待ちになる** (ready_for_review までが器の仕事)。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読した。調べ直さなくてよい)

- `ops/runner/runner.py` (927 行): 分類は純関数 `classify_session_failure()` +
  `FAILURE_PATTERNS` 表 (L59, usage_limit > auth > network > unknown の判定順)。
  opencode は死因を stderr に出さず stdout の type=error イベントで流すため、分類入力は
  `consume_stream_event()` が抽出した `error.data.message`。result.json への死因搬入は
  `failure_fields()` (L512, failure_kind + stderr_tail) が全 write_result 経路で共通に使う。
  unknown 死が停滞化する箇所は 2 系統: worker ループの `consecutive_error >= 3` →
  write_result("error") (L745-756) と initializer ループの即 error (L686)。
  `parse_usage_limit_reset()` (L135) は claude 形 (`limit reached | <epoch>`) 専用で、
  合致しなければ None を返す。
- fixture テストの見本: `ops/tests/fixtures/engine_stderr/` (P-0101 の実測原本) +
  `ops/tests/test_failure_patterns.py`。stdlib のみ (HTTP も `urllib.request`) が repo 慣習。
- 新規設定を持たせない導出元: `ops/models.json` (role→model の単一情報源、
  現在 `opencode-go/ox-alpha-free`) と既存 env `OPENCODE_API_KEY`
  (`rules.json` の `allowed_autopilot_doppler_keys` 済)。**新しい設定ファイル・env キーは増やさない。**
- `ops/validate.py` の rules.json スキーマ検査は固定キーの数値型チェックのみなので、
  `runner.unknown_error_max_rounds` (整数) の追加で壊れない。通知は heart 側の既存
  incident 型 (`ops/heart/notify.py`, 即時送信許可型) に乗る。

### 作り方

1. **プローブ**: session が outcome=error かつ failure_kind=unknown で死んだ直後に、
   models.json 既存設定から導出した推論 API エンドポイントへ 1 リクエスト打つ
   (リトライ・複数エンドポイント試行はしない。spec で 1 リクエストと決まっている)。
   HTTP status を `probe_status` として result.json に記録し、写像 401→auth /
   429→usage_limit / 接続不可→network で既知死因に寄せる。**プローブ自体も失敗した場合のみ
   unknown を維持する** (捏造しない原則の延長)。usage_limit に寄せられた回は既存の
   `hit_usage_limit()` → `quota_wait_or_yield()` 経路 (P-0026) に自然合流する。
2. **別カウンタ**: unknown のまま残った死を、既知死因の `consecutive_error` とは**別の**
   カウンタで数える。閾値は `rules.runner.unknown_error_max_rounds`。超過時は stalled 化ではなく
   incident 型で人間に渡す (「上限か実装詰まりか不明」は停滞ではなく障害報告の対象)。
3. **reset 解析**: `parse_usage_limit_reset()` に opencode 形の best-effort 解析を足す。
   取れなければ今どおり None を正直に返す (claude 形の既存解析は触らない)。
4. **テスト**: `ops/tests/test_unknown_death_probe.py` を新設し、fixture は
   `ops/tests/fixtures/engine_stderr/` 流儀に従う。substrate 実測の 4 文言
   (UnknownError / Cannot connect / Invalid API key / 429) をすべて網羅する。
   HTTP 層は注入可能にして network フリーで通す。

## やらないこと

- **FAILURE_PATTERNS 表への opencode 本物の上限文言追記**。まだ観測されていない (substrate.md)。
  観測されたらその回の stderr_tail を証拠に別途足す。本件はプローブによる機械的確定が主で、
  文言当て込みは補助
- **claude 形分類・models.json ロールバック経路 (claude 復帰) への改修**。温存されているだけの
  既存経路に触れない
- **heart 側 (reconcile / quota_wait_count / waiting_quota 周回の時限) の改修**。runner 側の
  寄せ結果が既存経路に乗るまで。1 PR 1 論点
- **Discord 送信経路の新設**。incident 型は既にある。送信側は heart の既存配線を使う
- **プローブの堅牢化 (再試行・タイムアウト調整の詰め)**。仕様は「軽量・1 リクエスト」。
  失敗したら unknown に落として正直に生きるのが本件の流儀
- **ops/backlog.json / ops/state.json / ops/journal/ の編集**。autopilot 直接 push 領域で
  コンフリクトする (CLAUDE.md)
