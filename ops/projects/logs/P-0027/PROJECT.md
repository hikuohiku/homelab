# P-0027 — 器が沈黙したことを、器の外側から気づく (GitHub Actions の外部 watchdog)

## 目的

`ops/rules.json` の `heartbeat.stale_seconds` には「watchdog (外部) が ops-state の heartbeat を
この時間以上 stale と見たら通知」と書いてあるが、**その外部 watchdog は存在しない**。今の生存確認は
全部クラスタの内側にある — heart 自身が `heartbeat.json` を書き、ops-health-reporter CronJob が同じ
クラスタで読み、ダッシュボードも同じクラスタが配信する。heart / node01 / k3s のどれが死んでも、
死んだと言う口ごと死ぬ。旧・細切れループは「止まったまま死んだ」のに誰も気づかなかった。
VISION が最も恐れる再演を、検知する仕組みだけが無い。**GitHub Actions (クラスタの外、別の障害
ドメイン) に鮮度チェックを置き、stale なら issue #56 で人間を叩く。**

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing** (2026-08-09、`project/p-0027` の
checkout で、リポジトリルートから実行)。

- [ ] `test -f .github/workflows/watchdog.yml && grep -qE '^ *schedule:' .github/workflows/watchdog.yml && grep -q 'check_heartbeat_fresh.py' .github/workflows/watchdog.yml`
  — workflow が存在し、**定期実行 (`schedule:`) で** check スクリプトを呼んでいること。
    現在 `.github/workflows/` にあるのは `build-autopilot-image.yml` / `ci.yml` /
    `direct-push-guard.yml` / `release-image.yml` の 4 本だけ (実測 rc=1)。
    `workflow_dispatch` だけでは通らない = 人間が手で叩く道具ではなく**自動で回る**ことを要求している。
- [ ] `bash -c 'printf ... > /tmp/hb-stale.json; python3 ops/check_heartbeat_fresh.py --file /tmp/hb-stale.json; test $? -eq 1'`
  — 古い `at` (2000-01-01) を与えたら **rc=1 ちょうど**。現在はファイルが無く
    `can't open file` (実測 rc=1 だが python の起動失敗によるもので、判定として無意味)。
    rc は 1 に固定すること (2 や 3 では落ちる)。
- [ ] `bash -c '... 現在時刻の hb-fresh.json を作る ...; python3 ops/check_heartbeat_fresh.py --file /tmp/hb-fresh.json'`
  — 現在時刻の `at` なら **rc=0**。現在 rc=2 (ファイル無し)。この項目だけ
    `--comments-file` を渡さずに実行されるので、**引数なしでネットワークに出てはいけない**。
- [ ] `python3 -c "... spec_from_file_location('chf','ops/check_heartbeat_fresh.py') ... exec_module ... 'stale_seconds' in inspect.getsource(m)"`
  — 閾値を自分で決め打ちせず `rules.json` の `stale_seconds` を読んでいること。現在
    `FileNotFoundError` (実測 rc=1)。**この項目は `exec_module` する = import 時に副作用が
    あってはならない** (argparse も I/O も `main()` の中。`if __name__ == "__main__":` で守る)。

DoD (4) の「意図的に古いタイムスタンプで `workflow_dispatch` を 1 回走らせ、run の URL を
PROGRESS.md に残す」は verify コマンドに入っていない (機械検証できない)。**やらなくてよいのでは
なく、PROGRESS.md への記録が唯一の証拠になる。** 実行順の制約は「設計方針 (4)」に書いた。

## 設計方針

### 前提 (調べて分かったこと)

- `ops-state` ブランチは main の全ファイル + 状態ファイルを持ち、**`heartbeat.json` は
  リポジトリルート**にある (`ops/` の下ではない)。実測: `git show origin/ops-state:heartbeat.json`
  → `{"beat": 1, "at": "2026-08-09T09:41:19Z", "writer": "heart"}`。書き手は
  `ops/heart/statefiles.py` の `write_heartbeat()` のみ。`at` の書式は `now_iso()` =
  `"%Y-%m-%dT%H:%M:%SZ"`。heart は毎ビート末尾で `commit_and_push_state()` するので、
  heart が生きていれば `at` はビート周期 (現行 60s) で必ず進む。
- 閾値の単一情報源は `ops/rules.json` の `heartbeat.stale_seconds` = **7200 (2h)**。
  このファイルは CODEOWNERS 保護 (人間レビュー必須) なので**触らない**。
  スクリプト側の他の定数はモジュール定数にする (`ops/heart/adoptgate.py` の流儀、P-0015)。
