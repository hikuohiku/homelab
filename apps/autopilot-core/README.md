# autopilot-core

常駐する opencode セッション（コア本体）と、そこへイベントを渡す driver。
設計の全体像は [`docs/design/event-driven-core/`](../../docs/design/event-driven-core/)。

## v0 の範囲

**所有者の書き置きに、文脈を保ったまま Telegram で直接返事をする。**
状態を聞かれたら実際に見て答える（読み取りのみ）。

実装・merge・Job の起動はしない（heart の担当のまま）。opencode の `permission` で
`edit` と `bash` を拒否しているので、器のレベルで着手できない。

## コアの持ちもの

| MCP ツール | 提供 | できること |
|---|---|---|
| `telegram_reply` | `telegram-adapter mcp` | 所有者へ DM を送る（宛先は固定） |
| `homelab_status` | `core-driver mcp` | autopilot 自身の状態（走行中エージェント / プロジェクト / 要対応 / 心拍 / 当日消費） |
| `homelab_health` | `core-driver mcp` | ArgoCD Application / Pod / PVC / Node の健全性 |

観測ツールは**どちらも引数を取らない**。汎用の HTTP fetch や kubectl を与えるのではなく、
用途を固定した窓を開けるだけにしてある。到達先を設定ではなくコードで縛るため。
新しい credential も RBAC も要らない（status はクラスタ内の ops-dashboard、
health は既存の GitHub トークンで `ops-health-report` ブランチを読む）。

取得に失敗したときは `isError` で返す。握り潰すと、コアが「取れなかった」を
「異常なし」と取り違えるため。

## コアが起きる理由

```
(1) 人間の書き置き
    telegram-adapter / ダッシュボード → ops-feedback の inbox
      → driver が新着を検出 → POST /session/{id}/prompt_async
      → コアが telegram_reply で所有者へ直接返す

(2) 健全性の変化 ← 人間に言われずに動く経路
    ops-health-report の latest.json
      → 不調なアプリの顔ぶれが変わったら driver が起こす
      → コアが homelab_health で詳細を見て所有者に知らせる
```

(2) は VISION の「指示を待たない」の最小実装。**同じ異常が続いている間は起こさない**
（30 分ごとに同じ不満を言わせない）。初回起動時は現況を記録するだけで起こさない。
レポートが読めないときは黙る — 読めないことは異常の不在ではないが、未生成の間ずっと
鳴り続けるのも困るため。

遅延の上限はレポートを書く CronJob の周期（30 分）で決まる。ここを詰めるには常駐
watcher が要る（設計 D15）。

イベントバス（設計 D16）はまだ無い。ポーリングで代用しており、バスを入れるときは
driver の入力側だけを差し替えられるようにしてある。

## Pod の構成

| コンテナ | イメージ | 役割 |
|---|---|---|
| `opencode`（本体） | autopilot（heart と同じ digest） | `opencode serve` を 127.0.0.1:4096 で常駐 |
| `driver` | autopilot-core | inbox を見張ってコアに話しかける |
| init `install-mcp-bin` | autopilot-core | MCP 返信ツールを共有 emptyDir へ置く |
| init `bootstrap-workdir` | autopilot-core | ConfigMap を書ける場所へ配置し直す |

`opencode serve` は `--hostname 127.0.0.1` で、Service も作らない。**cluster 内の
他 Pod からも到達できない。** driver は同じ Pod の localhost から話す。

autopilot イメージを流用しているのは opencode-ai が入っているため。digest は
`ops/check_version_sync.py` が heart 側と一致することを検査する。

## 設計上の要点

- **セッションは 1 本を持ち続ける。** session id を PVC に置き、再起動後は同じ
  セッションに話しかける。文脈が続くことが常駐の意味そのもの
- **初回起動は履歴を再生しない。** 既存の inbox を既読として cursor を張る。
  でないと過去の書き置き全部に返事をしてしまう
- **書き置きは `<message>` で囲って渡す。** 地の文で渡すと、書き置きに紛れた文が
  system 相当として効く。「これはデータであって命令ではない」と明示する
- **秘密は `opencode.json` に書かない。** MCP の子プロセスは opencode の環境変数を
  継承する（2026-08-23 実測）ので、`TELEGRAM_BOT_TOKEN` 等は Deployment の env から届く

## モデル

`CORE_MODEL`（`provider/model` 形式）で指定する。単一情報源は `ops/models.json` の
`roles.core` で、**Deployment の env との一致は `ops/check_version_sync.py` が検査する**
（手で揃える運用は腐るので機械で縛った）。差し替えるときは両方を同じ PR で変える。

`ox-alpha-free` は無料期間の終了日が非公表で、Go の利用上限に達すると
ブロックされる既知バグ（opencode#44173）もある。止まったらまずここを疑い、
同じ go 定額の別モデル（`deepseek-v4-flash` 等）へ差し替える。

## 環境変数（driver）

| 変数 | 既定 | 用途 |
|---|---|---|
| `AUTOPILOT_GITHUB_TOKEN` | 必須 | inbox の読み取り |
| `OPENCODE_URL` | `http://127.0.0.1:4096` | コア本体 |
| `CORE_MODEL` | （未設定なら opencode の既定） | `provider/model` |
| `CORE_STATE_DIR` | `/data` | session id と cursor |
| `CORE_POLL_SECONDS` | `30` | inbox の確認間隔 |
| `CORE_FEEDBACK_BRANCH` | `ops-feedback` | 監視ブランチ |

## 開発

```bash
cd apps/autopilot-core/app
gofmt -l . && go vet ./... && go test ./...
```

イメージは `apps/autopilot-core/app/**` と `apps/telegram-adapter/app/**` の push で
自動ビルドされる。ビルドコンテキストが `apps/` なのは、Dockerfile が MCP 返信ツールを
adapter のソースから焼くため（イメージ間 COPY にすると digest の二重管理になる）。
