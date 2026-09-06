# discord-dialogue-data-bot

Discord の VC を話者別に録音し、Soniox で文字起こし、Gemini で議事録を作って Discord へ返す bot。

ソースと image は [voicist/discord-dialouge-data-bot](https://github.com/voicist/discord-dialouge-data-bot)
にある。ここにあるのは k8s 上の器だけで、アプリの仕様はあちらの README が持つ。

## 人間が一度だけやること

1. Doppler (homelab/prd) に 4 つのキーを登録する。値はこのリポジトリには置かない。

   | キー | 取得元 |
   |------|--------|
   | `DISCORD_BOT_TOKEN` | Discord Developer Portal の専用 Bot。`bot` + `applications.commands` スコープでサーバーへ招待し、録音 VC の閲覧・接続と、送付先チャンネルの閲覧・メッセージ送信・ファイル添付を許可する。特権 Intent は不要 |
   | `SONIOX_API_KEY` | Soniox コンソール |
   | `GEMINI_API_KEY` | Google AI Studio |
   | `GHCR_VOICIST_PULL_TOKEN` | GitHub の classic PAT。`read:packages` のみ。private パッケージを pull するためだけに使う |

2. Pod が上がったら、録音したい VC に参加した状態で、議事録の送付先にしたい
   テキストチャンネルで `/dialogue setup` を一度実行する。サーバー ID・チャンネル ID の
   手入力は無く、この操作で `state.sqlite` に保存される。以後は再起動しても再設定は要らない。

## image の更新

上流の `main` に push が入ると
[build-image.yml](https://github.com/voicist/discord-dialouge-data-bot/blob/main/.github/workflows/build-image.yml)
が ghcr.io へ焼き、workflow の notice に digest を出す。その digest を
`deployment.yaml` の `image:` に貼る（tag ではなく digest で pin する）。

## 承知している穴

- **backup が無い。** 収集原本は録り直しがきかないが、restic → B2 を今は付けていない。
  理由と、足す条件は `ops/tests/test_backup_coverage.py` の `EXEMPT_PVCS` に書いてある。
- **API の月次上限が未設定。** `MONTHLY_ASR_SECONDS` / `MONTHLY_SUMMARY_CALLS` は
  既定の 0（無制限）のまま。金額の上限は Soniox / Google 側でも設定すること。
