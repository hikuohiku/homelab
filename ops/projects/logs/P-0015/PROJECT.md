# P-0015 — 壊れた仕様を予告する前に殺す (採択と予告の間の新品 clone ゲート)

## 目的

VISION「完成は verify の実測だけが宣言する」の裏面が抜けている。今は spec が壊れていることを
runner Job が起動してから気づく。P-0012 と P-0013 は連続で `spec_error` (開始前に verify が
pass している) で死に、拒否権窓の予告 2 回・Job 2 回・Discord 通知 2 通を捨てた。3 回目
(P-0014) は人間が手で新品 clone を作って all-fail を実測してからでないと通せなかった
(archive.jsonl の `proposed_by` に記録がある)。VISION「同じ失敗を 2 回したら仕組みの不備」に
そのまま当たる。**採択 (`proposed`) と予告 (`announced`) の間に、使い捨ての新品 clone で
verify を実測するゲートを置き、all_fail 以外を予告前に殺す。**

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing** (2026-08-08、`project/p-0015` の
checkout で、リポジトリルートから実行)。

- [ ] `test -f ops/heart/adoptgate.py`
  — 新規モジュールが存在すること。現在 `ops/heart/` にあるのは
    `config.py / facts.py / gh.py / gitutil.py / heart.py / k8s.py / metrics.py / notify.py /
    reconcile.py / spawn.py / statefiles.py / triage.py` のみ。
- [ ] `grep -q 'adoptgate' ops/heart/reconcile.py`
  — 状態機械がゲートを参照していること。現在 `reconcile.py` に `adoptgate` の語は 0 箇所。
    **`import` するだけで grep は通ってしまう**ので、実際に proposed → announced の分岐で
    使うこと (下の設計方針を守れば自然に満たされる)。
- [ ] `python3 -m unittest ops.heart.tests.test_adoptgate`
  — 新規テストが存在して green。現在は `ModuleNotFoundError` (`Ran 1 test / FAILED (errors=1)`)。
    ファイル名は `ops/heart/tests/test_adoptgate.py`。CI (`ops` job) は
    `python3 -m unittest discover -s ops/heart/tests -t .` なので、置くだけで CI にも載る。
- [ ] `grep -q 'single-branch' ops/memory/substrate.md`
  — clone の罠が意味記憶に載っていること。現在 `substrate.md` に `single-branch` の語は 0 箇所。

4 項目とも `origin/ops-state` の有無にも Discord/クラスタにも依存しない。ローカル checkout だけで
判定できる。

## 設計方針

### 前提 (調べて分かったこと)

- `reconcile.py` の契約は冒頭 docstring で **「純関数のみ — I/O を書かない」**。clone も
  subprocess もここには書けない。副作用は `heart.py` の `execute()` が action を受けて行う
  (`spawn_runner` が `p["job"]` を書き戻す既存パターンがそのまま使える。`execute()` は
  1 段目の `save_projects` の後に走り、2 段目の `save_projects` (heart.py:283) で永続化される)。
- 採択の登録は `reconcile.decide()` 冒頭の `_register_spec()` (reconcile.py:110-114) で、
  **登録されたその同じビートの中で `proposed` → `announced` まで進む**。ゲートはこの直進を
  1 ビート止める形で挟むのが最も無理がない。
- 予算・閾値の単一情報源は `ops/rules.json` だが、**このファイルは ruleset の人間レビュー必須パス**
  (rules.json の `_comment`)。触ると auto-merge できなくなる。タイムアウト値は
  `reconcile.py` の `REVIEW_TIMEOUT_HOURS` と同じ流儀で **`adoptgate.py` のモジュール定数**にする。
- `runner.run_verify()` (runner.py:293-308) が verify 実行の既存実装
  (`subprocess.run(["bash","-c",cmd], cwd=repo_dir, capture_output=True, text=True, timeout=600)`)。
  ゲートはこれと**測定として等価**でなければ意味がない (ゲートが通して runner が弾く、が最悪)。
  実行の形をそのまま合わせる。
- heart の clone は `gitutil.run()` 経由で `https://github.com/<repo>.git` を token 無しで
  clone している (= public repo)。認証の追加は要らない。

### (1) `ops/heart/adoptgate.py` — 純粋関数 + 実行部

**純粋関数 `classify(results) -> dict`** — verify の実測レコード列から判定だけを出す。
`{"verdict": "all_fail" | "some_pass" | "broken_command", "passed": [cmd...], "broken": [{cmd, reason}...]}`。
判定順は **broken_command > some_pass > all_fail** (壊れたコマンドが 1 本でもあれば測定自体が
信用できないので、他が all_fail でも予告しない)。spec が返り値を 3 値に固定しているので
**タイムアウトは独立の verdict にせず `broken_command` に畳む** (理由文字列に `timeout` を残す)。

