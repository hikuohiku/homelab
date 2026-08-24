# telegram-adapter

人間の Telegram DM と autopilot をつなぐ。OpenClaw (P-0090 / P-0107) の置き換え。
Go 標準ライブラリのみの単一バイナリで、2 つのモードを持つ。

| モード | 起動 | 役割 |
|---|---|---|
| adapter | 引数なし | **受信**。`getUpdates` を long poll し、allowlist の private DM を NATS へ publish する |
| mcp | `mcp [--listen host:port]` | **送信**。MCP サーバとして `telegram_reply` ツールを提供する（既定は stdio、`--listen` で HTTP streamable） |

同じバイナリに同居させているのは、allowlist の解釈と Telegram の呼び出し方を
一箇所に保つため。受信と送信で判定がずれると、拾わない相手に喋る事故になる。

## 受信 (adapter)

Deployment として常駐する。経路は次のとおり。

```
人間の DM → Telegram → getUpdates (long poll 50s)
  → allowlist + private 判定 (決定論のみ。LLM は通らない)
  → NATS (events.raw.homelab.telegram) へ publish
  → bus-sidecar → heart の triage / 常駐コア
```

- **git は触らない。** 以前は `ops-feedback` ブランチの inbox にも PUT していたが、
  状態を git から出すのに合わせて閉じた (設計 state-out-of-git Phase 7)
- note ID に `update_id` を埋めてあるので、再処理しても同じ `Nats-Msg-Id` になり
  JetStream が重複を落とす。cursor の永続化 (PVC) が要らない
- **publish が通るまで Telegram の offset を進めない。** 出口が 1 本なので、
  送れなかったものを受信済みにすると書き置きが消える
- NATS が未設定なら起動しない (fail-closed)
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

転送は 2 通り。既定の stdio では opencode の**子プロセス**になるため、Telegram の
トークンを opencode 自身の env に置くことになる。コアに bash があると
`cat /proc/self/environ` でそれが読めるので、**本番のコアは HTTP を使う** —
別コンテナで待ち受け、秘密はそちらの env にだけ置く（`apps/autopilot-core`）。

```json
{
  "mcp": {
    "telegram": {
      "type": "remote",
      "url": "http://127.0.0.1:4097/mcp",
      "enabled": true,
      "oauth": false
    }
  }
}
```

`"oauth": false` は必須。省くと OAuth の自動検出が走り、401 を `needs_auth` として
扱う経路に入る。`headers` に秘密を置かないこと — opencode の `GET /config` は
`{env:...}` 展開後の値をそのまま返す。同一 Pod の loopback はネットワーク名前空間が
境界なので、そもそも認証が要らない。

stdout は JSON-RPC 専用で、ログはすべて stderr に出す。

## 環境変数

| 変数 | adapter | mcp | 既定 |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 必須 | 必須 | — |
| `TELEGRAM_ALLOWED_USER_ID` | 未設定なら取り込み 0 件で待機 | 未設定なら起動失敗 | — |
| `TELEGRAM_ACK_TEXT` | 空なら ack を送らない | — | — |
| `ADAPTER_POLL_SECONDS` | long poll の秒数 | — | `50` |
| `NATS_URL` | 必須 (未設定なら起動失敗) | 不要 | — |
| `NATS_NKEY_SEED` | 必須 (未設定なら起動失敗) | 不要 | — |
| `NATS_SUBJECT` | publish 先 | — | `events.raw.homelab.telegram` |

## 開発

```bash
cd apps/telegram-adapter/app
gofmt -l . && go vet ./... && go test ./...
```

イメージは `apps/telegram-adapter/app/**` の push で自動ビルドされる
(`.github/workflows/build-telegram-adapter-image.yml`)。
ビルド後は SHA タグの OCI index digest を実測して `deployment.yaml` を pin し直すこと。