- `.github/` は `.github/CODEOWNERS` で `@hikuohiku` 必須。**この PR は auto-merge されない。**
  トークンの `workflow` scope は 2026-08-07 に付いた (`ops/memory/substrate.md`) ので push は通る。
- **既存 CI job を増やさない。** ruleset の必須チェック一覧は人間専有 (substrate) で、新 job は
  「壊れていてもマージできてしまう」状態になる。テストは既存 `ops` job に**ステップを 1 つ追加**する。
- コメント投稿に `ops/post_issue_comment.py` を使ってはいけない。あれは
  `<!-- autopilot:self-posted -->` を必ず付け、取り込み側 (CHARTER §6) にスキップさせる道具で、
  spec が明示的に禁じている「食われるコメント」そのものになる。
- **本文が heart の triage に誤爆しないこと (最大の罠)。** 投稿したコメントは次のビートで
  `ops/heart/facts.py::collect_feedback` → `triage.classify` に食われる。`_matches_stop()` は
  ①いずれかの行が停止キーワード (`止めて` `止まって` `やめて` `中止` `stop` `abort` `veto`) で
  **始まる**、または②全体が 50 文字以下でキーワードを含む、で `stop_all` (全停止) に倒れる。
  さらに `veto P-\d{4}` を含むと veto になる。**本文は 50 文字超にし、どの行頭にも停止キーワードを
  置かず、`veto P-NNNN` の形を書かない。** (「〜が停止している可能性」のような叙述は行頭でなければ安全)
- GitHub Actions 側の credential は `${{ github.token }}` で足りる (`issues: write` を宣言する)。
  投稿者は `github-actions[bot]` になり、人間本人の PAT とは author で区別できる。
  `.github/workflows/direct-push-guard.yml` が `gh` + `GH_TOKEN: ${{ github.token }}` で
  issue にコメントする既存パターンを持っているので、それに合わせる (`gh` は runner に入っている)。

### (1) `ops/check_heartbeat_fresh.py` — stdlib のみ、判定は純粋関数

`ops/check_feedback.py` / `check_version_sync.py` と同じ粒度・同じ流儀 (stdlib のみ、docstring に
存在理由、CI から呼ばれる)。**判定を純粋関数に、I/O を `main()` に寄せる。**

- `load_stale_seconds(rules_path)` → `rules.json` の `heartbeat.stale_seconds` を読む。
  既定パスは `Path(__file__).parent / "rules.json"`。**import 時に読まない** (verify 4)。
- `judge(doc, now, stale_seconds)` → `{"stale": bool, "age_seconds": int|None, "at": str|None,
  "reason": str}`。純粋関数。
  - `at` を `datetime.fromisoformat(s.replace("Z","+00:00"))` で読む (`statefiles.parse_iso` と同じ流儀)。
  - `age >= stale_seconds` → stale。未来の `at` (clock skew) は stale ではない扱いにし、reason に残す。
  - **fail-closed**: ファイルが無い / JSON が壊れている / `at` が無い・読めない → **stale 扱い**。
    沈黙を検知する道具が「読めなかった」を「元気」に倒したら存在意義が無い。
- `should_post(comments, now, cooldown_seconds, marker, comments_since=None)` →
  `{"post": bool, "reason": str, "last_at": str|None}`。純粋関数。DoD (3) の重複抑止。
  - `comments` は GitHub API の issue comments JSON をそのまま (`created_at` / `body` を見る)。
  - `body` に `MARKER` を含む最新のコメントが `now - cooldown` より新しければ `post=False`。
  - **`comments_since` (取得に使った lookback) が `now - cooldown` より新しければ、抑止の判断が
    できないので `post=True` に倒す** (重複より沈黙の方が悪い)。この不変条件はテストで固定する。
- 定数: `MARKER = "<!-- watchdog:heartbeat-stale -->"` /
  `DRILL_MARKER = "<!-- watchdog:heartbeat-stale-drill -->"` /
  `REPOST_COOLDOWN_SECONDS = 21600` (6h)。
  - cooldown 6h の根拠: `stale_seconds` が 2h、schedule が 30 分毎なので、抑止が無いと 1 障害で
    1 日 48 件投稿される。6h なら最大 4 件/日。**stale が続く事実は job の fail で Actions 上に
    残り続ける**ので、コメントは「気づかせる」だけでよい。
  - **drill 用のマーカーを別にする理由**: 訓練 (DoD 4) のコメントが本番の抑止窓を食うと、直後に
    本当の沈黙が来ても黙る。`MARKER` の完全一致 (部分文字列) 判定に `DRILL_MARKER` が引っかからない
    ことをテストで固定する (`"<!-- watchdog:heartbeat-stale -->"` は
    `"<!-- watchdog:heartbeat-stale-drill -->"` の部分文字列ではない — 末尾の空白+`-->` が違う)。
