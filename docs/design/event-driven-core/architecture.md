# Autopilot Event-Driven Core — 設計書

2026-08-23 · 提案段階（未実装）。インタラクティブ図: [architecture.html](architecture.html)

改訂 rev2: 思考エンジンが opencode であることを反映し、常駐基盤を `opencode serve` に
変更（D4/D5 破棄）。Codex レビュー指摘の事実誤認 3 件を訂正し、D8 を撤回。

## 一言で

入力をイベントバスに集約し、rules + 安価モデルの分類器がゴミを吸収し、
**`opencode serve` の常駐セッション**（コア）が wake を受けて即応・起票・ディスパッチする。
納品の決定論（heart-and-projects）はゲートとして残す。

## 動機

現行の入力はすべてポーリング（heart 60 秒ビート、Telegram bridge 15 秒、
health 30 分、feedback は git/issue の毎ビート走査）。この構造の限界:

- 人間の発話への初応答が最悪ビート数分遅れ、かつ heart のキーワード triage しか通らない
- 障害遷移も 30 分 + ビートの遅延を食う
- すべての判断入力が「毎ビート全量再読込」なので、ソースを足すたび heart が太る

VISION の「身の回りのイベントに応じて自発的に動く。指示を待たない」に対し、
イベントが「届く」経路そのものが無い。

## 推奨構成

```
producers ──▶ イベントバス ──▶ 分類器 ──▶ 常駐コア ──▶ ディスパッチ
(adapter 群)  events.raw.*   rules+安価LLM  events.core   ├─ 即応 (OpenClaw/Discord)
                                          (prompt_async)  ├─ task-request → bus → heart
                                                          ├─ 調査 spawn 依頼 → heart
                                                          └─ journal / seeds → heart
```

### 1. 取り込み層（producers / adapters）

| ソース | 変更 |
|---|---|
| Telegram | **`telegram-adapter` を新設**し OpenClaw は撤去（D26）。`getUpdates` の long poll → allowlist 判定 → publish。応答・起票はコアに一本化（D14）、送信は Bot API 直（D25） |
| health | **常駐 watcher を新設**して遷移を即時 publish。30 分 CronJob はスナップショット用に併存（D15） |
| GitHub | poller adapter（cursor 付き 60 秒 poll → publish）。Tailscale 内に webhook は届かず、Funnel での公開はしない（D6） |
| K8s Job | watcher adapter が Job 完了を publish（コアが結果をポーリングしない） |
| ダッシュボード書き置き | 当面現行のまま（ops-feedback ブランチ）。後続で publish 化 |

producer は判断しない。捨てるかどうかは分類器に一元化する。
publish が server に受理されてから cursor を進める（未受理で cursor を進めない）。

### 2. イベントバス

第一候補は NATS JetStream（単一 Pod・永続ストリーム・durable consumer の ack/再配送）。
ただし実負荷は GitHub 60 秒・health 遷移・Telegram 少数と小さく、既存の
ops-state ブランチ queue や PVC 上の追記ログでも要件を満たす可能性がある。
**採用は latency / consumer 数 / replay 期間 / 件数を数値化した比較の後に確定する**（D16）。

- subjects: `events.raw.<domain>.<source>` / `events.core.<domain>` /
  `events.heart.<domain>.<command>`（判断済み・コマンド系にも domain を入れる）
- 認証は NKey または mTLS で、producer / classifier / core / heart ごとに
  最小 pub/sub ACL を持つ。Phase 1 の完了条件に含める
- retention は subject ごとに MaxAge / MaxBytes / MaxMsgs と DiscardOld を設定。
  無期限保存はしない（単一ノードのボリューム枯渇で全経路が同時停止するため）

### 3. 分類器（前段のゴミ吸収）

バス consumer の小プロセス。2 段構え:

1. **rules**（コスト 0）: dedup・デバウンス・既知ノイズ drop。障害遷移と人間の発話は
   ここで即 wake（高速レーン、LLM を待たない）。停止キーワードもここ（D12）
2. **安価モデル 1 call**: rules で白黒つかないものを `discard / batch / wake` の
   3 値 + priority で判定。迷ったら上位へ（取りこぼしの方が高くつく）

batch は**専用の永続バッファ**（別 durable stream か KV/PVC）へ書いてから raw を ack し、
6 時間毎に 1 通へ束ねて wake する（D9）。メモリ上に溜めない。