**実行部 `run_gate(repo_url, verify, ...) -> results`** — 使い捨ての新品 clone で 1 本ずつ実行。

- `tempfile.mkdtemp()` で作り `finally: shutil.rmtree(...)` で必ず捨てる。固定パスを使わない
  (`/tmp` は Pod の生存期間を通じて持続し、前回の残骸を拾う — `ops/memory/substrate.md`)。
- **`--depth=1` を使わない。** 使うなら refspec を明示して fetch し直す (下の罠)。
  clone 後に `git fetch origin '+refs/heads/*:refs/remotes/origin/*'` を明示的に打ち、
  `origin/main` と `origin/ops-state` の両方が見える状態を作ってから
  `git checkout -B main origin/main` する。verify に
  `git show origin/ops-state:projects.json` 系を含む spec (ダッシュボード系) がこれを要る。
- 実行は `["bash", "-c", cmd]`、`cwd` = clone 先。**per-command timeout はモジュール定数**
  (既定 120s 程度)。全体にも上限を持たせる (ビートを何十分も止めない。既定 300s 程度)。
- 各コマンドのレコード: `{"cmd", "ok" (rc==0), "rc", "timeout": bool, "syntax_error": bool,
  "not_found": bool, "output": 末尾 2000 字}`。

**broken の判定 (実測済み, 2026-08-08, bash 5.x / git 2.54.0):**

| 事象 | 実測 | 使い方 |
|------|------|--------|
| `bash -n -c '<cmd>'` | 構文エラーのときだけ rc=2、それ以外 (存在しないコマンドを含む) は rc=0 | **構文エラーの唯一の判定手段。実行前に打つ** |
| `bash -c 'no_such_cmd_xyz'` | rc=127 | `not_found` |
| `bash -c 'grep -q x /nonexistent'` | **rc=2** | **罠**: 実行時 rc==2 を構文エラーと見なしてはいけない。grep の「ファイルが無い」も 2 を返す。これは正当な「まだ出来ていない」 |
| `bash -c 'test -f /nonexistent'` | rc=1 | 正当な fail |
| timeout | `subprocess.TimeoutExpired` | `timeout=True` → broken_command |

### (2) `ops/heart/reconcile.py` — proposed の分岐にゲートを挟む

`state == "proposed"` の枝 (reconcile.py:139-145) を次に置き換える。**announce も veto 窓も
一切消費しない**のが要件の核心。

- `p` にゲート結果 (`p["adopt_gate"]`) が無い → **`announced` に進めず**、
  `_action("run_adopt_gate", pid)` を出して `proposed` のまま次のビートを待つ
  (breaker 中は従来通り何もしない)。
- ある → `adoptgate.classify(...)` の verdict で分岐:
  - `all_fail` → 従来通り `announced` + `veto_deadline` + `announce` action。
  - それ以外 → **予告せず** `_stall(p, actions, "adopt_gate_" + verdict, ...)`。
- `heart.py` の `execute()` に `run_adopt_gate` を足す: `adoptgate.run_gate()` を呼び、
  結果を `p["adopt_gate"] = {"at":..., "verdict":..., "passed":[...], "broken":[...],
  "verify": [...]}` として書き戻す (2 段目の `save_projects` で永続化)。shadow モードでは
  他の action と同様 `log("[shadow] ...")` だけにするか、副作用が読み取りのみなので実行して
  記録だけ残すか、どちらかに決めて heart.py にコメントで理由を残すこと。
- **記録は incident ではなく「採択の不良」**: `stalled_reason` は
  `adopt_gate_some_pass` / `adopt_gate_broken_command`。理由の実体 (どの verify が pass したか /
  どのコマンドが壊れているか) は `p["adopt_gate"]` に残る = projects.json に残る。
  通知型は `incident` を使わない。`Notifier.IMMEDIATE_TYPES` は
  `announce/deliver/question/incident/review` なので **`question`** を使う (spec の直しを促す)。
- **新しい state を作らない。** `statefiles.PROJECT_STATES` は 9 個のまま、
  `validate_projects()` も触らない。`stalled` は既に終端で、`REQUIRED_PROJECT_FIELDS` に
  追加フィールドの制約は無いので `adopt_gate` キーを足しても検証は通る。
- ゲートは spec 1 件につき 1 回でよい。`p["adopt_gate"]` があれば再実行しない (再実行すると
  毎ビート clone する)。

### (3) `ops/heart/tests/test_adoptgate.py`

