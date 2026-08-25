# 復旧不能境界確定プラン — Doppler が消えた朝に、どの秘密が二度と生まれないか

Doppler (`homelab/prd`) は全 ExternalSecret の唯一の上流であり、P-0175 は「Doppler 遮断
でも既存 Secret は持ちこたえる」ことを実証した。しかし復旧不能事態 (アカウント消滅・
新規ノード再構築) で「どの秘密の**値**が器の環境から再生成できず Doppler にしか無いか」は
機械分類されたことが無かった。この文書は P-9065 の分類結果に基づく recovery path の列挙で、
**秘密値を一切含まない** (verify の assert)。値の複製はしない — escrow (P-0217) の前段として
境界を 1 枚に固定するのが目的。

- 機械分類の実体: [`ops/health/secret-recoverability.json`](../ops/health/secret-recoverability.json)
- 生成ツール: `python3 ops/tools/secret_recoverability.py` (--selftest 付き)
- 入力元: `apps/` 配下の ExternalSecret manifest の静的走査 (クラスタ到達不能でも再現可能。
  check_credential_map.py と同じ網)

## 分類規則

Doppler キーが `ops/rules.json` の `allowed_autopilot_doppler_keys` (エージェント環境に
入れてよい鍵の allowlist) に**載る** → `recoverable` (器の環境で再生成可能)。
**載らない** → `doppler_only` (値が Doppler にしか無い)。allowlist は事実として読み、
分類を結果に合わせて曲げない。recovery_path はツール内 `RECOVERY_PATHS` が唯一の編集場所で、
新しいキーを参照し始めたら必ずそこに再生成手順を足す (足さないとツールが fail-closed で落ちる)。

2026-08-25 時点の実測: **26 キー** (recoverable 10 / doppler_only 16)、**分類不能 0**。

## recoverable — 器の環境 (allowlist) で再生成可能

これらの値は autopilot エージェント環境に入ることを許された鍵で、Doppler 消滅後も
「再生成する手段」が器の外のどこかに存在する。再生成して Doppler へ書き戻すのが回復手順。

| Doppler キー | recovery path |
|---|---|
| `AUTOPILOT_GITHUB_TOKEN` | GitHub → Settings → Developer settings → Personal access tokens で再発行 (contents write)。autopilot / ops-dashboard / telegram-adapter が共有 |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code の再ログイン (OAuth) で再発行し Doppler へ登録 |
| `DISCORD_WEBHOOK_URL` | Discord サーバー設定 → 連携サービス → Webhook URL を再取得し Doppler へ登録 |
| `OPENCODE_API_KEY` | opencode プロバイダ (LLM API キー発行元) のダッシュボードで再発行し Doppler へ登録 |
| `NATS_CORE_NKEY_SEED` | `nats nk -gen` で再生成し、公開鍵側の認証 (apps/nats/config.yaml の JWT ユーザー定義) を更新してから Doppler へ登録 |
| `NATS_CONSUMER_NKEY_SEED` | 同上 (`nats nk -gen` → 公開鍵認証の更新 → Doppler へ登録) |
| `NATS_DASHBOARD_NKEY_SEED` | 同上 |
| `NATS_PRODUCER_NKEY_SEED` | 同上 |
| `TELEGRAM_ALLOWED_USER_ID` | Telegram アカウントの数値 ID を確認 (機密性低) し Doppler へ登録 |
| `TELEGRAM_BOT_TOKEN` | BotFather で /token を発行し Doppler へ登録 |

> 注意: NATS NKey は「再生成できる」が「同じ鍵が戻る」わけではない。seed を再生成すると
> 公開鍵が変わり、NATS サーバー側の認証定義を同時に更新する必要がある (recovery は
> 「器の環境で値を作り直せる」ことを意味し、「無損失」を意味しない)。

## doppler_only — 値が Doppler にしか無い (再生成境界)

これらの値は allowlist 外で、器の環境からは再生成できない。各キーに「再生成手順」を
書くが、**DB パスワードと RESTIC_PASSWORD は再生成不能** (再生成すると実体と不一致になる /
既存リポジトリを復号できない)。これらは escrow (P-0217) または Doppler のバックアップからの
復元が唯一の経路で、ここが「消えると二度と生まれない」境界の核心。

