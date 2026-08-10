# P-0045 — 器が自分の詰まりと利用者の不満を、人間より先に見つける (critic の常設配線)

## 目的

人間の指摘 (2026-08-10)「ダッシュボードのゴミも、アイドルの空費も、自分で見つけてほしかった」。
critic はプロンプト (`ops/prompts/critic.md`) も Job モード (`ops/runner/runner.py` の `mode_oneshot`)
も spawn の受け皿 (`ops/heart/spawn.py` の `kind="critic"`) も実装済みなのに、**heart から呼ぶ配線だけが
無く一度も走っていない**。指標 (状態別滞留時間・アイドル率) と利用者面の定期検分があれば器が先に
気づけた種類の問題なので、日次の自己観測器官として常設する。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-10、`project/p-0045` の checkout で、リポジトリルートから実行)。

- [ ] `grep -q '_critic_due' ops/heart/reconcile.py`
  — critic の日次 spawn 判定が**純関数側 (reconcile.py) にある**ことの番人。判断を heart.py の
    I/O 側に書くと遷移表テストで固定できない。実測 rc=1 (ファイルはあるが `critic` の語が 1 つも無い)。
- [ ] `grep -q '/data/critic' ops/prompts/curriculum-generate.md`
  — critic の所見が**次の立案の原料として自動で流れ込む**ことの番人。DoD (3) 後半。
    実測 rc=1 (現行の「読むもの」は VISION / archive / seeds / memory / journal / inventory / repo の 7 項目)。
- [ ] `grep -rq 'critic_due\|spawn_critic' ops/heart/tests/`
  — 遷移表テストが追加されていることの番人。CI の ops job が
    `python3 -m unittest discover -s ops/heart/tests -t .` を実行する (`.github/workflows/ci.yml:151`)。
    実測 rc=1 (`test_reconcile.py` / `test_metrics.py` / `test_adoptgate.py` / `test_statefiles.py` /
    `test_triage.py` のどれにも該当語が無い)。

**この 3 項目は DoD の下限であって、DoD そのものではない。** 3 つとも grep なので、文字列だけ置けば
通ってしまう。とくに DoD (3) の Discord 出口と DoD (4) の初回実測は verify が一切見張っていないので、
**PROGRESS.md に実測の証拠 (実際に生成された `/data/critic/2026-08-10.md` の中身・送信結果) を
貼ること**で担保する (`/data` は Git 管理外なので、貼らなければ存在しなかったことになる)。

## 設計方針

### 前提 (initializer が実測・実読して分かったこと。調べ直さなくてよい)

- **`/data` (PVC `autopilot-data`) はこの worker Job にもマウントされている。** 実在するのは
  `/data/ops-state/` (heart が push している ops-state ブランチの作業ツリー)、`/data/projects/<ID>/`、
  `/data/transcripts/{worker,review,curriculum}/`。`/data/critic/` は**まだ無い** (作るところから)。
  `/data` は `drwxrwsrwx root:autopilot` なので uid 10001 で作成できる。
- **指標の実体は `/data/ops-state/metrics.jsonl`** (1 行 1 ビート、2026-08-07T17:52Z から 2386 行)。
  1 行に `at` / `beat` / `projects` (id → state のマップ) / `actions` (その回に実行した action 種別の配列) /
  `breaker` / `unhealthy_apps` が入っている。**`beat` は heart 再起動でリセットされる**
  (今日の先頭は beat 2)。滞留時間は必ず `at` の差分で数えること。ビート間隔も一定ではない
  (今日の実測: 中央値 65s、最大 78s)。
- **今日 (2026-08-10) の実データを initializer が集計した結果** — DoD (4) の「所見が最低 1 件出る」は
  実データ側では既に満たせる:
  - 433 ビート中 **408 ビート (94%) が `actions` 空 = アイドル**
  - 状態別の滞留 (ビート数): `announced` 534 / `active` 112 / `merging` 211 / `in_review` 29 /
    `soaking` 57 — **拒否権窓での待ち (`announced`) が実作業 (`active`) の約 5 倍**
  - `stalled` 1732 (延べ)、`delivered` 3248 (延べ)
  - 今日の action 内訳: `spawn_runner` 7 / `consume_result` 5 / `spawn_reviewer` 5 / `consume_review` 5 /
    `merge_pr` 4 / `deliver` 4 / `announce` 4 / `run_adopt_gate` 4 / `spawn_curriculum` 1 / `consume_curriculum` 1
  - **`stalled_reason` は metrics.jsonl に入っていない。** stalled の内訳は
    `/data/ops-state/projects.json` の各 project の `stalled_reason` から数える。