- `build_body(judgement, marker, ...)` → 投稿本文 (Markdown)。何が起きているか / 何を確認して
  ほしいか / 誤検知だった場合にどうするかを人間向けに書く。マーカーは末尾に 1 行。
  上の「triage 誤爆」制約をここで守る。
- CLI (`main()` のみ。argparse):
  `--file <path>` (必須) / `--rules <path>` / `--now <iso>` (テスト用) /
  `--comments-file <path>` / `--comments-since <iso>` / `--body-out <path>` / `--json`。
  **stdout に理由を書く** (DoD 1)。
- **終了コード** (workflow がこれで分岐する。表をスクリプトの docstring にも書く):

  | rc | 意味 | workflow の振る舞い |
  |----|------|----------------------|
  | 0 | fresh | 何もしない (job success) |
  | 1 | stale、通知すべき | コメント投稿 → **job fail** |
  | 3 | stale だが cooldown 内 | 投稿しない → **job fail** |
  | 2 | 引数の誤り (argparse 既定) | job fail |

  `--comments-file` を渡さなければ抑止判定を一切しない (= stale なら常に rc=1)。verify 2/3 が
  この経路を通る。

### (2) `ops/tests/test_check_heartbeat_fresh.py` — 純粋関数のテスト

`ops/tests/__init__.py` を置き、`ops/heart/tests/` と同じ流儀 (`from ops import
check_heartbeat_fresh`、リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`)。
`ops/heart/tests/` に置かないのは、あれが CODEOWNERS 保護パスで、以後この非保護スクリプトの
テストを直すだけで人間レビューが要るようになるのを避けるため。最低限固定するケース:

| ケース | 期待 |
|--------|------|
| `at` が現在 | `stale=False` |
| `at` が `stale_seconds` ちょうど / 超過 | `stale=True` |
| `at` が未来 | `stale=False` (reason に skew を残す) |
| ファイル無し / 壊れた JSON / `at` 欠落 | `stale=True` (fail-closed) |
| `stale_seconds` が rules.json から読めている | `load_stale_seconds()` == 7200 |
| cooldown 内に MARKER 付きコメントあり | `post=False` |
| cooldown 外に MARKER 付きコメントあり | `post=True` |
| MARKER 無しコメントのみ (人間の会話) | `post=True` |
| DRILL_MARKER のコメントのみ | `post=True` (訓練が本番を黙らせない) |
| `comments_since` が cooldown より新しい | `post=True` (判断不能なら投稿に倒す) |
| 本文が triage に食われない | `triage.classify(build_body(...), rules)["kind"] == "review_needed"` |

最後の 1 行が誤爆の唯一の機械的歯止め。`ops/heart/tests/test_triage.py` と同じ方法で
`rules.json` を読み込んで突き合わせる。

### (3) `.github/workflows/watchdog.yml`

```
on:
  schedule: [{cron: "*/30 * * * *"}]
  workflow_dispatch:
    inputs: {simulate_at: <ISO 文字列。空なら実物>, dry_run: <boolean。投稿せず本文を表示>}
permissions: {contents: read, issues: write}
concurrency: {group: watchdog, cancel-in-progress: false}
```

ジョブの段取り:

1. `actions/checkout@v7` (既定 shallow)。**main 側の** `ops/check_heartbeat_fresh.py` と
   `ops/rules.json` を使う (ops-state のコピーではなく main が単一情報源)。
2. `git fetch --no-tags --depth=1 origin ops-state` → `git show FETCH_HEAD:heartbeat.json`。
   **失敗しても job を止めない** (`|| true`。ブランチが消えた・空も検知対象なので、
   ファイルが無い状態でスクリプトに渡し fail-closed に判定させる)。`FETCH_HEAD` を使うので
   P-0015 が踏んだ `--single-branch` の refspec 罠 (`ops/memory/substrate.md`) には当たらない。
3. `inputs.simulate_at` が空でなければ、取得した JSON の `at` をそれで上書きする (python3 の 1 行)。
   **これが DoD (4) の「意図的に古いタイムスタンプの入力」**。
4. コメント一覧を取る: `gh api "repos/${{ github.repository }}/issues/56/comments?per_page=100&since=<lookback>" --paginate > comments.json`。
   `lookback` は `date -u -d '-48 hours'`。**48h は `REPOST_COOLDOWN_SECONDS` (6h) より必ず長く
   すること** — 短いと抑止判定が不能になる (スクリプト側は `--comments-since` を受けて
   投稿に倒すが、両方にコメントで相互参照を書く)。
5. `python3 ops/check_heartbeat_fresh.py --file ... --comments-file ... --comments-since ... --body-out body.md`
   の rc で分岐 (`set +e` して rc を捕まえる):
   - 0 → 終わり。
   - 1 → `dry_run` なら本文を `$GITHUB_STEP_SUMMARY` に出すだけ、そうでなければ
     `gh issue comment 56 --body-file body.md` → `exit 1`。
   - 3 → 投稿せず理由をログに出して `exit 1`。
   - それ以外 → `exit 1`。
6. どの経路でも判定内容を `$GITHUB_STEP_SUMMARY` に残す (Actions の画面だけで状況が読めるように)。

**この workflow 自身は CI の検証対象外**である (`release-image.yml` と同じ構図、CHARTER §5.4)。
だから DoD (4) の実走が要る。

### (4) `.github/workflows/ci.yml` — `ops` job にステップ 1 つ追加

`- name: check heartbeat watchdog logic` → `python3 -m unittest discover -s ops/tests -t .`。
新 job は作らない (前提の項参照)。

### 実行順の制約 (DoD 4 をやる前に読むこと)

**`workflow_dispatch` は workflow ファイルが既定ブランチ (main) に無いと発火できない。**
`project/p-0027` に置いただけでは dispatch できない可能性が高い (未実測。GitHub の仕様として
そう文書化されている)。したがって:

- worker セッションのうちに `POST /repos/hikuohiku/homelab/actions/workflows/watchdog.yml/dispatches`
  (`ref` にこのブランチ、`inputs.simulate_at` に `2000-01-01T00:00:00Z`、`dry_run: false`) を
  **一度は実際に叩いて、返ってきた HTTP status を PROGRESS.md に書く。**
  `AUTOPILOT_GITHUB_TOKEN` は worker の env にある (初期化セッションで確認済み)。
  `curl -X POST -H "Authorization: Bearer $AUTOPILOT_GITHUB_TOKEN"` を使う (`gh` はイメージに無い)。
  トークンは fine-grained PAT で `x-oauth-scopes` が返らないため **Actions: write を持つかは未実測**。
  `actions/workflows` の一覧取得 (read) は 200 を実測済み。
- 404/422 で弾かれたら、それは「main に入るまでできない」という事実なので、
  **PROGRESS.md に「残っているのは merge 後の 1 回の dispatch だけ」と、叩くべき curl を
  そのまま貼って残す。** 器が merge 後にそれを実行できないなら、人間への依頼として
  PROGRESS.md 冒頭に書く (この PR は `.github/` を含むので、いずれにせよ人間が触る)。
- 訓練の投稿は `DRILL_MARKER` を付ける (本番の抑止窓を食わせないため)。本文にも訓練であることを
  明記する。

## やらないこと

- **`ops/rules.json` / `ops/models.json` の変更。** CODEOWNERS 保護。`stale_seconds` は
  読むだけ。cooldown はスクリプトのモジュール定数にする。
- **`ops/heart/` の変更。** heartbeat の書式・内容・周期に触らない。watchdog は器の外から
  既にあるものを読むだけ (VISION「器を太らせる前に、器を使い切る」)。
- **CI に新しい job を追加すること。** ruleset の必須チェック一覧は人間専有なので、
  既存 `ops` job にステップを足す。
- **`ops/post_issue_comment.py` の変更・流用。** 自己投稿マーカーを付けてしまう。
  watchdog は自分で本文を組み、`gh issue comment` で投稿する。
- **Discord への通知。** webhook を叩く口は器の内側の設計 (`ops/heart/notify.py`) で、
  外部 watchdog がそこに乗ると「外側に置いた」意味が薄れる。今回は issue #56 だけ。
  Discord に出すかは別の論点 (1 PR 1 論点)。
- **heartbeat 以外の状態 (projects.json の stalled 滞留、metrics.jsonl の傾向、
  ops-health-report の鮮度、ArgoCD の health) を見ること。** 今回は「沈黙の検知」だけ。
  拡張したくなったら別プロジェクト。
- **「heart のプロセスは生きているが仕事をしていない」の検知。** shadow モードでも heartbeat は
  進むので、鮮度だけでは分からない。これは別の論点として PROGRESS.md の「発見」に書くだけにする。
- **`ops/state.json` の `routines` (無効化済みクラウド定期実行) の復活。** 別のバックストップの話。
- **auto-merge を通そうとする工夫。** `.github/` は人間レビュー必須で、それが正しい
  (自分の強制装置を自分で merge しない — CODEOWNERS の設計決定 #7)。抜け道を探さない。
