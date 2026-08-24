# コアを脳にする — 立案の内製化と即時 dispatch

[`../event-driven-core/architecture.md`](../event-driven-core/architecture.md) の D1「コアは将来の脳」を
前倒しで実行する設計。D2（起草は Job）と D7（コアは K8s write を持たない）を差し替える。

## 一言で

**判断はコアの常駐セッションに集め、強制は heart のコードに残す。**
立案は Job からコアのサブエージェントへ移し、着手の待ち時間をビート周期ではなく
同期呼び出しにする。

## いま何が遅いか（2026-08-24 実測）

所有者が Telegram で実装を依頼してから runner が動き出すまで:

| 区間 | 実測 | 支配要因 |
|---|---|---|
| Telegram → コア | 2 秒 | バス |
| コア → heart のキューに `pending` | 74 秒 | ビート周期 |
| キュー → 立案 Job の起動 | 42 分待ち | パイプライン満杯 + `min_interval` |
| 立案 Job（spawn→consume 中央値） | 29.8 分 | LLM 思考 + PR + CI + merge |
| 採択 → 予告 → 着手 | 0〜24 時間 | veto 窓 |

**2 秒で耳に届いた依頼が、着手まで最短 1〜2 時間、通常は翌日**になる。
支配項は LLM の思考時間ではなく、**Job 起動・PR・CI・merge・ビート周期という
「人間を待たせる必要のない待ち」**である。

同時に、プロジェクト 91 件のうち **delivered 34 / stalled 53**。詰まりの多くは
採択ゲートと runner の早期エラーで、立案と実装の往復コストが高いことを示す。

## 目標構成

```
                    ┌─────────────────────────────────────┐
  Telegram ──bus──▶ │ core (opencode serve, 常駐 1 セッション) │
  GitHub   ──bus──▶ │   目: repo clone / MCP read / webfetch │
  health   ──bus──▶ │   声: telegram_reply                  │
                    │   手: dispatch_task                   │
                    │   ├─ subagent: planner  (発散)        │
                    │   └─ subagent: judge    (採否)        │
                    └──────────────┬──────────────────────┘
                                   │ dispatch_task (同期・ミリ秒)
                                   ▼
                    ┌─────────────────────────────────────┐
                    │ heart (Go/Python, replicas:1)        │
                    │   admission gate ← 強制はここだけ      │
                    │   状態機械 / 納品ゲート / 単一書き手     │
                    └──────────────┬──────────────────────┘
                                   │ k8s Job
                                   ▼
                              runner Job（実装の手）
```

役割の再配分:

| 仕事 | いま | これから |
|---|---|---|
| 立案（発散） | curriculum Job | コアの `planner` サブエージェント |
| 採否（判定） | curriculum Job の第 2 段 | コアの `judge` サブエージェント（文脈は独立のまま） |
| 立案の起動 | heart がアイドル判定して spawn | コアが随時 + heart が保険で起動 |
| 着手の可否 | heart の reconcile（次ビート） | heart の admission gate（同期） |
| 実装 | runner Job | **変えない**（runner Job のまま） |
| merge / 納品 | heart（CI green + reviewer pass） | **変えない** |
| ops-state への書き込み | heart 単独 | **変えない** |

## 設計判断

### 1. 「直接 dispatch」は直接 Job を作ることではない

コアが k8s Job を自分で作れば、heart が持つ 20 個の不変条件（すべて実際に起きた
事故の対策）を LLM の判断に置き換えることになる。一方で所有者の不満は
**構成ではなく待ち時間**である。両立させる形はひとつしかない。

**heart に同期の admission gate を足し、コアはそれを MCP ツール `dispatch_task` で叩く。**

- 判定は `reconcile` の既存の純関数（`stop_engaged` / breaker / `max_concurrent` /
  capability 宣言）をそのまま呼ぶ。253 件のテストが仕様として効き続ける
- 判定と Job 作成はミリ秒。**人間の待ち時間からビート周期が消える**
- heart は既に ops-state の単一書き手であり、Job 作成の RBAC を持ち、
  `replicas:1 + Recreate` で単一性が k8s レベルで担保されている。新しい信頼点を作らない
