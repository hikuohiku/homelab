# 状態を git から出す — 設計書

作成: 2026-08-25 / 対象: autopilot のライフサイクル

## 一言で

**`main` は定義、クラスタは状態、GitHub はビーコンと台帳だけ。**
いま状態は「main から分岐した 4 本の枝」に載っていて、その枝はアプリ定義の複製を
道連れにしている。状態を器の中へ出し、git には「外から生死を見る最小の面」と
「人間が読む非同期の台帳」だけを残す。

## 何が起きているか（実測 2026-08-25）

### 1. 状態ブランチがアプリ定義を抱えている

`ops-state` は orphan ではなく **main からの分岐**で、木の中に `apps/` `nix/`
`terraform/` の凍結コピーを持つ。4 本すべて同じ。

| ブランチ | commit 数 | main との定義差分 (apps/nix/terraform) | 最終更新 |
|---|---|---|---|
| `ops-state` | 393 | 156 ファイル | 毎ビート |
| `ops-feedback` | 1157 | 149 ファイル | 発話ごと |
| `ops-health-report` | 2314 | 182 ファイル | 10 分ごと |
| `ops-dashboard` | 1555 | 174 ファイル | 2026-08-22 で停止 |

**危険が具体的に出ている**: 4 本すべてが `apps/argocd/values.yaml` に
`memory: 512Mi` を今も載せている。昨日の node01 障害からの復旧で外した、
まさにその行（#598）。修正は main にしかない。機械が毎分更新している 4 本の枝が、
壊れた定義を公開し続けている。

`targetRevision` は全アプリ `HEAD` なので**今は配られていない**。ただし
`just preview <app> <branch>` はブランチを指す口で、そこを踏めば配られる。潜在。

### 2. 状態の履歴が永久に積もる

`metrics.jsonl` は 8.9 MB。1 ビート 1 行 ≒ 8 KB で、中身は
**108 プロジェクト全部の state**。うち 95% は `delivered` / `stalled` / `vetoed` の
終端で、二度と変わらない。同じ配列を毎分 GitHub に commit している。

- 22 KB / beat（実測: 393 beat で pack 8.4 MB）
- 120 s/beat なら **15 MB/日、5.5 GB/年**
- 全履歴 clone に 52 秒（393 beat 時点）

pack は消せない。ブランチを作り直す以外に回収手段が無い。

### 3. ビートが GitHub に直列で依存している

`Heart.beat()` は毎回 `sync_main` → `sync_state_branch` → `commit_and_push_state`。
GitHub が落ちるかトークンが切れると**心臓が止まる**。

同じ構図は書き置き経路で一度否定されている。`apps/nats/config.yaml` に曰く:
「自宅クラスタの内部イベントが外部 SaaS を経由する構造で、GitHub が落ちるか
トークンが切れると所有者の『止めて』が heart に届かない。ここを閉じる」。
NATS + JetStream (PVC 5Gi) はそのために既に立っている。状態経路だけが残っている。

### 4. クラスタ内 → GitHub → クラスタ内 の往復がある

`ops-health-reporter` (CronJob) が `ops-health-report` ブランチに push し、
`ops/heart/facts.py:load_health()` が `git show origin/ops-health-report:...` で読む。
両端とも同じクラスタの中。GitHub を経由する理由が無い。

### 5. main 側にも状態が混ざっている

`ops/state.json` / `ops/backlog.json` は autopilot が直接 main に push する設計で、
CLAUDE.md に「手で編集するときは issue 経由で依頼するか、編集後すぐ push すること」
という但し書きが要る状態。定義のリポジトリに状態が居ることの直接の代償。
（実測: 両ファイルとも 08-07/08-08 以降 autopilot は書いていない。既に死んだ経路の可能性が高く、
Phase 0 で確認して落とす）

## 原則

1. **git は定義だけを持つ。** `rules.json` / `models.json` / prompts / manifests /
   コード。heart はこれを**読むだけ**。`sync_main` は残る。
2. **状態はクラスタが持つ。** 正は k8s API。単一書き手は慣習ではなく RBAC で縛る。
3. **git に出るのは仕事の成果 (ブランチと PR) だけ。** 状態の同期も、生存の通知も、
   健全性の記録も、台帳も置かない。機械が定期的に git を叩く経路を 1 本も残さない。

3 番目は所有者判断 (2026-08-25)。ビーコン (当初案の `ops-beacon`) も台帳
(`archive.jsonl`) も破棄した。前提は「壊れて外部からの復旧が要る事態には自分が
直接対応する」。**履歴が積もるかどうかではなく、機械が git を定期的に叩く構図
そのものを取らない。**