| Doppler キー | recovery path |
|---|---|
| `GITHUB_HEALTH_REPORTER_TOKEN` | GitHub の PAT を再発行 (repo read) し Doppler へ登録。version-watcher が参照 |
| `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY` | Backblaze コンソール → Application Keys で再発行 (削除権限を持つ restic 鍵) |
| `B2_ACCOUNT_ID_APPEND_ONLY` / `B2_ACCOUNT_KEY_APPEND_ONLY` | Backblaze コンソールで append-only (deleteFiles を含めない) の Application Key を再発行 |
| `RESTIC_B2_BUCKET` | Backblaze コンソールでバケット名を確認 (機密性低) |
| `RESTIC_PASSWORD` | **再生成不能**。restic リポジトリ初期化時に選んだ passphrase。escrow (P-0217) / Doppler バックアップからの復元が唯一の経路 |
| `CODER_DB_PASSWORD` | **再生成不能 (単独では)**。coder-postgres の実パスワードと不一致になるため、変更は ALTER USER + ExternalSecret 更新をセットで |
| `CODER_DB_URL` | CODER_DB_PASSWORD を含む接続 URL の複合値。パスワード再設定時に組み立て直す |
| `DEX_ARGOCD_CLIENT_SECRET` | ランダム値 (48 文字)。`openssl rand -hex 24` で再生成し、dex と argocd の両方へ同じ値を反映 |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | Google Cloud Console → APIs & Services → Credentials の OAuth 2.0 Client を再発行 |
| `IMMICH_DB_PASSWORD` | **再生成不能 (単独では)**。immich-postgres の実パスワードと不一致になるため、変更は ALTER USER + ExternalSecret 更新をセットで |
| `TAILSCALE_CLIENT_ID` / `TAILSCALE_CLIENT_SECRET` | Tailscale Admin Console → Settings → OAuth Clients で再発行 (devices:core:read スコープ) |
| `VAULTWARDEN_ADMIN_TOKEN` | `vaultwarden hash` で新しいハッシュを生成し、vaultwarden の ADMIN_TOKEN 設定と Doppler を更新 |

## 分類不能は 0 (実測)

`spec.dataFrom` はキー名を列挙できないため分類不能として検出し、ツールは fail-closed で
問題にする (validate.py の dataFrom 禁止と同じ発想)。2026-08-25 の実測で dataFrom を使う
ExternalSecret は存在せず、`ops/health/secret-recoverability.json` の `unclassifiable` は空。
将来 dataFrom が現れたら、ツールの problems に「どのファイルか」が載るので、その
ExternalSecret を `spec.data` + `remoteRef.key` の形に直すか、ツールを拡張すること。

## 復旧不能事態にやること (Doppler が消えて再生成もできない朝)

1. `ops/health/secret-recoverability.json` を開く。これが「どの鍵が二度と生まれないか」の唯一の地図
2. `doppler_only` のキーのうち、この文書の recovery path に「再発行」「確認」とある値は
   各プロバイダのコンソールから再生成して Doppler へ書き戻す (Doppler が生き返った場合) か、
   新規プロバイダへ移す
3. **再生成不能と明記した値** (RESTIC_PASSWORD / CODER_DB_PASSWORD / IMMICH_DB_PASSWORD)
   は生成で戻らない。escrow (P-0217) の実装が済むまでは、これらの値の唯一の所在は Doppler。
   アカウント消滅が現実に迫ったら、退避は人間の判断で行うこと (値の複製はこのプロジェクトの
   スコープ外。1 PR 1 論点)
4. `recoverable` のキーは器の環境から再生成できるが、NATS NKey は公開鍵側の認証も同時に
   更新する (上記の注意)

## この文書の維持

- 分類の再生成: `python3 ops/tools/secret_recoverability.py` (出力はコミット済み JSON。再実行は
  決定的で diff を出さない)
- recovery path の更新はツールの `RECOVERY_PATHS` を編集して JSON を再生成する。
  新規 Doppler キーを参照し始めたら必ずそこで足すこと (未定義だとツールが落ちる)
- 秘密値をこの文書・JSON・ツールに書かない (verify が PEM ブロック非含有を検査する)