- 既存の「日次/間隔をあけて 1 つだけ spawn する」の型は **curriculum がそのまま手本**
  (`reconcile.py` 末尾の `last_curriculum_at` / `gap_ok` / `spawn_curriculum` と、`heart.py` の
  `elif kind == "spawn_curriculum"`)。**spawn した時刻で刻む** (完了時刻ではない) ので二重 spawn しない。
- 待ち時間の閾値は `reconcile.py` のモジュール定数に置く流儀 (`REVIEW_TIMEOUT_HOURS` 等)。
  **`ops/rules.json` は触らない** — 人間レビュー必須パスの単一情報源であり、この程度の定数のために
  開けない (`ops/runner/runner.py` 冒頭に同じ判断の先例がある)。
- heart は `ops/heart` の tree hash が main で変わると自分を `execv` し直す (`self_update_check`)。
  **merge した時点で配線が生きる**ので、別途の再起動依頼は要らない。

### 決めてあること (この方針で作る。変えるなら理由を PROGRESS.md に書く)

1. **判定は純関数。** `reconcile.py` に `CRITIC_INTERVAL_HOURS = 24` と `_critic_due(doc, now)` を置き、
   `decide()` の末尾 (curriculum の隣) で `spawn_critic` action を出す。状態は projects doc の
   トップレベルに持つ: `last_critic_at` (spawn した時刻で更新) と `last_activity_at`
   (**`actions` が空でないビートで更新**。ただし `spawn_critic` 自身は活動に数えない — 自分で自分の
   条件を成立させ続けるのを防ぐ)。due の条件は「`last_critic_at` から 24h 経過」かつ
   「`last_activity_at > last_critic_at`」。`last_critic_at` が無い初回は活動が 1 度でも記録され次第 due。
   `breaker` / `stop_all` 中は spawn しない (冒頭の不変条件「breaker 中は新しい仕事を作らない」)。
   `max_concurrent` は見ない (critic は runner スロットを消費しない別 Job)。
2. **PROJECT_ID を `system` にしない。** `spawn.create(..., kind="critic")` は既定で `pid="system"` になり、
   runner が `/data/projects/system/result.json` を書く。そこは **curriculum の結果置き場**で、
   `facts.collect_curriculum()` がそれを読む。critic が `error` で終わると
   「curriculum Job がエラー終了」の incident 通知が飛び `last_curriculum_dry` まで書き換わる。
   `spawn.build_job()/create()` に `project_id=` を足す (または `{"id": "critic"}` を渡す) ことで
   `/data/projects/critic/` に隔離し、heart 側で `consume_critic` (result.json の退避) まで用意する。
   退避しないと同じ結果を毎ビート再消費して通知が発振する。Job 名は curriculum と同じく
   `attempt=int(time.time()) // 60 % 1000000` で一意にする (TTL 6h の残骸に 409 で黙って負けないため)。
3. **入力の絞り込みは heart (決定論側) がやる。** critic.md の冒頭が宣言している分業
   (「候補区間の特定は指標側がやり、ここは絞られた対象だけを読む」) に従う。
   `ops/heart/metrics.py` に純関数 `summarize_beats(records, now, window_hours=24)` を足し、
   状態別滞留秒 / アイドルビート率 / `announced` の窓ブロック時間 / (projects.json 由来の)
   stalled 内訳を返す。heart はこれを `/data/critic/input-YYYY-MM-DD.json` に書き、Job には
   `CRITIC_INPUT` (そのパス) と `CRITIC_TARGETS` (直近 transcript のパス 1〜3 件) を `extra_env` で渡す。
   集計は純関数なので `ops/heart/tests/test_metrics.py` で固定する。
4. **出口は `/data/critic/YYYY-MM-DD.md` に統一する。** 現行 `critic.md` は
   `/data/critic/<日付>-findings.json` と書いてあり spec とずれている。**spec (.md) に寄せて
   critic.md を直す** (構造化したい所見は md の中に JSON ブロックで埋める)。critic.md には併せて
   (a) `CRITIC_INPUT` / `CRITIC_TARGETS` の読み方、(b) ダッシュボードを**利用者レンズで**検分すること、
   (c) transcript を最低 1 本は精読すること、を書く。
