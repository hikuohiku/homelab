# P-0227 検死報告 — curriculum Job Failed 3 本 (2026-08-23)

- 調査日: 2026-08-23T19:5xZ (worker セッション 1)
- 対象: health 実測 (2026-08-23T18:30Z) で Failed だった
  `curriculum-system-a791568` / `a791735` / `a791795`
- 結論: **root_cause: auth**

root_cause: auth

## 断定

**failure_kind = auth** — 推論 API (opencode zen) 側の瞬間的な拒否窓で
エンジンセッションが死んだ。同一鍵で隣接する実行が成功しており、恒久的な
鍵無効ではない。usage_limit / network / budget / timeout の証跡は無い。

根拠となる機械実測 (全 5 ファイルが生のまま同梱, `raw-*`):

| result.json (死因欄) | 死んだフェーズ | stderr_tail (エンジンの言葉) | プローブ |
|---|---|---|---|
| raw-result-20260823T094220Z.json | generate | `Provider finish_reason: network_error` | HTTP 401 → auth |
| raw-result-20260823T104210Z.json | generate | 同上 | HTTP 401 → auth |
| raw-result-20260823T141135Z.json | generate | 同上 | HTTP 401 → auth |
| raw-result-20260823T171940Z.json | judge | 同上 | HTTP 401 → auth |
| raw-result-20260823T181430Z.json | judge | 同上 | HTTP 401 → auth |

health (18:30Z) に残っていた 3 Pod はこのうち 14:11 / 17:19 / 18:14 の
3 実行に対応する。Job には `ttlSecondsAfterFinished: 21600`
(ops/heart/spawn.py:130 附近) があるため、09:42 / 10:41 の失敗 Pod は
調査時点で既に GC されていた。

### 証拠の読み方 (substrate の死因表との突き合わせ)

1. **エンジンの言葉だけでは分類できない。** transcript (`raw-transcript-*`) の
   type=error イベントは
   `{"name":"APIError","data":{"message":"Provider finish_reason: network_error",
   "isRetryable":true,"metadata":{"code":"ProviderResponseStreamError"}}}`。
   この文言は FAILURE_PATTERNS のどれにも一致せず classify は unknown に落ちる
   ( substrate「opencode CLI の死因出力」節のとおり opencode は死因を stderr に
   出さず、429 系は UnknownError に潰れる実測済み)
2. **直後の API プローブが 401 を返した。** P-0141 で作られた unknown 死直後の
   軽量プローブ (runner.py `probe_inference_api`) が、死の数十秒後に同じ env の
   鍵で実際に API を叩き HTTP 401 を受け取った。純粋なネットワーク断なら
   URLError (→ network) になるはずで、HTTP 応答が返ったということは
   API に到達していて credential を拒否されたことになる
   (`probe_failure_kind`: 401→auth / 429→usage_limit)
3. **間欠性。** 同じ OPENCODE_API_KEY を載せた隣接実行が成功している
   (09:42 fail → 11:39 gen 成功 → 12:07 done PR #537、14:10 fail → 15:40 done
   PR #547、18:14 fail → 19:27 done PR #562)。鍵そのものは有効で、
   プロバイダ側の一時的な拒否窓 (401 と stream 切断が同時に起きる) と読むのが
   全事実を最もよく説明する

### 残る不確実性 (捏造しない範囲で明記)

- エンジン自身は `isRetryable: true` + "finish_reason: network_error" と報告して
  おり、内部で ~75 秒・6 回の指数退避リトライを使い切ってから死んでいる。
  「stream が切れた」という現象面と「プローブの 401」という計測面の齟齬を
  完全には閉じられない。ただし死因の**層**としてはどちらでも同じ結論になる:
  プロンプトの文言では治らず、再試行で回復する種類の死である
- 本物の上限死 (429) がこれと同じ顔をして unknown/auth に落ちる可能性は
  否定できない (substrate 実測: 429 は UnknownError に潰れる)。ただし今回の
  プローブは 429 ではなく 401 を観測した。上限説を採るには 429 の観測が必要
- pod log / events は取得できていない (このセッションに kubeconfig が無く、
  かつ ttlSecondsAfterFinished で Pod 自体が消滅)。代わりに heart が
  processed/ へ移した直前の runner 書き込み値 (result.json) と PVC 上の
  transcript を証跡とする。内容は加工していない (cp のみ)

## 副次発見 (本 spec の対象外。後続の curriculum が拾うこと)

1. **judge フェーズの死は生成 20〜30 万トークンを道連れにする。**
   17:19 / 18:14 の 2 回は generate 完了後の judge 即死で、proposals.json は
   Job ローカルの /work にしか無いため Job と共に消えた。transcript サイズの
   差 (失敗 judge は 2068 バイト / 直前の成功 generate は 1.79MB) がそれを示す
2. **黙って死む別経路の実例**: 2026-08-22 09:16Z / 16:32Z の 2 回は
   `state=error, error="curriculum generate: completed (failure_kind=None)"` —
   セッションは completed を名乗ったのに proposals.json を書かずに終わっている
   (PROJECT.md 前提 (b) の「completed だが産物無し」型)。今日の 3 Pod とは別の
   死に方なので今回の断定には影響しない
3. opencode は自前でリトライする (isRetryable) が、使い切ると rc≠0 で終了し、
   runner の mode_curriculum には再試行が無いので 1 回の瞬間死が Job 全体の
   Failed になる (backoffLimit: 0)。歯止めの実装は PROGRESS.md 参照
