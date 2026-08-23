# Telegram での止め方・再開の仕方と、届かなかったときの見分け方

autopilot への即時の指示は Telegram の bot DM で行える。判定は LLM を経由しない
決定論 (キーワード照合) で行われるため、文言さえ合っていれば確実に効く。
仕組みの詳細は `apps/openclaw/bridge.py` (P-0107) と `ops/heart/triage.py` を参照。

## 止め方 / 再開の仕方

| やりたいこと | Telegram で送る文 | 効果 |
|---|---|---|
| 全部止める | 「止めて」(「やめて」「中止」「stop」「abort」も可) | 新規タスクの spawn と予告が停止する |
| 特定プロジェクトだけ止める | `veto P-0123` | そのプロジェクトに拒否権 |
| 全部再開する | 「再開」(「resume」も可) | 停止を解除する |

- 短いメッセージ (50 文字以下) の中にキーワードがあれば命令とみなす。「一旦止めてください」「再開お願いします」のような自然な文で通る
- 長文の中で「〜で止めてしまう」と**叙述として**書いた場合は発火しない (誤爆防止の設計)。止めたいときは短く送るか、行頭から書き始める
- 判定されるタイミングは heart の次の起動時。bot からの返信は即座には来ない
- 送る前に文言を確かめたいときは dry-run ドリルが使える:
  `python3 ops/drills/telegram_veto_drill.py --input msg.txt`

## 届かなかったときの見分け方

Telegram から送ったメッセージは、gateway の応答とは無関係に
**ops-feedback ブランチの `ops/feedback/inbox/<id>.json`**
(`source: "telegram"`) に保存される。ここを見るのが最も確実な届き確認。

1. GitHub で ops-feedback ブランチの `ops/feedback/inbox/` を開く
2. 直近の `<id>.json` に `"source": "telegram"` と本文があるか見る
   - ある → 届いている。あとは heart が読む
   - 無い → 届いていない。次へ
3. 疑う点:
   - **allowlist 外のアカウントから送っていないか。** 登録済みユーザー以外の
     メッセージは意図的に無視される (fail-closed)
   - bridge コンテナのログ: `kubectl -n autopilot logs deploy/openclaw -c feedback-bridge`
     (`saved ops-feedback:...` 行が出るか。初回起動直後は履歴を既読化するだけで保存しない)
   - pod 自体が起動しているか (secret 未登録だと CreateContainerConfigError で待機する)

なお Mission Control の書き置きから送ったものは `source: "ops-dashboard"` になる。
issue #56 へのコメントも同じ経路で heart に届く (こちらは transport が違うだけで効果は同じ)。
