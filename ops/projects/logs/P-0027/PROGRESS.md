# P-0027 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## 人間への依頼 (この PR をレビューする人へ。器では実行できない)

**DoD (4) の実走訓練は、器の権限では実行できないことが実測で確定した。**
`AUTOPILOT_GITHUB_TOKEN` は Actions の **read は通るが write は 403**
(`Resource not accessible by personal access token`)。main に実在する `ci.yml` を ref=main で
dispatch しても同じ 403 なので、「workflow がまだ既定ブランチに無いから」ではなく
**トークンに Actions: write が無い**のが理由 (2026-08-09 実測、下記「分かったこと」参照)。

merge 後に、以下のどちらか 1 回をお願いしたい。これが「検知が本当に効く」ことの唯一の実証になる。

- GitHub UI: Actions → **Watchdog** → Run workflow → `simulate_at` に `2000-01-01T00:00:00Z`、
  `dry_run` は `false` (issue #56 に **[訓練]** と明記されたコメントが 1 件出て、job が fail すれば成功)
- または人間の PAT で:

  ```bash
  curl -i -X POST -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/repos/hikuohiku/homelab/actions/workflows/watchdog.yml/dispatches \
    -d '{"ref":"main","inputs":{"simulate_at":"2000-01-01T00:00:00Z","dry_run":"false"}}'
  # 期待: 204 No Content
  ```

訓練のコメントは `<!-- watchdog:heartbeat-stale-drill -->` を持ち、本番マーカー
`<!-- watchdog:heartbeat-stale -->` とは別物なので、**本番の再投稿抑止 (6h) を食わない**
(`test_drill_marker_does_not_suppress_production` で固定済み)。
訓練後に run の URL をこのファイルに追記すれば DoD (4) が閉じる。

`AUTOPILOT_GITHUB_TOKEN` に Actions: write を足すかどうかは別の論点 (下の「発見」参照)。
**足さなくてもこのプロジェクトの成立には影響しない** — schedule は権限に関係なく回る。

## 現況

受入 **4/4 green** (2026-08-09、`project/p-0027` で自分で実測)。
DoD (1)(2)(3) は実装・試験ともに完了。DoD (4) の実走だけが上記のとおり人間待ち。

## やったこと

- `ops/check_heartbeat_fresh.py` (新規、stdlib のみ)
  - `judge(doc, now, stale_seconds, load_error)` / `should_post(...)` / `build_body(...)` は純粋関数。
    I/O は `main()` と `read_heartbeat()` だけ。**import 時に副作用ゼロ** (受入 4 が `exec_module` する)
  - 閾値は `load_stale_seconds()` が `ops/rules.json` の `heartbeat.stale_seconds` (=7200) を読む。
    呼ばれたときに読む (import 時ではない)。cooldown は `REPOST_COOLDOWN_SECONDS = 21600` (6h) の
    モジュール定数 — rules.json は CODEOWNERS 保護なので触らない
  - fail-closed: ファイル無し / 壊れた JSON / 配列などの非 object / `at` 欠落 / `at` 解釈不能 は
    **全部 stale**。未来の `at` (clock skew) だけは fresh 扱いで reason に残す
  - rc 契約: `0`=fresh / `1`=stale で通知すべき / `3`=stale だが cooldown 内 / `2`=引数の誤り。
    docstring に表がある。`--comments-file` を渡さなければ抑止判定を一切しない (受入 2/3 がこの経路)
- `ops/tests/` (新規。`__init__.py` + `test_check_heartbeat_fresh.py`、38 tests)
  - `ops/heart/tests/` に置かなかったのは、あそこが CODEOWNERS 保護パスで、以後この
    非保護スクリプトのテストを直すだけで人間レビューが要るようになるため
  - 一番大事な 1 本は `test_not_eaten_by_triage`: 生成した本文を実際に
    `ops/heart/triage.classify()` に通して `review_needed` であることを確かめる
- `.github/workflows/watchdog.yml` (新規)。`schedule: */30 * * * *` + `workflow_dispatch`
  (`simulate_at` / `dry_run`)、`permissions: {contents: read, issues: write}`、
  `concurrency: watchdog`。ops-state を `git fetch --depth=1` して `git show FETCH_HEAD:heartbeat.json`
  (失敗しても止めず、ファイル不在のままスクリプトに渡して fail-closed に判定させる)
- `.github/workflows/ci.yml` の `ops` job にステップを 1 つ追加 (新 job は作らない。
  ruleset の必須チェック一覧は人間専有なので、新 job は「壊れていてもマージできてしまう」)

## 分かったこと / 罠

- **`AUTOPILOT_GITHUB_TOKEN` は Actions: write を持たない (実測)。**

  | 叩いたもの | 結果 |
  |---|---|
  | `GET /actions/workflows` | 200 |
  | `GET /actions/runs?per_page=1` | 200 |
  | `POST /actions/workflows/watchdog.yml/dispatches` (ref=project/p-0027) | **403** |
  | `POST /actions/workflows/ci.yml/dispatches` (ref=main、main に実在) | **403** |

  PROJECT.md は「404/422 で弾かれたら merge 待ち」を想定していたが、実際は **403 で、
  ref や workflow の存在以前にトークンで弾かれている**。したがって「merge 後に器が自分で
  dispatch する」経路は存在しない。次のセッションがここを試し直しても同じ 403 が返るだけ。
- **`<!-- autopilot:self-posted -->` は誰にも読まれていない** (`ops/post_issue_comment.py` が
  書くだけで、grep してもリポジトリ内に読み手が無い)。`ops/heart/facts.py::collect_feedback` が
  自分のコメントを除外する条件は `body.startswith("(Discord 不達のため代送)")` **だけ**。
  つまり「post_issue_comment を使うと器に食われる」という PROJECT.md の前提は、少なくとも
  heart の取り込み経路については成り立っていない。**結論は変わらない** (あの道具を使わないのは正しい)
  が、理由は「食われるから」ではなく「人間宛のコメントに自己投稿マーカーを付ける意味が無いから」。
- watchdog のコメントは triage で `review_needed` になるので、heart が生きていれば
  daily briefing の `review_needed` に載る。誤検知のときはそこにも出る (二重に人間へ届く)。
  `stop_all` / `veto` に倒れないことはテストで固定した
- `MARKER` は `DRILL_MARKER` の部分文字列では **ない** (`...stale -->` と `...stale-drill -->` で
  末尾が違う)。部分文字列一致のまま両方向のテストを置いてある。**マーカー文字列を変えるときは
  この性質を壊していないか確かめること**
- `should_post` の `comments_since` 分岐は PROJECT.md の記述より一段強くしてある:
  「取得窓が cooldown より短い → 投稿に倒す」は、**窓内に自分のコメントが見つからなかった場合だけ**。
  見つかっているなら抑止してよい (見つかった事実は窓の長さに依存しない)。両方テストで固定
  (`test_short_lookback_falls_back_to_posting` / `test_short_lookback_still_suppresses_when_evidence_found`)
- workflow の shell は全ステップ `bash -n` を通し、さらに **alert ステップを手元で実際に実行して**
  3 経路 (fresh→rc0 / stale+dry_run+drill→rc1 / 抑止中→script rc3 で job rc1) を実測した。
  `set -u` 下で空配列を渡す `"${drill[@]+"${drill[@]}"}"` も込みで動く
- `gh api --paginate` は gh のバージョンで配列の結合仕様が変わるので、`--jq '.[] | {...}'` で
  JSONL にしてから python で配列に組み直している。ここを素直な `--paginate > comments.json` に
  戻すと壊れる
- `date -u -d '-48 hours'` の 48h は `REPOST_COOLDOWN_SECONDS` (6h) より長いこと**が前提**。
  短くすると抑止判定が常に不能になる。`test_workflow_lookback_is_longer_than_cooldown` が見張っている

## 発見 (スコープ外。curriculum が拾う)

- **器はどの workflow も自分で起動できない** (上記 403)。今回は schedule で回るので困らないが、
  「器が GitHub Actions を道具として使う」類の設計は全部この壁に当たる。トークンに Actions: write を
  足すかは、権限を広げる話なので人間の判断。
- **`ops/post_issue_comment.py` の `<!-- autopilot:self-posted -->` が誰にも読まれていない。**
  書くだけで効果の無いマーカーが残っている (死んだ仕組み)。読み手を足すか、マーカーを消すか。
  CHARTER §6 の記述と実装が食い違っている可能性がある。
- **「heart のプロセスは生きているが仕事をしていない」は鮮度では検知できない。** shadow モードでも
  heartbeat は進む。beat 番号の進み方 / projects.json の状態遷移の停滞を見る別の watchdog が要る
  (PROJECT.md の「やらないこと」で意図的に外した論点)。
- watchdog が見ているのは heartbeat だけ。ops-health-report の鮮度、projects.json の stalled 滞留、
  ArgoCD の health は依然としてクラスタの内側からしか見えていない。

## 次のセッションへ

**実装は終わっている。手を入れる前に、まず自分で 4 つの verify を回して 4/4 を確認すること。**
green なら**新しいコードを書く必要は無い** — 残りは人間待ちの実走訓練 (このファイル冒頭) だけ。

- **`workflow_dispatch` をもう一度試さないこと。** 403 は ref の問題ではなくトークンの権限で、
  再試行しても同じ 403 が返るだけ (実測済み)。時間を捨てる。
- レビューで差し戻されたら、直す対象はほぼ `ops/check_heartbeat_fresh.py` か
  `.github/workflows/watchdog.yml`。**`.github/` を触ったら CI では守られない**ので、
  `python3 -m unittest discover -s ops/tests -t .` と、workflow の run ブロックを
  手元で `bash -n` + 実行して確かめてから commit すること (このセッションでやった手順が
  「分かったこと」に書いてある)。
- 触ってはいけないもの: `ops/rules.json` / `ops/models.json` / `ops/heart/` (全部 CODEOWNERS 保護、
  または器の領分)。閾値は rules.json から読むだけで、こちらで決め打ちしない。