5. **Discord に本当に出るようにする。** `notify.py` の `IMMEDIATE_TYPES` は
   `("announce","deliver","question","incident","review")` で、**`ntype="notify"` は必ず outbox の
   digest 行きになり Discord に出ない** (24h 後に issue #56 へ代送されるだけ)。所見の要点を流すには
   `IMMEDIATE_TYPES` に `critic` を足し、`ntype="critic"` で送る。日次予算 (`rules.notify.daily_budget` = 6)
   の枠は自動で効くので spec の「1 日の予算内」を満たす。本文は `_post_discord` が 1900 字で切るので、
   md の冒頭の要点だけを載せる。
6. **DoD (4) は本当に走らせて確かめる。** まず素直に end-to-end を試す:
   このセッション内で `RUNNER_MODE=critic PROJECT_ID=critic HEART_DATA_DIR=/data python3 ops/runner/runner.py`
   (worker Job の env には `CLAUDE_CODE_OAUTH_TOKEN` があるので動くはず)。上限や入れ子の都合で
   走らなければ、heart 側の入力生成だけ実行し、**同じ入力ファイルを自分で読んで critic の役をやり**、
   `/data/critic/2026-08-10.md` を書く。どちらの経路で得た所見かを PROGRESS.md に明記する。
   所見は今日の実データ (上の集計) から出す — 窓待ちの偏りとアイドル 94% は最低 1 件になる。
7. **テスト。** `test_reconcile.py` に `test_critic_due_*` / `test_spawn_critic_*` 系を足す:
   24h 未経過 / 24h 経過だが無活動 / 24h 経過 + 活動あり (→ `spawn_critic` と `last_critic_at` 更新) /
   breaker・stop_all 中 / 初回 (`last_critic_at` 無し)。`test_metrics.py` に `summarize_beats` の
   固定入力テスト (ビート間隔が不定でも `at` 差分で数えること、`beat` のリセットに引きずられないこと)。

### 実装上の罠 (踏むと 1 セッション無駄になる)

- **`python3 ops/dashboard/build.py` は `AUTOPILOT_GITHUB_TOKEN` があると `ops-dashboard` ブランチへ
  push する** (build.py の docstring)。この Job には token があるので、**検分目的で回すときは
  `env -u AUTOPILOT_GITHUB_TOKEN python3 ops/dashboard/build.py`** にすること。critic.md にも同じ注意を書く
  (critic Job も同じ token を持つ)。
- 同じく build.py は `ops/dashboard/prs.json` を書き換える (P-0015 / P-0026 の PROGRESS に実測あり)。
  **生成物を commit に混ぜない。**
- **`ops/dashboard/build.py` を編集しない。** P-0044 (ダッシュボードの 3 層表示) が同じファイルを
  同時進行で触っている。ここでは「実出力を読む」だけ。
- `/data/critic/` は heart 側が `mkdir(parents=True, exist_ok=True)` してから書く。Job 側で存在を前提にしない。
- `projects.json` は `statefiles.validate_projects()` を通ってから push される。トップレベルへの
  キー追加は自由 (`last_curriculum_at` の先例) だが、project エントリの必須フィールドは崩さない。
- 一時ファイルは `mktemp`。固定パス `/tmp/...` は前セッションの残骸を拾う (実測済みの罠)。
- セッション終了時に HEAD は `project/p-0045` のまま。wrapper が `git push origin HEAD:project/p-0045` を
  無条件に打つ。別ブランチに移らない。

## やらないこと

- **`ops/dashboard/build.py` の変更** — P-0044 の領分。ここは実出力を読むだけ (1 PR 1 論点)。
- **`ops/rules.json` の変更** — 24h の間隔は `reconcile.py` のモジュール定数で足りる。
  人間レビュー必須パスを閾値追加のために開けない。
- **consolidation / chore の spawn 配線** — 同じ「Phase 3 で配線」の未接続モードだが別の論点。
  気づいたことは PROGRESS.md の「発見」に 1 行書くだけにする。
- **critic が見つけた問題を直すこと** — 器の分業 (見つける役と直す役を分ける) が壊れる。
  所見は `/data/critic/` と Discord と curriculum の入力に流すところまでで、修繕は次のプロジェクト。
- **transcript の Git への持ち出し** — `/data/transcripts/` は PVC 内に留める規約
  (`rules.transcripts`)。PROGRESS.md に引用するのは所見の根拠として必要な数行まで。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` / CHARTER・VISION・`ops/memory/` の更新** — heart と
  別プロジェクトの領分。