### 4. 常駐コア — `opencode serve`

長命 Pod で `opencode serve` を起動し、HTTP + SSE のクライアントとして駆動する。
Claude Agent SDK ではなく opencode を使う（D17）。思考エンジンは既に opencode へ
移行済み（`ops/models.json` 全役 + `ops/runner/runner.py:219-228`）であり、
定額枠を使い切る方針に沿う。

| 用途 | API |
|---|---|
| 常駐 | `opencode serve --port 4096`（`OPENCODE_SERVER_PASSWORD`） |
| イベント投入 | `POST /session/{id}/prompt_async` |
| 結果受信 | `GET /event`（SSE）/ `session.idle` で一手完了を検知 |
| 文脈維持 | セッション storage は永続。session_id を ops-state に保持して再起動後も継続 |
| 世代交代 | `POST /session/{id}/compact`、実験は `POST /session/{id}/fork` |
| モデル切替 | `POST /session/{id}/model`（平常は安価、重い判断だけ上位へ） |

- **やること**: 人間への即応 / task-request 起票（bus 経由）/ 調査 spawn の依頼
  （heart 経由）/ journal・seeds への書き込み（bus 経由）/ incident 通知
- **やらないこと**: 実装・merge（heart の納品ゲートのまま）/ 重い調査（Job に委譲）/
  git への直接書き込み（D3）/ K8s write（D7）
- **権限**: `--auto` の丸投げをしない。`permission.asked` を SSE で受けて自前ポリシーで
  `once|always|reject` を返す。CHARTER §5 の歯止めを実装として持てる（D18）
- **liveness**: イベント到着とは独立した定期 heartbeat + consumer lag を持ち、
  「無入力」と「処理停止」を区別する（D19）
- **外部送信の冪等化**: event_id をキーに送信 intent/result を永続化する outbox を持ち、
  再配送時に完了済み intent を再実行しない（D20）
- セッション storage は自動 GC されない既知問題があるため、定期削除を入れる

### 5. heart は「変えない」ではなく「拡張する」

reconcile の純関数、reviewer + CI + soak の納品ゲート、breaker、ops-state 単一書き手 —
この骨格は維持する。ただし bus command を消費する経路は**新規実装が必要**であり、
これを設計の一部として明記する（D21）。現行 `facts.py` は Git/K8s/PVC しか収集せず、
`reconcile.py` に bus command の遷移は無く、`spawn.py` は調査 spawn を知らない。

必要な追加: bus inbox の収集 / command スキーマ / reconcile の遷移 / spawn 種別 /
command_id による処理済み台帳（台帳を永続化してから ack）/ 決定論的 Job 名 / テスト。

コアが死んでも heart は現行どおり自走する（可用性のフォールバック）。

## 段階導入

| Phase | 内容 | 完了条件 |
|---|---|---|
| 0 | bus + adapter + 分類器 + heart の command consumer。コアは shadow（判定を記録するだけ） | 分類差分の検分、stop・task-request の canary（D8 撤回） |
| 1 | 人間レーン（即応・起票）と障害レーンを active 化 | Telegram 往復が実輸送で通る（P-0118 の宿題） |
| 2 | digest 安定化、heart の feedback ポーリング削減、立案の段階吸収（D1/D2）、声の一元化（D11） | 旧経路を落としても取りこぼしゼロ |

継続監査: discard は raw ストリームに痕跡が残るので、週次でサンプルを目視し、
誤り検出時にルール/プロンプトを直す。

## 決定記録

### 2026-08-23 ヒアリング

- **D1. コアは将来の脳**: Phase 2 以降、立案（curriculum の役割）をコアが段階的に
  吸収し、heart は執行と納品ゲートに純化する。
- **D2. 選定はコア、起草は Job**: コアは「何をやるか・優先度」を判断し、spec の
  起草・検証は planning Job に発注する。コアが長時間塞がる作業を持たない。
- **D3. コアは git に書かない**: task-request・journal・seeds はすべて bus への
  publish とし、commit は heart だけが行う（単一書き手の不変条件を維持）。
- **D6. GitHub は poller adapter**: cursor 付き 60 秒 poll → publish。外部公開はしない。
- **D7. コアは K8s write を持たない**: 調査 spawn も bus 経由で heart が実行し、
  breaker・並列上限・監査を一元維持する。障害初動は第一報を即時に出す。