- Job 作成だけは非同期。コアには「受理した」を返し、結果をバスで返す

> 2026-08-24（所有者の決定）: dispatch 経路から **verify と採択ゲートを外した**。
> verify を書くのも LLM なのでいくらでも迂回でき、機械の判定として意味を成さない。
> この経路では「頼んだ変更が実際に行われたか」を機械が判定しない。残る機械のゲートは
> CI（壊れていないこと）と soak（健全性が悪化していないこと）、および PR が在ること
> だけで、完成の判断は reviewer とコアの確認に依存する。
> 詳細は [`ops/heart/README.md`](../../../ops/heart/README.md) の
> 「dispatch 経路で失われる保証」。curriculum 由来の採択ゲートは残す。

これは「コアが直接やる」の**文字どおりの実装ではない**。失うものは無く、
得るもの（応答性）は同じである、という判断。

### 2. 立案はサブエージェント 2 段のまま移す

opencode 1.18.21 で実測できたこと:

- サブエージェントは**親と別セッション・文脈は白紙**（`task_id` を渡さない限り）。
  いまの「judge は fresh session で別モデル」という独立性がそのまま保てる
- **親 `bash: deny` / 子 `bash: allow` が成立する**（ルールは配列で後勝ち）
- driver から `POST /session/{id}/prompt_async` の `parts` に
  `{"type":"subtask","agent":"planner",...}` を入れると、**親 LLM の判断を介さず**
  立案役を起動できる（実装は `bypassAgentCheck` で許諾チェックを飛ばす）

したがって「アイドルになったら必ず立案する」という決定論的な起動は維持できる。
LLM が担うのは思考だけで、**いつ考えるかは引き続きコードが決める**。

消えるもの: Job 起動 + clone のオーバーヘッド、`/work` の emptyDir に proposals を
置いたまま judge が死んで発散結果を道連れにする失敗様式（P-0227）。

### 3. 立案結果を main の PR 経由で流さない

いまは採択 spec が `main` の `archive.jsonl` に PR → CI → merge されて初めて動き出す。
今日はここが 6.5 時間かかった。

**dispatch の正を ops-state の `projects.json` に移す。** `archive.jsonl` は
非同期・バッチの記録として残す（人間が読む台帳）。

改竄耐性は落ちない — むしろ上がる。`main` は CI を通る PR なら誰でも書けるが、
`ops-state` は heart しか書けない。要変更: `runner.load_spec()` の読み先。

### 4. コアに何を持たせるか

「何でも見えて操作できる」を 3 段に分ける。

| | 内容 | 判断 |
|---|---|---|
| 見る | repo の作業コピー、read-only bash、MCP（k8s/ArgoCD/ログ）、webfetch | **全部渡す** |
| 作る | ブランチ・commit・PR、`dispatch_task` | **渡す**（main 直 push は不可のまま） |
| 適用する | `kubectl apply` 等のクラスタ変更、PR の merge | **渡さない** |

適用を渡さない理由は不信ではない。**merge の条件（CI green ∧ reviewer pass）は
機械のゲートであり、LLM の自己申告より強い**からで、クラスタ変更を Git 経由に
縛るのは CHARTER §5 の根幹である。ここを緩めるなら別途の意思決定が要る。

### 5. bash を開けるなら、まず秘密をコアの環境から出す

いまのコアは `TELEGRAM_BOT_TOKEN` / `AUTOPILOT_GITHUB_TOKEN` / `NATS_NKEY_SEED` を
環境変数に持ち、MCP の子プロセスがそれを継承する設計になっている。
**bash を開けた瞬間、`env` 一発でこれらが読める。**
コアは Telegram・GitHub・web という信頼できない入力を読むので、これは
「秘密 + 信頼できない入力 + 外部送信経路」が揃った状態になる。

対策（bash を開ける前提条件とする）:

- MCP を**子プロセスから別コンテナへ**移す。秘密はそのコンテナの環境にだけ置き、
  opencode のコンテナには `OPENCODE_API_KEY` だけを残す
- `serviceAccountName` を専用のものにし、`automountServiceAccountToken: false` を明示する
  （現状どちらも未指定 = `default` SA のトークンが自動マウントされている）
