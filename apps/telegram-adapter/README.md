# telegram-adapter

人間の Telegram DM と autopilot をつなぐ。OpenClaw (P-0090 / P-0107) の置き換え。
Go 標準ライブラリのみの単一バイナリで、2 つのモードを持つ。

| モード | 起動 | 役割 |
|---|---|---|
| adapter | 引数なし | **受信**。`getUpdates` を long poll し、allowlist の private DM を `ops-feedback` の inbox へ保存する |
| mcp | `mcp` | **送信**。MCP stdio サーバとして `telegram_reply` ツールを提供する |

同じバイナリに同居させているのは、allowlist の解釈と Telegram の呼び出し方を
一箇所に保つため。受信と送信で判定がずれると、拾わない相手に喋る事故になる。

## 受信 (adapter)

Deployment として常駐する。経路は次のとおり。

```
人間の DM → Telegram → getUpdates (long poll 50s)
  → allowlist + private 判定 (決定論のみ。LLM は通らない)
  → ops-feedback:ops/feedback/inbox/<id>.json へ PUT
  → heart が毎ビート走査 → triage
```

- note ID に `update_id` を埋めてあるので、再処理しても同じパスへの PUT になる。
  422 (既存) は「保存済み」として成功扱いにするため、cursor の永続化 (PVC) が要らない
- 未処理の update は起動時にすべて処理する。Telegram は offset で ack された分だけ
  キューから消すので、ダウン中に届いた分も復帰後に受け取れる
- private チャット限定。allowlist の送信者でも、グループでの発言は拾わない

## 送信 (mcp)

常駐コアが人間へ直接返信するための口。ツールは 1 つだけ。

```
telegram_reply(text) → allowlist の所有者へ DM を送る
```

**宛先を引数に取らない。** 送り先は常に `TELEGRAM_ALLOWED_USER_ID` で固定されており、
呼び出し側が指定する余地が無い。プロンプト注入で「別の相手に送れ」と指示されても
到達先は変わらない。`TELEGRAM_ALLOWED_USER_ID` が未設定なら起動そのものが失敗する
(fail-closed)。

opencode から使うときの設定:

```json
{
  "mcp": {
    "telegram": {
      "type": "local",
      "command": ["/telegram-adapter", "mcp"],
      "environment": {
        "TELEGRAM_BOT_TOKEN": "...",
        "TELEGRAM_ALLOWED_USER_ID": "..."
      }
    }
  }
}
```

MCP モードは inbox に触らないので `AUTOPILOT_GITHUB_TOKEN` を要求しない。
stdout は JSON-RPC 専用で、ログはすべて stderr に出す。

**未了**: 常駐コアはまだ無い。コアを立てるときに、この bin をコア側のイメージへ
運ぶ経路 (adapter イメージからの multi-stage COPY 等) を決める必要がある。
設計は [`docs/design/event-driven-core/`](../../docs/design/event-driven-core/) を参照。

## 環境変数

| 変数 | adapter | mcp | 既定 |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 必須 | 必須 | — |
| `TELEGRAM_ALLOWED_USER_ID` | 未設定なら保存 0 件で待機 | 未設定なら起動失敗 | — |
| `AUTOPILOT_GITHUB_TOKEN` | 必須 | 不要 | — |
| `TELEGRAM_ACK_TEXT` | 空なら ack を送らない | — | — |
| `ADAPTER_POLL_SECONDS` | long poll の秒数 | — | `50` |
| `ADAPTER_BRANCH` | 保存先ブランチ | — | `ops-feedback` |
| `ADAPTER_INBOX_DIR` | 保存先ディレクトリ | — | `ops/feedback/inbox` |

## 開発

```bash
cd apps/telegram-adapter/app
gofmt -l . && go vet ./... && go test ./...
```

イメージは `apps/telegram-adapter/app/**` の push で自動ビルドされる
(`.github/workflows/build-telegram-adapter-image.yml`)。
ビルド後は SHA タグの OCI index digest を実測して `deployment.yaml` を pin し直すこと。