- **D9. digest は 6 時間毎**: batch 判定イベントは 6h 毎に 1 通へ束ねて wake。
- **D11. 声は Phase 2 でコアに一元化**: heart の announce / deliver / question も
  コアが人語で伝える。コア停止時のフォールバックは D22 を参照。
- **D12. 停止・veto は二重系**: rules 段（LLM 以前）のキーワード判定を直行 publish し、
  コアの自然言語理解を補助として上乗せ。解除は人間のキーワードのみ。
- **D13. subject にドメイン境界**: `events.raw.<domain>.<source>` 命名で、Phase 3 の
  lethal trifecta 分離（送信能力なしの第二コア等）の選択肢を保存する。

### 2026-08-23 rev2（Codex レビューと実行環境の訂正を反映）

- **D4/D5 破棄**: 「平常 Sonnet・選定 Opus」「分類器は Haiku」は前提が誤り。思考エンジンは
  opencode（`opencode-go/ox-alpha-free`）であり、Anthropic API key も存在しない。
- **D14. 口はコアが持つ（決定）**: OpenClaw を **transport-only** にし、応答と起票は
  コアだけが行う。`agents-md.yaml:32-41` の「自ら要約・返信し `/api/feedback` に
  task-request を POST する」指示は撤去し、OpenClaw のエージェント応答を無効化する。
  Telegram の `update_id` を全経路の共通冪等キーにする。
  実装上の未確認事項: OpenClaw をエージェント無効のまま起動でき、かつ受信が
  `channel_ingress_events` に入り続けるかは実機検証が要る（Pod 自体は稼働中）。
- **D25a. 発話は `telegram_reply` MCP ツールで行う（D25 の実装形。実装済み）**:
  コアが Bot API を直に組み立てるのではなく、`telegram-adapter mcp` が提供する
  MCP stdio ツールを呼ぶ。**宛先は引数に取らず** allowlist の所有者に固定するため、
  プロンプト注入で到達先を変えられない。受信と送信で allowlist の解釈がずれない
  よう、受信アダプタと同じバイナリに同居させている。
- **D25. コアの発話は Telegram Bot API を直接叩く**: OpenClaw ゲートウェイ経由に
  しない。理由: `autopilot` ns の Service は `ops-dashboard` のみで **OpenClaw には
  Service が無く**（`config.yaml` にも「Service / Ingress は作らない」と明記）、
  コアから届く安定した宛先が存在しない。Service 新設 + ゲートウェイ API 依存より、
  既存 Secret の `TELEGRAM_BOT_TOKEN` で `sendMessage` を直接呼ぶ方が部品が少なく、
  **OpenClaw が落ちてもコアの声が止まらない**。
  代替案（不採用）: OpenClaw に Service を新設し `OPENCLAW_GATEWAY_TOKEN` で
  control plane API を使う。OpenClaw 側の送信 API の有無は未検証。
- **D26. OpenClaw は撤去し、`telegram-adapter` に置き換える**: transport-only にすると
  役割は「allowlist 付きで DM を受けて bus に流す」だけになり、OpenClaw の実体とは
  釣り合わない。2026-08-23 の実機ログで確認した現状:
  - agent runtime が 9 プラグイン（browser / canvas / device-pair / file-transfer /
    memory-core / ollama / phone-control / talk-voice / telegram）を読み込み、
    Telegram メニューに **63 コマンド**を登録している
  - 自前の LLM（`go/ox-alpha-free`）を抱えている
  - bridge サイドカーの送信実績はゼロ（起動時の cursor 初期化ログのみ。
    P-0118「口は開いたが号令は通ったことがない」と整合）

  受信専用の用途に対して面積が大きすぎ、攻撃面（Telegram 経由で到達可能な
  file-transfer / phone-control / browser）も割に合わない。置き換え後に残す機能は
  「allowlist の送信者からの private DM だけを取り込む」の一点。

  **重要な制約**: OpenClaw は webhook ではなく **long polling** で受信している
  （ログ `[telegram] [diag] isolated polling ingress started`。Ingress も Service も
  無いため webhook は原理的に受けられない）。Telegram の `getUpdates` は
  **同一 bot token に対して同時に 1 つの消費者しか許さない**（並行すると 409）。
  したがって Telegram レーンだけは**並行 shadow ができず、原子的な切り替え**になる。
  D23 の shadow ゲートはこのレーンには適用できないので、切替前に adapter を
  別 bot token で単体検証してから入れ替える。

  撤去対象: `apps/openclaw/`（Deployment / PVC / ConfigMap ×2 / ExternalSecret /
  Application / bridge.py）、`apps/kustomization.yaml` の参照、`ops/inventory.json` の
  openclaw イメージ pin、`ops/rules.json` と `ops/check_credential_map.py` の
  `OPENCLAW_GATEWAY_TOKEN`、`ops/tests/test_openclaw_bridge.py`。
  残すもの: `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_ID`（adapter が使う）、
  `OPENCODE_API_KEY`（runner が使う）。