これで `ops-state` の 2 つの罪 — 定期コミットを打つことと、**git から読み戻して
次の判断に使うこと** — が両方消える。後者があると GitHub がライフサイクルの
部品になる。移行後、GitHub が落ちても器は回り続け、止まるのは PR を出す最後の
一手だけになる。

## 移す先

| 今 | 中身 | 移す先 | なぜ |
|---|---|---|---|
| `ops-state:projects.json` | プロジェクトの正 (322 KB) | **CRD `Project`** (`autopilot.homelab.hikuohiku.dev/v1`) | 単一書き手を RBAC で強制。`kubectl get projects` で人にもエージェント (read-only MCP) にも見える。watch でダッシュボードが即時になる |
| `ops-state:trust.json`, `cursors.json` | heart 内部の小状態 | ConfigMap 1 個 | heart 以外読まない。1 KB |
| `ops-state:heartbeat.json` | 生存 | **Lease**（クラスタ内のみ）。GitHub へは出さない | 下の「沈黙をどう気づくか」参照 |
| `ops-state:metrics.jsonl` | ビート毎の全 state (8.9 MB) | **git から廃止**。PVC 上に直近 N 時間だけ | `summarize_beats()` が実際に要るのは直近の窓だけ。終端 95% を毎分永久保存する理由が無い |
| `ops-state:outbox / task-requests / briefing-queue / commands / sent .jsonl` | heart の作業キュー | **PVC のファイル**（形は変えない、commit しないだけ） | heart しか読まない。消えて困るのは「未送信の Discord」程度で、記録ではない |
| `ops-state:audit.jsonl` | 監査 | PVC (直近 N 日) | 人間はダッシュボードで見る。git には出さない |
| `main:ops/projects/archive.jsonl` | 全案の spec・採否・棄却理由 (台帳) | **`Project` CR に吸収**して git から削除 | 下の「台帳を畳む」参照 |
| `ops-health-report` ブランチ | クラスタ内 → クラスタ内 | ConfigMap（reporter が書き、heart が読む） | 往復を切る |
| `ops-feedback` ブランチ | 人間の発話 | **NATS**（D16 で用意済み） | D16 の積み残しを閉じる |
| `ops-dashboard` ブランチ | 旧・静的ダッシュボードの遺物か | 要確認 → 遺物なら削除 | 08-22 で更新停止 |

### `Project` CRD にする理由（ConfigMap でない理由）

- 108 件 × ~3 KB。今は 322 KB で ConfigMap の 1 MB に収まるが、増える一方。
- CR ごとに RBAC が効く。今の「heart が唯一の書き手」は**慣習**で、Job が
  `ops-state` に push するのを止めるものは何も無い。CR なら API が止める。
- `kubectl get projects -o wide` が効く。**今、自分は MCP からプロジェクト状態を
  一切見られない**（git を読む口が無い）。これが直る。
- watch がある。ダッシュボードの 20 秒ポーリング + 毎回 clone が消える。

コントローラは要らない。CRD と RBAC だけ。heart が書き、皆が読む。

### 終端プロジェクトは消さない — live set を selector で切る

台帳が無くなったので、**終端 CR は消せない**。退避先が存在しないうえ、棄却理由は
次の立案が読む唯一の教師信号だから (下記)。

代わりに live set を**問い合わせ側**で切る。`state` をラベルに出し、
`kubectl get projects -l lifecycle=live` が非終端だけを返すようにする。
heart の判断も dashboard の表示もこの selector を通す。

規模の見積り: 現在 108 件 × ~3 KB ≒ 320 KB。増加は月 100 件程度なので年 3〜4 MB。
etcd に対して問題になる量ではない。**毎ビート全件を書き直す今の形をやめる**
(CR は変わったものだけ更新される) ので、書き込み量は桁で減る。

### 沈黙をどう気づくか — 外部ビーコンを作らない

GitHub Actions で走る番人が 2 つある — `.github/workflows/watchdog.yml` から呼ばれる
`ops/check_heartbeat_fresh.py` と `ops/check_health_freshness.py` (30 分ごと)。
どちらも P-0027 の「器の中の口ごと死ぬ」問題への答えで、`ops-state` と
`ops-health-report` を読むために存在している。

**この 2 つは廃止する。** 状態ブランチを畳めば読む先が無くなるうえ、代わりの
ビーコンも置かない (原則 3)。

代わりに、**沈黙を人間へ運ぶ役をコアに移す**。

- heart は生存を **Lease** で示す (クラスタ内。git を触らない)。
- **コアが Lease の鮮度を見て、古ければ Telegram で人間に言う。** コアは既に
  人間と話す口を持っていて (openclaw / telegram-adapter)、heart を叩く経路も
  持っている (`heartgate.go`)。新しい面は増えない。
