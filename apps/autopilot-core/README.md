# autopilot-core

常駐する opencode セッション（コア本体）と、そこへイベントを渡す driver。
設計の全体像は [`docs/design/event-driven-core/`](../../docs/design/event-driven-core/)。

## 範囲

**所有者の書き置きに、文脈を保ったまま Telegram で直接返事をする。**
聞かれたら実際に見て・読んで答える（読み取りのみ）。

実装・merge・Job の起動はしない（heart の担当のまま）。opencode の `permission` で
`edit` を拒否し、`bash` は `main` の履歴を読む git だけに絞ってあるので、器のレベルで
着手できない。

## コアの持ちもの

| MCP ツール | 提供 | できること |
|---|---|---|
| `telegram_reply` | `telegram-adapter mcp` | 所有者へ DM を送る（宛先は固定） |
| `homelab_status` | `core-driver mcp` | autopilot 自身の状態（走行中エージェント / プロジェクト / 要対応 / 心拍 / 当日消費） |
| `homelab_health` | `core-driver mcp` | 健全性レポート（30 分ごとの定点観測） |
| `homelab_applications` | `core-driver mcp` | ArgoCD Application の sync/health を **live** で（k8s API 直読み） |
| `homelab_pods` | `core-driver mcp` | 全 namespace の Pod を live で（phase / ready / 再起動 / 理由） |
| `homelab_events` | `core-driver mcp` | Normal でない Event を新しい順で |
| `request_task` | `core-driver mcp` | heart のタスク依頼キューに起票する |

観測ツールは**すべて引数を取らない**。汎用の HTTP fetch や kubectl を与えるのではなく、
用途を固定した窓を開けるだけにしてある。到達先を設定ではなくコードで縛るため。

取得に失敗したときは `isError` で返す。握り潰すと、コアが「取れなかった」を
「異常なし」と取り違えるため。

### live の 3 つはどうやって k8s を読んでいるか

Pod は `automountServiceAccountToken: false` のまま。projected な
`serviceAccountToken` volume を作り、**`core-driver-mcp` コンテナにだけ**
`volumeMounts` している。`volumeMounts` はコンテナ単位なので、`opencode` 本体からは
トークンが見えない。コアは MCP ツールという固定された窓越しにしかクラスタを読めず、
生の API を叩く経路を持たない。

権限は既存の ClusterRole `autopilot-reader`（`apps/autopilot/rbac.yaml`）を bind して
いるだけ。**get/list のみ・書き込み動詞なし・Secret を含まない。**

`403` は握り潰さず `isError` で返す。RBAC が足りないことを「異常なし」に化けさせない。

## 作業コピー（コアが `main` を読む）

driver が PVC 上に `main` の clone を持ち、5 分ごとに合わせ直す（`app/repo.go`）。
`opencode` コンテナには **read-only で** `/data/repo` に mount してある。

- 中身は `read` / `grep` / `glob` でそのまま読める
- 履歴は bash の git で読む。許可されているのは **`git -C /data/repo <サブコマンド>`**
  の形だけで、`log` / `show` / `diff` / `blame` / `grep` / `ls-files` / `ls-tree` /
  `shortlog` / `describe` / `rev-parse`（と `date`）だけ
- **書けない。** read-only mount であり、push 用の credential もこのコンテナには無い
  （設計 D30。allowlist ではなくマウントの性質として持たせている）

更新は `fetch` → `reset --hard FETCH_HEAD` → `clean -fd` で、**ディレクトリ自体は
消さない**。作り直すと opencode 側の subPath mount が古い inode を指したままになり、
コアから中身が消えたように見える。

### `ask` を作らないこと

opencode の permission は **マッチするルールが無いと既定が `ask`**。誰も答えられない
常駐コアが `ask` を踏むと、応答待ちのまま固まる（CHARTER §5.1、run #1 と同型）。
だから `config.yaml` では**使いうるツールを全部列挙して allow か deny で埋めている**。
`ops/tests/test_core_permissions.py` が CI で毎回検査する（`ask` が無いこと、
種別の書き漏らしが無いこと、`bash` がブランケット allow でないこと）。