**ネットワークに出ない。** `run_gate` の中の「clone する部分」と「与えられたディレクトリで
verify を実行する部分」を分け、テストは後者 (例 `run_verify_in(dir, cmds)`) を
`tempfile.TemporaryDirectory()` に対して実行する。spec が要求する 4 ケース:

| ケース | 例 |
|--------|-----|
| all_fail | `test -f nonexistent` / `grep -q zzz missing.txt` → 全 rc≠0 → `all_fail` |
| 一部 pass | 上に `test -d .` を混ぜる → `some_pass` + `passed` に該当コマンド |
| 存在しないコマンド | `no_such_cmd_xyz_p0015` → rc=127 → `broken_command` |
| タイムアウト | `sleep 5` を timeout=1 で → `broken_command` (理由 `timeout`) |

加えて `classify()` は合成レコードでの純粋関数テストも書く (判定順 broken > some_pass > all_fail
を固定する)。`tests/test_reconcile.py` の流儀 (ヘルパで dict を組み立て、遷移表を表として書く)
に合わせる。reconcile 側の新しい分岐 (ゲート未実施なら announce しない / verdict 別の遷移) は
`test_reconcile.py` にも足すこと — **これを足さないと既存の proposed→announced のテストが
落ちるはず**なので、いずれにせよ直すことになる。

### (4) `ops/memory/substrate.md` に罠を追記

`git clone --depth=1 <url>` は **`--single-branch` を含む**。実測 (2026-08-08, git 2.54.0):
clone 直後の `remote.origin.fetch` が
`+refs/heads/<clone したブランチ>:refs/remotes/origin/<同>` の **1 本だけ**になる。以後
`git fetch origin` を何度打ってもこの refspec しか使われないので、`origin/main` も
`origin/ops-state` も生えず、`git show origin/ops-state:projects.json` が
`rc=128` で静かに落ち続ける。復旧は明示 refspec
`git fetch origin '+refs/heads/*:refs/remotes/origin/*'` (打った直後に `origin/main` が
生えるのを実測)。**shallow のままでも `git show origin/<branch>:<path>` は成功する**ので、
`--unshallow` までは要らない。P-0014 の worker が踏んだ。

書式は既存行に合わせ、**`verified_at:` と出典を必ず付ける** (`ops/memory/README.md`)。
節は「コンテナ / ファイルシステム」か、新設するなら「git」。README は
「書き手は consolidation の PR のみ」としているが、**この追記は spec の DoD (4) が
名指しで要求している例外**である旨を 1 行添えると、後の consolidation が混乱しない。

## やらないこと

- **`ops/rules.json` / `ops/models.json` の変更。** ruleset の人間レビュー必須パスで、
  触ると auto-merge が止まる。タイムアウト等は `adoptgate.py` のモジュール定数にする
  (`reconcile.py` の `REVIEW_TIMEOUT_HOURS` と同じ流儀)。
- **`ops/runner/runner.py` の変更 / `run_verify` との共通化リファクタ。** 重複は承知の上で
  「形を合わせる」に留め、コメントで相互参照するだけにする。統合したくなったら別プロジェクト
  (1 PR 1 論点、CHARTER §4)。開始前 all-fail ゲート (runner.py:346-356) は**残す** —
  ゲートは予告の前段であって、runner 側の最後の砦を置き換えるものではない (二重に守る)。
- **`statefiles.PROJECT_STATES` への新 state 追加・`validate_projects()` の変更。**
  `stalled` + `stalled_reason` + `adopt_gate` で足りる。
- **`ops/heart/notify.py` / `IMMEDIATE_TYPES` の変更。** 既存の `question` 型を使う。
- **curriculum 側 (`ops/prompts/`・立案プロンプト) の変更。** 「壊れた spec を作らせない」は
  別の論点。今回は「作られてしまった壊れた spec を予告前に殺す」だけ。
- **同じ id での再採択の面倒を見ること。** 差し戻された P-NNNN は projects.json に終端
  (`stalled`) で残るため、同じ id で archive.jsonl に再採択されても `_register_spec` は
  蘇らせない (reconcile.py:110-114 の既存仕様)。P-0012→P-0013→P-0014 と同様、直した spec は
  新しい id で採択される前提。これを変えるなら別プロジェクト。
- **`ops/validate.py` の変更。** ここが見るのは main 側の状態ファイルで、projects.json
  (ops-state ブランチ) ではない。
- **ゲートを非同期化する仕組み (専用 Job / 別スレッド)。** 今回は heart のビート内で同期実行し、
  モジュール定数のタイムアウトで上限を切る。ビートが重いと分かってから別プロジェクトで直す。
- **`apps/` 配下・Discord・ダッシュボードへの変更。** `touches_apps: false` の spec であり、
  soak も要らない。