- **node01 ごと死んだ場合はコアも死ぬ。** そのときは Telegram が応答しなくなる。
  所有者は Telegram で日常的に話しかけているので、**沈黙は使っている経路の上で
  可視になる**。器の外に別の見張りを置く必要はない。

旧構成との違いは、警報が「GitHub Actions → issue #56」から
「コア → Telegram」に移ること。**heart だけが死んでコアが生きている**という
一番ありがちな壊れ方には、旧構成より速く気づける (30 分間隔のポーリングではなく
コアがその場で言う)。

撤去対象: `.github/workflows/watchdog.yml`、`ops/check_heartbeat_fresh.py`、
`ops/check_health_freshness.py`、`ops/tests/test_health_freshness.py`。

## 台帳を畳む — `archive.jsonl` を git から消す

`ops/projects/archive.jsonl` (392 行) は全案の spec・採否・`reject_reason` /
`improve_hint` を持つ台帳で、**いまライフサイクルから読まれている**:

| 読み手 | 何のため | 移行後 |
|---|---|---|
| `facts.py:load_adopted_specs()` | 採択済み spec を全部読む | `Project` CR を読む |
| `runner.py:spec_from_archive()` | spec の読み先 (2) 番目。ops-state に spec を持たない過去案の後方互換 | 移行で不要 (spec は CR にある) |
| `reconcile.py` | **人間が `adopted` 行を足す手動採択の入口** | admission gate へ移す (下記) |
| curriculum の `planner` | 過去案を読み、同型の再提案を避ける | **MCP ツール経由で CR を読む (要実装)** |
| `dashboard:mergeArchive()` | 表示用に title / irreversible を補う | CR から直接引く |

### 棄却された案も CR にする

archive.jsonl の行の多くは **`adopted: false` の棄却案**で、これは `Project` に
なったことがない。だが `reject_reason` / `improve_hint` は
**「判定の教師信号が生成に戻る唯一の経路」** (`runner.py:429`) であり、
落とすと生成役は死因を知らずに同型再提案を繰り返す。

そこで `Project` CR のライフサイクルに `rejected` を足し、**候補の段階から CR に
する**。採択されなかった案は `state: rejected` の CR として残り、
`lifecycle=live` の selector からは外れる。

### planner に読ませる口が要る

planner / judge は `bash: deny` のサブエージェントで、いまはリポジトリの作業コピーを
`read` して archive.jsonl を読んでいる。CR に移ると読めなくなる。

コア側に MCP ツールを 1 つ足す (`homelab_proposals` 相当) — 過去案を
id / title / cell / 採否 / reject_reason / improve_hint に絞って返す。
**これは Phase 4 の必須部品**で、無いまま archive.jsonl を消すと立案の質が落ちる。

### 手動採択の入口を admission gate へ

`reconcile.py` は「人間が `archive.jsonl` に `adopted` 行を足したら動き出す」という
意味論を持っている。これがある限り git はライフサイクルの入力であり続ける。

入口を admission gate (`ops/heart/gate.py`、コアの `dispatch_task` が叩く口) に
一本化する。人間の依頼は Telegram → コア → gate を通り、機械の依頼と同じ経路になる。

代償: **GitHub しか手が届かない状況での手動採択ができなくなる。** ただしその状況では
クラスタが死んでいて Job も走らないので、採択できても何も起きない。実質の損失は無い。

## 未解決 — 移行の前に必ず決める

**耐久性。ここが今回いちばん重い。**

いま git は「オフサイトの複製」を無料で提供している。`ops-state` と
`archive.jsonl` の両方を畳むと、**プロジェクトの全記録の唯一の実体がクラスタに
なる**。node01 の消失 = 器の記憶の全損。git のときは GitHub 側に残った。

外部ビーコンを置かない判断 (原則 3) は「壊れたら自分で直す」で成立するが、
**記録の全損は「直す」の対象にならない** — 直す材料ごと消えるので、性質が違う。

したがって Phase 0 は「確認」ではなく**前提条件**にする:

1. k3s の状態ストアが何か (etcd か kine/sqlite か) を実測する。
2. 今の restic バックアップがそれを掬っているかを実測する。
3. 掬っていなければ、**先にそこを直す**。掬うまで Phase 4 に進まない。

git への日次エクスポートで緩和する案は、原則 3 (機械は git を定期的に叩かない) と
正面から衝突するので採らない。**バックアップはバックアップの仕組みで解く。**

**未確認**: `ops-dashboard` ブランチが遺物かどうか (2026-08-22 で更新停止)。
Phase 0 で書き手を特定する。

### 耐久性 — Phase 0 の実測結果と Phase 0b (2026-08-24)

実測: k3s のデータストアは **kine/sqlite** (`/var/lib/rancher/k3s/server/db/state.db`)。
etcd ではないので `k3s etcd-snapshot` は使えない。既存の restic CronJob 6 本はどれも
アプリの PVC が対象で、**データストアは 1 本も掬っていない**。