`permission` は `opencode.json`（エージェントレベル）にだけ書く。**driver が
`POST /session` に `permission` を渡してはいけない** — セッションレベルの deny は
子エージェントへ伝播して上書きできなくなる。

## コアが起きる理由

```
(1) 人間の書き置き
    telegram-adapter / ダッシュボード → ops-feedback の inbox（GitHub）
                                     → NATS の events.raw.*（設計 D16）
      → driver が両方から拾う → POST /session/{id}/prompt_async
      → コアが telegram_reply で所有者へ直接返す

(2) 健全性の変化 ← 人間に言われずに動く経路
    ConfigMap autopilot/ops-health-report の latest.json
      → 不調なアプリの顔ぶれが変わったら driver が起こす
      → コアが homelab_health で詳細を見て所有者に知らせる
```

(2) は VISION の「指示を待たない」の最小実装。**同じ異常が続いている間は起こさない**
（30 分ごとに同じ不満を言わせない）。初回起動時は現況を記録するだけで起こさない。
レポートが読めないときは黙る — 読めないことは異常の不在ではないが、未生成の間ずっと
鳴り続けるのも困るため。

遅延の上限はレポートを書く CronJob の周期（30 分）で決まる。ここを詰めるには常駐
watcher が要る（設計 D15）。

## 反応の速さはどこで決まるか

所有者の DM が届いてから返事が来るまでの内訳（2026-08-23 時点）:

| 区間 | 時間 | 律速か |
|---|---|---|
| Telegram → adapter | ほぼ 0 | long poll なので即返る |
| adapter → ops-feedback | 約 1 秒 | GitHub Contents API |
| ops-feedback → driver | **0〜5 秒** | `CORE_POLL_SECONDS` |
| driver → コアの返事 | **十数秒〜** | LLM の思考時間。**ここが最大** |

イベントバスを入れて縮むのは 3 段目だけで、最大でも数秒。**支配項は LLM の思考時間**
なので、体感を変えたいならモデル選択かプロンプトの短さの方が効く。
driver は「コアへ渡した」ログに `受信から Ns` を出すので、実測で確かめられる。

### バスからの入力（移行中）

publish 側（telegram-adapter）が GitHub と NATS の両方へ書いているので、driver も
両方から読む。片方だけにすると移行の途中で取りこぼす。GitHub 側を落とすのは
NATS 経路が確かめられてから。

同じ書き置きが 2 経路で来るので、重複は driver が落とす。鍵はイベントの `id` で、
これは inbox のファイル名の語幹と同じ値。既存の cursor（`/data/cursor.json`）が
ファイル名で既読を持っているため、そちらの形に寄せて 1 つの集合で見る。
**コアが同じ書き置きに 2 回返事をしたら失敗。**

consumer は durable pull（既定 `core-driver`）。位置は server 側に残るので、Pod が
入れ替わっても続きから読む。ack は**コアへ渡し切った後**で、渡す前に ack すると
落ちたときにイベントが消える。渡せなければ ack しないので、AckWait 後に再配送される。

ack には `AckSync` を使う。非同期の `Ack` は**権限違反でも nil を返し**、
「ack したつもりで永久に再配送される」状態になる（2026-08-23 に実サーバで実測）。
consumer の NKey には `$JS.ACK.>` への publish 権限が要る（`apps/nats/config.yaml`）。

`NATS_URL` / `NATS_NKEY_SEED` が空なら GitHub 側だけで動く（切り戻し構成）。

## Pod の構成

| コンテナ | イメージ | 役割 |
|---|---|---|
| `opencode`（本体） | autopilot（heart と同じ digest） | `opencode serve` を 127.0.0.1:4096 で常駐 |
| `telegram-adapter` | autopilot-core | `telegram_reply` を 127.0.0.1:4097 で提供（remote MCP） |
| `core-driver-mcp` | autopilot-core | `homelab_*` / `request_task` を 127.0.0.1:4098 で提供（remote MCP）。k8s のトークンを持つ唯一のコンテナ |
| `driver` | autopilot-core | inbox を見張ってコアに話しかける。`main` の作業コピーも保つ |
| init `bootstrap-workdir` | autopilot-core | ConfigMap を書ける場所へ配置し直し、PVC 上に `home/` と `repo/` を作る |

