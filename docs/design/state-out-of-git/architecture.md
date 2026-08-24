# 状態を git から出す — 設計書 / 実施記録

作成: 2026-08-25 / 最終更新: 2026-08-25 / 対象: autopilot のライフサイクル

## この文書の読み方

**この文書は `main` にあり、autopilot が読んで自分で実装 PR を出す。** だから
「これからやること」と「もう済んだこと」を混ぜない。

- 済んだ段は「段階」の表で **完了 + PR 番号**として畳む。残したままにすると
  同じ段をもう一度実装してくる (#645 が実際にそうなった)。
- 採らなかった案は「棄却案」に**理由ごと**書く。設計の途中に残った書きぶりを
  そのまま実装してきたことがある (#622 — 既に棄却していた k3s データストアの
  hostPath バックアップ)。
- 「何が起きているか」以降は**移行前の実測**で、当時の記録として残してある。
  現況は「段階」の表と各段の「進捗」節を見る。

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
**これらはまだ `main` にある** — 全ジョブが `if: false` で止まっているだけ。ファイルの
撤去は #636 (Phase 7c) にあるが、#635 の上に積んであるので main には未着。

### 実施 (Phase 7a, #632 / #642) — 実際の障害で検知した

コアの `silence.go` が見張る。設計と違えた点が 1 つ — 見るのは Lease だけではなく
**2 つ**にした。

1. heart の Lease の `renewTime` (ビートが最後まで通ったときだけ進む)
2. 健全性レポート ConfigMap の `generated_at` (reporter が実際に書いたときだけ進む)

(2) は Phase 5 で読み先が枝から ConfigMap へ移ったあと、旧 `check_health_freshness.py`
の役が宙に浮いていた穴。閾値は `ops/rules.json` の `health.stale_seconds` /
`heartbeat.stale_seconds`。どちらも「仕事の成果が更新された時刻」で、返事ができることでは
進まない — P-0027 の事故は「プロセスは生きているのにループが回っていない」形だったので、
`/healthz` が 200 を返すことを見る実装ではその事故を再現する。読めない・壊れている・
時刻が無いは沈黙とみなす (fail-closed)。

**仕込んだ試験ではなく、実際の障害で動いた。** heart が Lease を RFC3339 で書いていて
API が毎ビート 500 を返し、生存が一度も見えていなかった (#649 で MicroTime に修正)。
その間コアは沈黙として検知し、修正後に回復も検知している (本番ログ):

```
23:05:39 沈黙をコアへ渡した (沈黙の顔ぶれが変わった): (なし) → heart
23:36:32 沈黙をコアへ渡した (沈黙が解消した): heart → (なし)
```

**未確認**: 検知から先、**Telegram に実際に届いたかは見ていない**。検知とコアへの
受け渡しまでが確かめられた範囲。

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

### 進捗 — 4b-2a (2026-08-24): 読み手を CR へ

**読み手だけ**を Project CR に切り替えた。書き込みは `projects.json` /
`archive.jsonl` にも残っているので、git 側は正しい写しのまま戻せる。
書き込みを止めるのは次の段 (4b-2b)。

| 読み手 | 今 |
|---|---|
| `facts.load_adopted_specs()` | CR (`state!=rejected`) |
| `reconcile` の採択登録 | 同上。CR は doc の写しなので通常は no-op で、効くのは復元後の埋め直し |
| `runner.load_spec()` | Job の env `HEART_SPEC_JSON` だけ。worker はトークン automount 無しで CR を読めず、そこを開けるのは決定 #5 の境界を崩す |
| dashboard | CR (`state!=rejected`)。`heartbeat` と `stop_engaged` はまだ ops-state (Phase 7) |
| コアの shadow | 非終端の CR (`lifecycle=live`) + heart の `/healthz` (`stop_engaged` / `last_curriculum_at` は CR に載らない doc 全体の値) |
| curriculum Job | heart が spawn 時に CR から `/data/curriculum/proposals.jsonl` へ書き出す (`PROPOSALS_HISTORY`)。**棄却案を読む唯一の読み手** |

**CR が読めないときの挙動**: 採択登録は空で進む (ビートは止めない。次のビートが
やり直す)。curriculum は **spawn しない** — 死因を知らない立案は同型再提案を採択まで
通すので、走らせない方が安い。ダッシュボードは直近の写しを警告つきで出す
(黙って 0 件を出さない)。

**手動採択の入口は塞がった**。`archive.jsonl` に `adopted` 行を足しても動き出さない。
admission gate への移設は下記 Phase 4.5 のまま。

### 進捗 — 4b-2b (2026-08-25): 書き込みを止めた

**ビートは git に 1 度も書かなくなった。** `ops-state` への push を打つ関数
(`commit_and_push_state` / `sync_state_branch`) と `Gh.ensure_branch` はコードごと
消えている。プロジェクトの正は Project CR で、外に出るのは restic のバックアップ
(Phase 0b) だけになった。

| 何 | 前 | 今 |
|---|---|---|
| `projects.json` | ops-state (毎ビート push) | PVC の `state/` (ビートの作業用の写し。正は CR) |
| `heartbeat.json` | ops-state | PVC。読み手は livenessProbe だけ |
| `metrics.jsonl` (最新 1 行) | ops-state | **廃止** (経過措置の読み手が居なくなった) |
| `archive.jsonl` への追記 | curriculum Job が PR | **廃止**。棄却案は result.json → PVC の台帳 → Project CR |
| ダッシュボードの `heartbeat` | ops-state を毎回 clone | Lease (`coordination.k8s.io/autopilot-heart`) |
| ダッシュボードの `stop_engaged` / 使用量 | ops-state の doc | heart の `/healthz` |
| 外部 watchdog (GitHub Actions) | 30 分ごとに ops-state を読む | 止めた (誤報しか出ない)。撤去は Phase 7c |

**止める前の守り**: `projects.json` を PVC へ移す前に、heart が
「移行前の doc のプロジェクト id + 台帳の全 id」が**すべて CR に在ること**を
突き合わせる (`Heart.cr_gap`)。1 件でも欠けていれば移行しない — 移した瞬間、CR に
ならなかったものは restic のバックアップにも乗らず静かに消えるため。欠けている間は
従来どおり移行前の doc で回り (push はしない)、毎ビートの `sync_project_crs` /
`plan_rejected` が穴を埋めるので自力で収束する。人間には incident で言う。
CR を読めなかったビートも「揃っている」に倒さない (fail-closed)。

**PVC ごと失った場合**: `load_doc()` が Project CR から doc を組み直す。
空の doc で走り出さない (CR が読めなければ例外を上げて次のビートに任せる) —
空の doc は「全プロジェクトを忘れた」と同義だから。

**`ops-state` ブランチと `archive.jsonl` は消していない。** 中身はそのまま残り、
戻せる状態を保っている。削除は所有者の判断を待つ (別 PR)。

**人間が手で採択する手段は Telegram → コア → admission gate だけ**になった。
`archive.jsonl` に `adopted` 行を足しても何も起きない (4b-2a で入口は塞がっている)。

### 手動採択の入口を admission gate へ

`reconcile.py` は「人間が `archive.jsonl` に `adopted` 行を足したら動き出す」という
意味論を持っている。これがある限り git はライフサイクルの入力であり続ける。

入口を admission gate (`ops/heart/gate.py`、コアの `dispatch_task` が叩く口) に
一本化する。人間の依頼は Telegram → コア → gate を通り、機械の依頼と同じ経路になる。

代償: **GitHub しか手が届かない状況での手動採択ができなくなる。** ただしその状況では
クラスタが死んでいて Job も走らないので、採択できても何も起きない。実質の損失は無い。

**現況 (2026-08-25): 未実装。ただし旧入口は 4b-2a で既に塞がっている。**
`facts.load_adopted_specs()` の読み先が `archive.jsonl` から CR に移った時点で、
`adopted` 行を足しても何も起きなくなった。いま人間が手で採択する手段は
**Telegram → コア → admission gate だけ**。残っているのは `reconcile.py` の意味論を
gate に一本化して経路に名前を持たせる整理で、塞ぐという目的自体は果たされている。

## 耐久性 — 移行の前に決めたこと

**耐久性。ここが今回いちばん重い。** (決着済み。結論は下の Phase 0 / 0b)

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

`ops-dashboard` ブランチは**遺物**と確定した。書き手の `ops/dashboard/build.py` は
Mission Control (`apps/ops-dashboard/`) の稼働後に退役していて、リポジトリに存在しない
(`ops/CHARTER.md` §7.1 の手順だけが残骸として残っている)。

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

設計時は 0〜7 の 8 段だったが、実装では**依存の切れ目で細かく割った** (1 段 1 PR、
それぞれ単独で戻せる形を保つため)。Phase 2 は 4a に吸収され、Phase 4 は 4 つに割れた。

| 段 | やったこと | PR | 状態 |
|---|---|---|---|
| 0 | k3s のデータストアを実測 (kine/sqlite。restic 6 本はどれも掬っていない) | — | 完了 |
| 0b | `Project` CR だけを restic で B2 へ (`apps/autopilot-projects-backup/`) | #624 | 完了 |
| 1 | `metrics.jsonl` を git から外す (PVC の rolling window)。1 行 8 KB → 300 B | #610 | 完了 |
| 3 | 作業キュー jsonl 8 ファイルを PVC へ | #613 | 完了 |
| 5 | 健全性レポートを `ops-health-report` ブランチから ConfigMap へ | #617 / #619 | 完了 |
| 4a | `Project` CRD を入れ、heart が CR にも書く (二重書き)。`lifecycle` ラベル = 旧 Phase 2 | #621 | 完了 |
| — | (計画外) 機械が打つ clone を blobless に。65s/124MB → 2.1s/9.2MB | #626 | 完了 |
| 4b-1 | `rejected` state。棄却案を CR に入れ、立案役が `homelab_proposals` MCP で読めるようにする | #629 / #631 | 完了 |
| 6a | ダッシュボードの書き置きを NATS へも publish (両書き) | #627 / #633 | 完了 |
| 4b-2a | 読み手 (facts / reconcile / dashboard / コア / curriculum) を全部 CR へ | #639 / #641 | 完了 |
| 7a | 沈黙の検知をコアへ移す (Lease + 健全性 ConfigMap の鮮度 → Telegram) | #632 / #642 | 完了 |
| 4b-2b | **git への書き込みを止めた。** `projects.json` を PVC へ、心拍を Lease へ | #647 / #648 | 完了 |
| — | Lease の `renewTime` を MicroTime 形式に直す (毎ビート 500 で生存が見えていなかった) | #649 | 完了 |
| 4.5 | 手動採択の入口を admission gate へ | — | **未実装** (旧入口は 4b-2a で閉塞済み。上記) |
| 6b | 書き置きの `ops-feedback` 経路を落とす | #628 | 完了 (実機の疎通は未確認。下記) |
| 7b | `ops-feedback` に触る口を全部閉じる (telegram-adapter / コア) | #635 | **保留** (下記) |
| 7c | 外部 watchdog の撤去 (`watchdog.yml` は 4b-2b で `if: false` 済み) | #636 | #635 の上に積んである。**main には未着** |
| 7d | 4 本のブランチを削除 | — | 未着。所有者の判断 |

Phase 4 が本丸で、それ以外は本丸を軽くするための地ならし。実際、Phase 1・3・5 が
入った時点で git への churn の大半は止まっていた。

### 6b / 7b で残っている確認

実装は入ったが、**新しい NATS 経路は実機で一度も通っていない**。ダッシュボードは
NATS へ遅延接続するので、書き置きを 1 件投げるまで接続の成否が分からない。

**確認手順**: ダッシュボードから書き置きを 1 件投げ、heart のログに `feedback received:`
が出ること。ここが通らないと、所有者の「止めて」が届かない。届かないこと自体は
heart の `feedback_bus_alert` が incident で鳴らす (黙って落とさない)。

### やり残し

- **4 本のブランチ (`ops-state` / `ops-feedback` / `ops-health-report` /
  `ops-dashboard`) は消していない。** 書き込みを止めただけで、中身は戻せる状態に
  残してある。削除は不可逆なので所有者の判断。
- **`apps/version-watcher` がまだ `ops-health-report` ブランチに push している。**
  機械が git に定期コミットを打つ経路として**最後に残っている 1 本**
  (`watch.py` の `REPORT_BRANCH`)。しかも watcher は `ensure_branch` で枝を作り直すので、
  ここを畳まずに枝を消すと翌晩に生え直る。**ブランチ削除の前提条件**。
- **Phase 4.5 (手動採択の入口)** — 上記。
- **未確認**: `ops/state.json` / `ops/backlog.json` を autopilot が今も main へ push
  しているか (「何が起きているか §5」)。リポジトリ内で書いている口は見つからず、
  `apps/autopilot/loop.sh` が `backlog.json` を読むだけ。実機で確かめていない。

## 棄却案

- **外部ビーコン (`ops-beacon` orphan + force-push)。** 履歴は積もらず定義との混入も
  無いが、機械が git を定期的に叩く構図が残る。所有者判断で破棄 (2026-08-25)。
  沈黙の検知はコア → Telegram に移す。
- **台帳 (`archive.jsonl`) を git に残す。** 人間が diff で読めて grep で遡れる利点は
  あるが、同じく git への定期的な書き込みが残り、手動採択の入口として
  git がライフサイクルの入力であり続ける。所有者判断で破棄 (2026-08-25)。
- **`Project` の日次エクスポートを git へ積んで耐久性を担保する。** 原則 3 と正面から
  衝突する。バックアップはバックアップの仕組みで解く。
- **k3s のデータストア (`state.db`) を丸ごと restic へ (PR #622、2026-08-24 に close)。**
  実装自体は正確だった (SQLite Online Backup API で一貫スナップショット、hostPath は
  readOnly の 2 本、`token` が無いと戻せないことも明記) が、**攻撃面**で採らない。
  kine の中身は etcd の中身であり、`state.db` を読める Pod は全 Secret を読めるのと同じ。
  守りたいのはプロジェクトの記録であってクラスタ全体ではない。範囲を絞った #624 が採択。
  この案が生まれたのは Phase 0 の書きぶりが「k3s の状態ストアを掬う」と読めたためで、
  そこは実測結果に差し替えてある (上記「耐久性」節)。

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