**データストアを丸ごと掬う案は採らない。** `state.db` を hostPath で読める Pod は実質
すべての Secret を読めるのと同じで、この 1 個のために作る攻撃面としては大きすぎる。
守るべきはプロジェクトの記録であって、クラスタ全体ではない。

Phase 0b として **`Project` CR だけを書き出す CronJob** を置いた
(`apps/autopilot-projects-backup/`)。read-only の SA で CR を全件取り、
`kubectl apply -f` で戻せる v1 List を決定的に書き出して restic で B2 へ送る。
0 件・前回比の急減では書かずに落ちる (fail-closed)。実装・保持方針・**復元手順**は
[`docs/backup.md`](../../backup.md) の「Project CR の restic バックアップ」。

restic の credential を autopilot namespace に置かないため、専用 namespace に立てた
(削除権限つきの B2 鍵に `autopilot-writer` の Job が手を伸ばせる構図を作らない。
`ops/rules.json` の `allowed_autopilot_doppler_keys` の宣言と同じ壁)。


## 段階

依存の少ない順。各段は単独で戻せる。

| # | やること | 効果 |
|---|---|---|
| 0 | k3s の状態ストアとバックアップを実測し、掬えていなければ直す。`ops/state.json` / `ops/backlog.json` / `ops-dashboard` ブランチが死に経路かを確認 | **Phase 4 の前提条件** |
| 0b | `Project` CR を restic へ書き出す CronJob (`apps/autopilot-projects-backup/`) | 記録がクラスタの外に出る。**4b の前提条件** |
| 1 | `metrics.jsonl` を git から外す（PVC の rolling window へ） | 履歴の増加が止まる。**一番安く一番効く** |
| 2 | `state` をラベルに出し、live set を selector で切る (終端は消さない) | 作業集合が 108 → 5 件規模 |
| 3 | 作業キュー jsonl 群を PVC へ | ビートの push が小さくなる |
| 4 | `Project` CRD 導入 (`rejected` 含む)。planner 用の MCP ツールを足す。heart が二重書き → 読み手 (dashboard / runner / core) を CR へ → `projects.json` と `archive.jsonl` を止める | 正がクラスタへ。MCP から見えるようになる |
| 4.5 | 手動採択の入口を admission gate へ移す | git がライフサイクルの入力でなくなる |
| 5 | `ops-health-report` を ConfigMap 化 | クラスタ内往復を切る |
| 6 | `ops-feedback` を NATS へ寄せて撤去 | D16 を閉じる |
| 7 | 沈黙の通知をコアへ移し、番人 2 つと watchdog.yml を撤去。`ops-state` / `ops-health-report` / `ops-dashboard` / `ops-feedback` を削除 | 機械の定期コミットが 0 になる |

Phase 4 が本丸で、それ以外は本丸を軽くするための地ならし。Phase 1〜3 だけでも
git への churn はほぼ止まる。

## 棄却案

- **外部ビーコン (`ops-beacon` orphan + force-push)。** 履歴は積もらず定義との混入も
  無いが、機械が git を定期的に叩く構図が残る。所有者判断で破棄 (2026-08-25)。
  沈黙の検知はコア → Telegram に移す。
- **台帳 (`archive.jsonl`) を git に残す。** 人間が diff で読めて grep で遡れる利点は
  あるが、同じく git への定期的な書き込みが残り、手動採択の入口として
  git がライフサイクルの入力であり続ける。所有者判断で破棄 (2026-08-25)。
- **`Project` の日次エクスポートを git へ積んで耐久性を担保する。** 原則 3 と正面から
  衝突する。バックアップはバックアップの仕組みで解く。

- **JetStream KV に載せる。** 既に立っていて CAS も watch もある。だが heart は
  意図的に NATS を話さない（サイドカーがファイルに落とし heart はファイルとして読む）。
  そこを崩すうえ、ダッシュボードにも NATS クライアントが要る。**k8s API は
  heart もダッシュボードも既に居る面**で、新しく増やす面が少ない。
- **heart の PVC + HTTP (:8099) だけで配る。** 一番簡単。だが heart が死ぬと
  状態が読めなくなる。ダッシュボードの存在意義の一つは「heart が死んだと見せること」で、
  そこと衝突する。今は git のおかげで heart の死後も最後の状態が見える。
- **orphan ブランチにして git のまま。** 定義との混入は消えるが、GitHub 依存も
  毎ビート 22 KB の履歴も残る。問題の半分しか解けない。
- **全部やめて main に置く。** CLAUDE.md の但し書き（手編集の競合注意）が
  リポジトリ全体に広がるだけ。