- bash は allowlist で開ける。ブランケット allow にしない

### 6. `ask` を一つも残さない

opencode の permission は**マッチするルールが無いときの既定が `ask`**（実測）。
headless のコアが `ask` を踏むと応答できる者が居ない。CHARTER §5.1 が
「`ask` を新設しない、止めたいものは `deny` にする」と定めた事故（run #1）と同型である。
サブエージェントが使うツールは**すべて明示的に allow か deny で書き切る**。

## 失われるものと、その置き場所

heart の不変条件を、この構成のどこが持つか。

| 不変条件 | いまの持ち主 | これからの持ち主 |
|---|---|---|
| 採択ゲート（着手前に verify 全 fail） | heart（次ビート） | **dispatch 経路では廃止**（2026-08-24 の所有者判断）。curriculum 由来は heart に残す |
| `max_concurrent` = 6 | heart | admission gate（同期で拒否） |
| breaker $100/日 | heart（runner transcript 集計） | **廃止**（D36）。定額移行で名目コストが意味を失った |
| トークンの soft cap | runner（`budget_exhausted`） | **廃止**（D36）。無限ループは回数と無活動で止める |
| `stop_engaged`（「止めて」の永続化） | heart | 変えない。admission gate も同じフラグを見る |
| veto 窓（不可逆は常に 24h） | heart | 変えない。`proposed_by: human-request` は窓 0 を明文化 |
| SA の宣言連鎖（spec→予告→Job） | heart | 変えない。`dispatch_task` も同じ経路を通す |
| 二重 spawn 防止 | heart（3 重） | admission gate（決定論的 Job 名 + 409 冪等） |
| 監査（audit.jsonl） | heart | 変えない。dispatch 元（core/heart）を記録に足す |
| コアが死んでも自走する | heart | **変えない**。curriculum Job の経路を冷スペアとして残す |

## 段階導入

| Phase | 内容 | 完了条件 |
|---|---|---|
| A | 秘密の分離（MCP を別コンテナへ）、専用 SA、automount 停止 | コアの env に長期秘密が無い |
| B | コアに目を渡す（repo clone、bash、k8s/ArgoCD の read MCP） | コアが自分で状況を調べて答える |
| C | `planner` / `judge` サブエージェントを shadow で走らせる | Job 版と採択結果を突き合わせて差分を検分 |
| D | heart に admission gate、`dispatch_task` を active 化 | 依頼 → 着手が分オーダー |
| E | 立案の正を ops-state へ、curriculum Job を冷スペアに降格 | PR/CI が人間の待ち時間から外れる |

## 未検証・前提

- ~~opencode が remote/HTTP MCP を受けられるか~~ → **実測で確定（2026-08-24）。**
  `opencode-ai@1.18.21` は `{"type":"remote","url":...}` を受け、HTTP streamable
  （失敗時 SSE フォールバック）で話す。localhost の別コンテナに繋がる。
  remote MCP が死んでいても `opencode serve` は起動し、そのサーバのツールだけが消える。
  実装上の注意は 3 点:
  - **`"oauth": false` を明示する。** 未指定だと OAuth 自動検出が走り、401 を
    `needs_auth` として扱う経路に入る
  - **接続状態は自動更新されず、自動再接続も無い。** サイドカー再起動後に
    `POST /mcp/<name>/connect` を叩く仕掛けが要る。入れないとツールが黙って壊れたままになる
  - **`timeout` の既定は 30000ms**（ドキュメントの 5000ms は誤り。実バイナリを信じる）
- コンテキスト上限到達時の実挙動と `compaction.auto` の既定値
- `subagent_depth` の既定は 1。孫を呼ばせるなら明示設定が要る
- 子セッションのトークンは親に合算されない（`GET /session/{parent}/children` が要る）。
  D36 で強制はしなくなったので、**表示のためだけに**必要かどうかを決める
- 常駐セッションが誤った思い込みを抱えたまま長期化するリスク。日次で
  セッションを切り直し、状態は記憶でなくツールで読み直す運用を併せて決める

## 決定記録

### 2026-08-24 rev3