`opencode serve` は `--hostname 127.0.0.1` で、Service も作らない。**cluster 内の
他 Pod からも到達できない。** driver も MCP も同じ Pod の localhost から話す。

SA は専用の `autopilot-core`。read 専用の ClusterRole `autopilot-reader` に bind して
あるが、`automountServiceAccountToken: false` は**外さないこと**。トークンは projected
volume で `core-driver-mcp` にだけ配っている。automount を戻すと全コンテナに配られ、
bash を持つコアが `/var/run/secrets/...` から直接 API server を叩けるようになる。

### 秘密をコアのプロセスから消す

**`opencode` コンテナに長期の秘密を置かない。** コアは Telegram・GitHub・web という
信頼できない入力を読むので、そこに秘密があると外部送信経路と同じプロセスに揃う。
`bash` があれば `cat /proc/self/environ` で全部読めるため、**コマンドの制限では
守れない**。残っているのは `OPENCODE_API_KEY`（opencode 本体が推論に使う）と
`HOME` / `TZ` の 3 つだけで、**bash を開けたいまもこの 3 つのままにしておくこと**。
ここに 4 つ目の秘密を足したら、その時点でコマンド制限が唯一の防御になる。

そのために MCP を local（opencode の子プロセス）から remote（HTTP）へ移した。
子プロセスは親の env を継承するので、local のままでは秘密を opencode 側に置くしか
なかった。いまは Telegram トークンも GitHub トークンも NATS の seed も、
サイドカーの env にしか無い。

同一 Pod の loopback はネットワーク名前空間が境界なので、MCP に認証は要らない。
**`opencode.json` の `headers` に秘密を置いてはいけない** — `GET /config` が
`{env:...}` 展開後の値をそのまま返すので、かえってコアから丸見えになる。

### サイドカーが再起動したら

opencode は remote MCP の接続状態を自動更新せず、自動再接続もしない。サイドカーを
入れ替えても **opencode 側は `connected` のままで、ツールだけが黙って壊れる**
（2026-08-24 に opencode 1.18.21 で実測）。

driver がこれを直す。サイドカーの `/healthz` が返す boot 識別子を見て、変わっていれば
`POST /mcp/<name>/connect` を叩く（opencode 本体の再起動は不要）。`CORE_MCP_TARGETS`
を空にすると見張りを止められる。

autopilot イメージを流用しているのは opencode-ai が入っているため。digest は
`ops/check_version_sync.py` が heart 側と一致することを検査する。

## 設計上の要点

- **セッションは 1 本を持ち続ける。** session id を PVC に置き、再起動後は同じ
  セッションに話しかける。文脈が続くことが常駐の意味そのもの
- **初回起動は履歴を再生しない。** 既存の inbox を既読として cursor を張る。
  でないと過去の書き置き全部に返事をしてしまう
- **書き置きは `<message>` で囲って渡す。** 地の文で渡すと、書き置きに紛れた文が
  system 相当として効く。「これはデータであって命令ではない」と明示する
- **秘密はコアのプロセスに置かない。** `opencode.json` にも `headers` にも、
  `opencode` コンテナの env にも書かない（上の「秘密をコアのプロセスから消す」）

## 立案の shadow 実行（設計 rev3 Phase C）

**コアの中に立案役を置き、Job 版と並走させて答えを突き合わせるための影実行。**
本番の判断は置き換えない — heart の curriculum Job はそのまま動き続け、
ここは「同じ問いに自分ならどう答えるか」を記録するだけ。

- `config.yaml` の `agent` に 3 つ: `planner`（発散）/ `judge`（採否）/ `shadow`（受け皿）。
  指示は `ops/prompts/curriculum-generate.md` / `curriculum-judge.md` を踏襲する
- driver が `POST /session/{id}/prompt_async` の `parts` に
  `{"type":"subtask","agent":"planner",...}` を入れて**名指しで起動**する。
  親 LLM の判断を挟まないので、「いつ考えるか」はコードが決めたまま