- **D15. health は常駐 watcher を分離**: `*/30` CronJob のままでは「遷移の即時 publish」は
  原理的に不可能（プロセスが動いていない）。常駐 watcher Deployment を新設し、
  30 分スナップショットの CronJob と併存させる。
- **D16. バス採用は数値比較の後に確定 → NATS JetStream で確定（2026-08-23）**:
  当初は「遅延を縮めるため」の検討だったが、実測すると自前で縮められるのは
  0〜30 秒の区間だけで、支配項は LLM の思考時間だった（バスでは体感が変わらない）。
  採用の理由は遅延ではなく**構造**にある — 自宅クラスタの内部イベント（所有者の
  「止めて」を含む）が GitHub という外部 SaaS を経由しており、GitHub が落ちるか
  トークンが切れると緊急停止が届かない。この依存を切るためにバスを入れる。
  外部依存は許容する方針を所有者が明示（「綺麗に使えるなら外部依存あっていい」）。
- **D27. heart も Go へ寄せる（方針・未着手）**: producer / consumer が Go に揃うため、
  Python の heart だけが NATS を素で話せない。所有者の意向は Go への統一。ただし
  heart は 218 テストを持つ決定論の要なので、**バス導入とは別立てで段階的に**行う。
  それまでは heart の入力経路を変えない（archive された ops-feedback を読み続ける）。
- **D17. 常駐基盤は `opencode serve`**: HTTP + SSE のサーバーで、セッション永続・
  fork・compact・実行中のモデル切替を公式に持つ。Claude Agent SDK は使わない。
- **D18. 権限は SSE の permission イベントで自前判定**: `--auto` に丸投げせず、
  `permission.asked` を受けて自前ポリシーで応答する。
- **D19. liveness はイベント到着から独立**: 定期 heartbeat + consumer lag。
  「静かな正常」と「ハング」を区別する（D9 の 6 時間間隔と両立させるため必須）。
- **D20. コアの外部送信を冪等化**: event_id キーの outbox と単一 sender。ack 前の
  クラッシュで通知・返信が二重送信されるのを防ぐ。
- **D21. heart の改修を設計範囲に含める**: bus command の消費経路は新規実装。
  command_id 台帳を永続化してから ack する。
- **D22. 通知の機械的な上限は残す（D10 を撤回）**: hard cap・event_id dedup・
  incident 集約・通知 breaker を機械で強制し、会話による調整はその上乗せとする。
  コア停止時は critical 型すべてを現行 Notifier からフォールバック送信する。
- **D23. shadow ゲートを復活（D8 を撤回）**: 現行 heart も 300 shadow beat を経て
  active 化した。stop の偽陽性 1 件で全非終端プロジェクトが kill されうるため、
  実データ shadow・旧経路との判定差分・canary を昇格条件にする。
- **D24. モデル ID は一箇所に外出しし fallback を持つ**: `ox-alpha-free` は
  (a) 無料終了日が非公表、(b) Go 利用上限到達でブロックされる既知バグ
  （opencode#44173）、(c) Zen 経由だとツール付きリクエストが失敗（同 #44300）
  という無人運用上のリスクを持つ。常駐化と同時に fallback（deepseek-v4-flash 等の
  Go 定額内モデル）への自動切替を入れる。

## 残課題（実装時に詰める）

- ダッシュボード書き置き経路の publish 化（Phase 2）
- journal / seeds を main に反映する経路（curriculum は main から読むため、
  ops-state に置くなら明示注入、main に置くなら PR 経路が要る）
- 立案者の owner フェンス（`planning_owner=heart|core` と lease）
- コアと分類器の消費量を breaker の集計対象に含める（現行は runner result のみ）
- コアのシステムプロンプト（CHARTER のどの節を継承するか）と compact 閾値の実測調整