- **D28. D2 を差し替える**: 「選定はコア、起草は Job」→ **起草もコアのサブエージェント**。
  Job は実装（runner）にだけ残す。理由は起草の待ち時間が Job 起動・PR・CI に
  支配されており、思考時間ではないため。
- **D29. D7 を差し替える**: 「コアは K8s write を持たない」→ **コアは dispatch を
  同期で要求できる**。ただし Job を作るのは heart のままで、コアに k8s の
  write 権限は渡さない。強制点を増やさない。
- **D30. D3 は維持**: コアは git に書かない（ブランチ・PR は作れるが、
  ops-state への commit は heart 単独）。
- **D31. 実装は Job のまま**: runner をサブエージェント化しない。中央値 21 分・
  最長 8 時間の作業を常駐セッションに載せると、Pod の生存と作業の生存が結合する。
- **D32. dispatch の正は ops-state**: 採択 spec の読み先を main の archive.jsonl から
  ops-state の projects.json へ移す。archive.jsonl は非同期の台帳として残す。
- **D33. bash の前に秘密を出す**: MCP を別コンテナに移し、専用 SA を与え、
  automount を止めるまで bash を開けない。
- **D34. `ask` 禁止**: サブエージェントの permission は使用ツールを全列挙する。
- **D35. 冷スペアを残す**: curriculum Job の経路は消さない。コアが N 分応答しない
  ときは heart が従来どおり Job で立案する。
- **D38. 分離が縮めるのは境界であって、消すのではない（2026-08-24 の実測を受けて）**:
  秘密をサイドカーへ移すと core の env からトークンは消えるが、**bash を持つ core は
  `127.0.0.1` のサイドカーへ直接 JSON-RPC を投げ、公開されたツールを何でも呼べる。**
  境界は「生のトークンで API を叩き放題」から「サイドカーが公開したツールの範囲内」へ
  **縮む**だけで、外部送信経路が消えるわけではない。したがって次の論点は
  **サイドカー側のツール設計**になる — 宛先を所有者に固定する（`telegram_reply` は
  既にそうなっている）、レート制限、スコープの最小化。
  - 系として: **MCP の `headers` に秘密を置かない。** `GET /config` は `{env:...}` 展開
    **後**の値をそのまま返すので、bash のある core からは丸見えになる。同一 Pod の
    localhost ならネットワーク名前空間が境界なので、そもそもトークンは要らない。
  - 系として: `{env:VAR}` は**未定義でも空文字になりエラーにならない**。渡し漏れが
    黙って通るので、渡す側で存在検査をする。
- **D37. 秘密の分離を「目を渡す」より先にする（rev3 の自己訂正）**: 当初 Phase A を
  「read-only bash を含む目の付与」、Phase B を「秘密の分離」としていたが、順序が誤り。
  **read-only の bash でも `cat /proc/self/environ` で環境変数は読める。**
  「読み取り専用のコマンドしか許さない」は秘密の防御にならない。防御になるのは
  「そのプロセスに秘密が無いこと」だけなので、分離を先に置く。
- **D36. 予算方式を廃止する（所有者の決定）**: opencode Go の定額へ完全移行したので、
  金額・トークンで仕事を止めるのをやめる。**計測は残し、強制をやめる。**
  - 廃止: breaker（$100/日）、トークンの soft cap、`budget_exhausted`
  - 残す: 通知の日次上限 6 件（金銭ではなく**人間の注意力**の予算）、
    `max_sessions_per_project`（無限ループの最後の歯止め）、並列上限、
    無活動 kill、連続エラー・crash loop のカウンタ
  - 根拠: stalled 53 件のうち `budget_exhausted` が 15 件で、`error` と並ぶ最多の
    詰まり要因になっていた。`rules.json` 自身が「名目コストで実請求と一致しない」と
    認めていた
  - **帰結**: 上限待ち（`waiting_quota`）が異常ではなく**通常の運転状態**になる。
    `QUOTA_WAIT_MAX_ROUNDS = 6`（ラウンド数はビート周期に依存し意味を持たない）を
    連続 24 時間の壁時計に置き換える。これをやらないと廃止が大量 stall を生む。