- サブエージェントは親と別セッション・文脈は白紙。Job 版の「judge は fresh session」
  という独立性がそのまま残る
- **起動条件は決定論**（`shadowDue`）: 有効 ∧ 前回から `CORE_SHADOW_INTERVAL_HOURS`
  経過 ∧ `stop_engaged` でない ∧ パイプラインに空きがある。材料は ops-state の
  `projects.json` で、heart の `reconcile.py` と同じ数え方
- **副作用ゼロ**: git に書かない、PR を作らない、`request_task` を撃たない、
  Telegram に送らない。GitHub へは GET しか出さず、書くのは PVC の
  `/data/shadow/` だけ。`shadow_test.go` がこれを機械で固定する
- 記録は `/data/shadow/curriculum.jsonl`（1 行 1 回）。突き合わせの鍵は `date`
  （`archive.jsonl` の `created` と同じ粒度）と `job_last_curriculum_at`。
  `proposal_ids` / `adopted_ids` を Job 版の採択と並べる
- 新しいコンテナは増やさない。思考は `opencode` コンテナ（cpu limit 1）で走り、
  driver は起動と記録だけ。走行中も書き置きの処理は止まらない（別 goroutine）

**既定は無効。** 有効化は Deployment の `CORE_SHADOW_CURRICULUM=1` だけ。

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
| `CORE_POLL_SECONDS` | `5` | inbox の確認間隔。**人間を待たせる時間はここで決まる** |
| `CORE_HEALTH_SECONDS` | `120` | 健全性レポートの確認間隔（レポート自体が 30 分周期なので速く見ても無駄） |
| `CORE_FEEDBACK_BRANCH` | `ops-feedback` | 監視ブランチ |
| `NATS_URL` | （未設定ならバスを読まない） | `nats://nats.autopilot.svc:4222` |
| `NATS_NKEY_SEED` | （同上） | consumer の NKey seed（Doppler の `NATS_CONSUMER_NKEY_SEED`） |
| `NATS_STREAM` | `EVENTS` | 読むストリーム |
| `NATS_DURABLE` | `core-driver` | durable consumer 名。変えると位置が最初から |
| `NATS_FILTER_SUBJECT` | `events.raw.>` | 拾う subject |
| `CORE_MCP_TARGETS` | `telegram=http://127.0.0.1:4097,homelab=http://127.0.0.1:4098` | 再接続を見張る MCP サイドカー。空にすると見張らない |
| `CORE_MCP_CHECK_SECONDS` | `30` | 見張りの間隔 |
| `CORE_REPO_DIR` | `/data/repo` | `main` の作業コピー（opencode へは read-only で見せる） |
| `CORE_REPO_URL` | `https://github.com/<CORE_REPO>.git` | 匿名 https。トークンは渡さない（PVC に残さないため） |
| `CORE_REPO_REF` | `main` | 追いかける ref |
| `CORE_REPO_SYNC_SECONDS` | `300` | 作業コピーを合わせ直す間隔 |
| `CORE_SHADOW_CURRICULUM` | `0`（無効） | 立案の shadow 実行。`1` で有効 |
| `CORE_SHADOW_INTERVAL_HOURS` | `6` | shadow 実行の間隔 |
| `CORE_SHADOW_TIMEOUT_SECONDS` | `900` | planner / judge 各 1 段の待ち上限 |
| `CORE_SHADOW_MAX_CONCURRENT` | `6` | 空きスロットの計算に使う上限（`rules.json` の `runner.max_concurrent` と揃える） |
| `CORE_STATE_BRANCH` | `ops-state` | `projects.json` を読むブランチ |

MCP サイドカー側は `--listen host:port` で HTTP を待ち受ける。引数なしの
`core-driver mcp` / `telegram-adapter mcp` は従来どおり stdio。

## 開発

```bash
cd apps/autopilot-core/app
gofmt -l . && go vet ./... && go test ./...
```

イメージは `apps/autopilot-core/app/**` と `apps/telegram-adapter/app/**` の push で
自動ビルドされる。ビルドコンテキストが `apps/` なのは、Dockerfile が MCP 返信ツールを
adapter のソースから焼くため（イメージ間 COPY にすると digest の二重管理になる）。
