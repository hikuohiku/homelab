# P-0182 PROGRESS

## 現在地

- 2026-08-23 initializer: PROJECT.md を作成し commit。実装は未着手。
- 受入チェックリスト 4 項目とも failing を実測済み (PROJECT.md 参照)。
- 2026-08-23 セッション2: **mechanism 実装完了**。verify 1〜3 が green (自己実測)。
  verify 4 (resume-evidence.json) のみ意図的に未達 — 下記「DoD (4) 証跡の取り方」参照。
- 2026-08-23 セッション3: 監視セッション。**merge 未了・PR 未開を確認** (下記経過)。
  証跡は merge 前なので原理的に発生不能。今日の予算死 3 件を特定したがすべて旧挙動。
- 2026-08-23 セッション4: 監視セッション。**merge 未了・PR 未開のまま** (約 11 分監視、
  下記経過)。verify 1〜3 green 再実測。コード変更なし。

## 経過

### セッション2 (2026-08-23)

PROJECT.md「作り方 (遷移表)」どおりに実装し、一括 commit:

1. **rules.json**: `runner.continuation {enabled: true, extra_budget_tokens: 2000000,
   max_continuations: 1}` を追加 (comment 付き)。validate.py check_heart_config
   は既存キーの型検査のみで追加キーを禁じないことを確認済み (0 error)。
2. **reconcile.py**: `_continue_budget()` ヘルパ新設 + active の budget_exhausted
   分岐を作り替え。遷移表のとおり stop/veto → (enabled/上限/証拠で抑止) → 継続発火。
   発火時は state→proposed、continuation_count+1、last_continuation_at、
   budget.soft_cap += extra、一過性フィールド (job / drift_count / restart_count /
   quota_wait_until / quota_wait_count / veto_deadline) を pop、adopt_gate と
   adopt_gate_attempts も pop (ゲート再実測。試行カウンタも新規扱い)。prs 履歴は残す。
3. **facts.py / heart.py**: `collect_continuation(repo_dir, doc, results)` 新設 —
   「active かつ budget_exhausted result 持ち」のプロジェクトだけ、判定対象ブランチを
   取り直してから `origin/<branch>:ops/projects/logs/<PID>/PROGRESS.md` の存在を観測する
   (True=続きあり / False=無い / None=観測失敗)。heart.beat() が
   `continuation_evidence` fact として decide() へ渡す。
4. **ops/tests/test_budget_continuation.py**: 15 テスト新設 (発火 3 / 抑止 4 /
   human_stop・veto 2 / rules.json の器 1 / 観測側 5 — 実 git bare origin での実測含む)。
   運用値から独立させるため continuation 設定は各テストで deepcopy 注入
   (test_reconcile.py の max_concurrent 注入と同じ流儀)。
5. **ops/heart/tests/test_reconcile.py**: 既存テーブル行
   `test_budget_exhausted_stalls_with_question_and_consumes` に
   `continuation_evidence={"P-0001": False}` を与えて更新 (「このテーブルが仕様」なので
   実装変更に合わせて先に変えた。継続レーン側の表は新テストファイルが持つ)。

verify 自己実測: 1 OK / 2 OK / 3 = 15 tests OK。全体回帰:
ops/heart/tests 196 OK、ops/runner/tests 36 OK、ops/tests 270 OK、validate.py 0 error。

### セッション3 (2026-08-23)

ops-state 監視と観測レシピの実測検証。コード変更なし。

- **merge 未了・PR 未開を実測**: origin/main 先頭は #531 (P-0185、10:13:59Z merge)。
  `git ls-remote origin` の refs/pull/*/head (513 個) のいずれも本ブランチ head c353eca55
  と一致しない → wrapper による PR はまだ開かれていない (全項目 green 待ち、と整合)。
  heartbeat.json は beat 120 @ 10:13:59Z で heart 自体は生きている。
- **merge 前の予算死が今日だけで 3 件発生し、すべて旧挙動で stalled 化** (証跡機会は失われた):
  - P-0157 @ 08:07:33Z (commit b957419bb, beat 17 decide)
  - P-0116 @ 08:32:11Z (commit a75f81a4c, beat 35 decide)
  - P-0161 @ 09:35:24Z (commit ff300b0bf, beat 90 decide)
  特に P-0116 は手動継続で soft_cap 4M まで積まれた三重奏の 1 つが**再び**予算死 —
  spec の why (「手動継続を二度と起きなくさせる」) の追強実績。死の頻度は半日で 3 件+、
  merge が間に合えば証跡はすぐ取れる水位。
- **ビート実測**: ops-state への push は約 80 秒間隔の常時ループ (09:51〜10:14 で 20 ビート)。
  CLAUDE.md の「毎時」は routines (curriculum 等) の話でビートではない。
  **beat 番号は boot 単位でリセットされる** (06:02Z の "beat 118 decide" と 10:11Z の
  "beat 118" は別インスタンスのものが線形履歴に混在)。監査照合は必ずコミット時刻で行う。
- verify 1〜3 再実測 green (15 tests OK)。ブランチは origin/project/p-0182 と同期済み。

### セッション4 (2026-08-23)

ops-state 監視の続き。コード変更なし。

- **merge 未了・PR 未開を再確認** (10:26〜10:39Z、約 11 分監視): origin/main 先頭は
  #532 (feat/telegram-reply-tool)。refs/pull の最新は #533 (P-0181、open) で本ブランチ
  head 50a0f5726 に対応する pull ref 無し。heart は生存 (beat 129 @ 10:24:35Z →
  beat 141 @ 10:38:32Z、約 70 秒間隔を維持)。10:38:32Z に spawn_curriculum を確認 —
  新規採択があれば観測対象が増える。
- **今日の budget_exhausted 出現コミットは遡及列挙で合計 11 件** (00:22〜09:41Z)。
  セッション3 特定の P-0157/0116/0161 を含むすべてが merge 前の旧挙動 stalled。
  遡及レシピ (下記) はこの実行で再検証済み、そのまま使える。
- **active 盤面を実測**: P-0092 announced / **P-0164 active cap=3M** /
  **P-0175 active cap=1.5M** / **P-0181 active cap=1.2M** / P-0182 (自プロジェクト) active。
  P-0181 は 10:23:21Z consume_review → spawn_runner 済みで、merge 後に最初に死ぬ候補の筆頭。
- verify 1〜3 再実測 green (15 tests OK)。ブランチは origin/project/p-0182 と同期済み。

## 分かったこと (次のセッションへの罠と前提)

- **runner の soft_cap は projects.json でなく main の archive.jsonl spec から来る**
  (runner.py load_spec → Budget(soft_cap))。しかも Budget は Job ごとに used_tokens=0
  から数え直す。つまり自動継続の実効的な予算増は「新しい runner Job が soft_cap 分を
  フルで使い直せること」と、帳簿 (projects.json budget.soft_cap) 上の積み増しの
  2 層。archive.jsonl は heart から書けないので、spec 側の増額は今回のレーンでは
  行わない (P-0106/P-0138 のメーター正確化とは別レイヤーという spec の注記どおり)。
- **quota 待ち梯子との順序**: active 冒頭の quota_wait_until 梯子は未来時刻なら
  continue するので、quota_wait_until を持つ fixture で budget_exhausted 分岐を
  テストすると到達しない (セッション2で実踏み)。result 判定前に梯子が優先される
  のは既存契約のまま、継続レーンは触っていない。
- **証拠取り逃し (誤 stalled) の防御を 2 重に入れた**: runner は checkpoint push 後に
  result.json を書くため、ビート冒頭の sync_main fetch が push に追いつかない隙間が
  ある。collect_continuation は対象ブランチだけ fetch し直してから読む。さらに git
  失敗時は False でなく None を返し、decide 側は None のビートでは判断も消費もしない
  (jobs=None と同じ規約)。False (確定的に無い) だけが stalled 化する。
- **既存 stalled (P-0080 等) は復活しない**: decide() は終端をスキップするので、
  本案件は「判定時に止めないようにした」だけ。証跡は今後の予算死で取る。
- **監視セッションの罠 (セッション3 実測)**:
  - `/tmp/opencode` は autopilot ユーザから書けない (permission denied)。
    `mktemp -d` の戻りを直接使えばよい (「固定パス /tmp」罠の強化版)。
  - runner 環境に `gh` CLI は無い。PR の有無は
    `git ls-remote origin | grep refs/pull` をブランチ head SHA と照合して判定する。
    PR コメント/レビュー状態はこの環境からは読めない (プロンプトの「レビュー指摘」節が情報源)。
    **注意 (セッション4 実測)**: `grep -c <head-sha>` は `refs/heads/project/p-0182`
    自身にもマッチする。PR 判定は必ず `refs/pull` に絞ること
    (`git ls-remote origin | grep "refs/pull.*<head-sha>"`)。
  - **証跡は遡及可能**: 遷移は ops-state の git 履歴と audit.jsonl に永久残る。
    ライブ監視を逃しても後から履歴を漁って resume-evidence.json が書けるので、
    監視のためにセッションを連打する必要はない。merge 後の最初の監視で十分。

## DoD (4) 証跡の取り方 (後続セッションへの引き継ぎ)

捏造は禁止 (PROJECT.md)。mechanism は人間 merge 後に本番の heart で生きる
(merge されると self_update_check が ops/heart の tree 変化を検知して exec し直す。
Pod 再作成は不要)。現在 active なプロジェクト (P-0164/0175/0181/0185) が予算死した
瞬間を ops-state ブランチで捕まえる:

1. merge 済みか確認 (この PR の人間レビュー待ち)。
2. ops-state の `projects.json` の履歴を見る:
   `git -C <state_dir> log -p -- projects.json | grep -B20 continuation_count` のように、
   `"state": "active"` → `"state": "proposed"` の逆行 + `"continuation_count": 1` の
   出現を探す (通常の proposed 登録とは違い、id が既存エントリの書き換えで出る)。
3. 同ビートの `audit.jsonl` に `{"action": "consume_result"}` が有り、直後のビートで
   同 pid に `run_adopt_gate` → `announce` → `spawn_runner` が続くこと。Discord の
   announce が「再予告」本体 (veto 窓付き)。
4. before/after を resume-evidence.json に書く。形式の目安:
   `{project, observed_at, sources: [ops-state の commit SHA ×2], before: {state,
   soft_cap, ...}, after: {state, continuation_count, soft_cap}, links: {audit.jsonl
   の該当行}}`。出典 (commit SHA / 監査行) が添わっている限り中身の正確さが担保される。

見つかるまで resume-evidence.json は置かない (= verify 4 は failing のまま)。
wrapper は全項目 green でしかレビューに進めないので、後続セッションは
「ops-state の監視」を主タスクにしてよい。

### 観測レシピ (セッション3 で全コマンド実測済み)

state_dir を探す必要はない。どの clone からでも `git show origin/ops-state:...` で読める:

```bash
# 現在の snapshot (state 別一覧)
git show origin/ops-state:projects.json

# 遷移検出: continuation_count の出現 (0 件なら未発火。セッション3時点で実測 0)
git log origin/ops-state -p -- projects.json | grep continuation_count

# 過去の予算死と PID の特定 (stalled_reason budget_exhausted を含むコミットを列挙)
git log origin/ops-state --format="%H %at %s" -- projects.json | \
  while read sha at rest; do
    git diff "$sha^" "$sha" -- projects.json 2>/dev/null | grep -q budget_exhausted && \
      echo "$sha $(date -u -d @$at +%FT%TZ)"
  done
# PID は同 diff で budget_exhausted より上に出る直近の "id" 行

# 直近の heart アクションと生存確認
git show origin/ops-state:audit.jsonl | tail
git show origin/ops-state:heartbeat.json
```

merge 後に遷移が発火していれば、grep continuation_count がヒットした commit SHA と
その 1 個前 (before) を sources に使う。audit.jsonl は append-only なので
`git show <decide-commit>:audit.jsonl | grep <pid>` で該当行も取れる。

## 次の一手

唯一のゲートは人間 merge。merge 済みになったら (refs/pull 照合 or main の log)、
上のレシピで continuation_count 出現を探し、出典つきで resume-evidence.json に書く。
actives は P-0164/0175/0181 (+ curriculum が随時新規採択。セッション4 時点で 10:38:32Z
に spawn_curriculum を確認済み)。死の頻度は半日 3 件+なので、
merge から数時間以内の観測を期待してよい。

監視セッションの進め方 (セッション3/4 の実績): 冒頭で fetch → merge 状態確認
(refs/pull 絞り込み) → 未 merge なら数分待機を 1〜2 回繰り返して様子見。
それでも merge 無しなら本セッションの観測事実をこのファイルに追記して commit して終了でよい
(証跡は遡及可能なので連打不要)。merge 済みなら遡及レシピで発火を探すのが主タスク。

## セッション5 の記録 (2026-08-23 10:42–10:53Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch で head 84075d7fe を確認 → refs/pull 照合で
PR 未開・origin/main 未含を再確認 (セッション3/4 と同状態)。5 分待機 ×2 を挟んで
3 回確認したが merge 無し。本ファイル追記 + commit して終了。

**盤面の実測 (10:42Z と 10:52Z の 2 回)**:

- `continuation_count` の出現は 0 のまま (`git log origin/ops-state -p -- projects.json | grep -c continuation_count` 相当で未ヒット。merge 前なので当然)
- heart は生存: heartbeat beat 143 @ 10:40:56Z → beat 152 @ 10:51:46Z (ビート約 70 秒間隔)
- **P-0185 が delivered になった** (main log で PR #531 の merge を確認。9a8562226)
- **P-0181 が active → in_review に遷移** (runner が soft_cap 1.2M 内で完了した模様。
  セッション4 が「merge 後の観測候補筆頭」と書いた候補はこれで一旦外れる。
  review reject で active に戻れば再び候補になりうる)
- P-0175 は active → in_review (10:40:56Z の consume_result + spawn_reviewer を audit で実視) →
  10:52Z 時点で active に戻っている。レビュー往復が速い
- 観測候補の現在地: **P-0164 (active, 3M)** と **P-0175 (active, 1.5M)** + 以後の curriculum 新規採択分
- 人間の活動兆候: 今日だけで #527/#528/#529/#530/#531/#532 と merge が続いている。
  本 PR のレビューも今日中に開かれる可能性はある

**次のセッションへの一言**: 変更なし — merge 待ち。盤面は「actives は P-0164/0175」に
入れ替わったが手順は一切変わらない。冒頭で fetch → refs/pull 照合 → merge 済みなら
遡及レシピ (上記) で continuation_count 出現を探す。未 merge なら待機 1〜2 回して
観測事実だけ追記して終了でよい。

## セッション6 の記録 (2026-08-23 10:56–11:08Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch → head 6039fa980 を refs/pull 照合
(0 件 = PR 未開) → origin/main 未含も確認 → 4〜4.5 分待機 ×2 を挟んで 3 回確認したが
merge 無し。本ファイル追記 + commit して終了。

**盤面の実測 (10:57Z / 11:03Z / 11:08Z の 3 回)**:

- `continuation_count` の出現は 0 のまま (merge 前なので当然)
- heart は生存: beat 157 @ 10:57:45Z → beat 162 @ 11:03:30Z → beat 166 @ 11:07:58Z
  (ビート約 70 秒間隔を維持)
- **P-0164 が active を外れた**: 11:01:13Z の `notify` (P-0164) を audit で実視。
  セッション5 の観測候補筆頭だったが in_review へ遷移した模様。review reject で
  active に戻れば再び候補になりうる (P-0181 と同じパターン)
- **P-0175 はレビュー往復後に再走中**: 11:02:22Z に consume_review + spawn_runner を
  audit で実視。actives 実測は **P-0175 (1.5M)** と自プロジェクト P-0182 のみ —
  現時点で「死にうる」active プロジェクトが 1 つしかない水位。curriculum の新規採択
  (spawn_curriculum) が観測候補の補充線になる
- 人間の活動兆候: 変化なし (今日の merge は #527〜#532 のまま、本 PR のレビューは未開)

**次のセッションへの一言**: 変更なし — merge 待ち。actives は P-0175 のみに減ったが
手順は一切変わらない。merge 済み判定 → 遡及レシピで continuation_count 出現を探す、
未 merge なら待機 1〜2 回して観測事実だけ追記して終了。監視対象の薄まりは
curriculum 新規採択と review-reject 復帰 (P-0164/0181) で自然に補われる想定。

## セッション7 の記録 (2026-08-23 11:10–11:22Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch (ops-state 91565e93b..81f47e1c6 更新を取得) →
refs/pull 照合 0 件 = PR 未開 → origin/main 未含も確認 → 4.5 分待機 ×2 を挟んで
3 回確認したが merge 無し。本ファイル追記 + commit して終了。

**盤面の実測 (11:11Z / 11:16Z / 11:21Z の 3 回)**:

- `continuation_count` の出現は 0 のまま (merge 前なので当然)
- heart は生存: beat 169 @ 11:11:19Z → beat 178 @ 11:21:22Z (ビート約 70 秒間隔を維持)
- **観測候補が全滅した**: セッション6 までの候補 2 件が両方 stalled になり、
  しかもどちらも予算死では無い — P-0164 は `stalled_reason: error`
  (11:01:13Z notify 後に runner エラー)、P-0181 は `stalled_reason: review_rejected`
  (10:55:25Z notify 後)。review-reject 復帰パターンでの補充は今回外れた
- **P-0175 は 2 周目を完了し in_review へ**: 11:12:26Z に consume_result +
  spawn_reviewer を audit で実視。reject で active に戻れば再び候補になりうる
- **actives 実測は自プロジェクト P-0182 のみ** — 「死にうる」他者プロジェクトが
  0 の水位。curriculum の新規採択 (spawn_curriculum) 以外に観測候補の供給源が無い。
  死の頻度は半日 3 件+だったので、採択が続けば数時間以内に予算死は再発する見込み
- 人間の活動兆候: 変化なし (今日の merge は #527〜#532 のまま、本 PR のレビューは未開)

**次のセッションへの一言**: 変更なし — merge 待ち。ただし盤面は「actives = 自分のみ」
まで薄まったので、観測候補の主供給源は curriculum 新規採択。手順は一切変わらない:
冒頭 fetch → refs/pull 照合 → merge 済みなら遡及レシピで continuation_count 出現を探す、
未 merge なら待機 1〜2 回して観測事実だけ追記して終了。

## セッション8 の記録 (2026-08-23 11:23–11:34Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch → head 092504803 を refs/pull 照合
(0 件 = PR 未開) → 実装コミット c353eca55 が origin/main 未含も確認 → 約 4.5 分待機 ×2 を
挟んで 3 回確認したが merge 無し。本ファイル追記 + commit して終了。

**盤面の実測 (11:24Z / 11:29Z / 11:34Z の 3 回)**:

- `continuation_count` の出現は 0 のまま (merge 前なので当然)
- heart は生存: beat 180 @ 11:23:36Z → beat 185 @ 11:29:12Z → beat 189 @ 11:33:34Z
  (ビート約 70 秒間隔を維持)
- **P-0175 が delivered になった**: 11:23:36Z の `merge_pr` (P-0175) を audit で実視。
  main log では PR #534 の merge (4ff29cfe3)。merge 後に origin/project/p-0175 は削除済み
  (fetch で `[deleted]` を実視)。レビュー往復 2 周で当日中に通った例
- **観測候補の新供給源が現れた**: **P-0092 が announced (3M)**。veto 窓通過後に active 化すれば
  次の予算死候補になる。actives 実測は自プロジェクト P-0182 (1.5M) のみのまま
- 人間の活動兆候: 今日の merge が #527〜#534 に増加 (本セッション監視中にも #534 が通った)。
  本 PR のレビューも今日開かれる可能性は維持

**次のセッションへの一言**: 変更なし — merge 待ち。手順は一切変わらない:
冒頭 fetch → refs/pull 照合 → merge 済みなら遡及レシピで continuation_count 出現を探す、
未 merge なら待機 1〜2 回して観測事実だけ追記して終了。観測候補は P-0092 (announced→active 待ち)
と curriculum 新規採択。

## セッション9 の記録 (2026-08-23 11:37–11:53Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch → head 7730dc58b を refs/pull 照合
(0 件 = PR 未開) → 実装コミット c353eca55 が origin/main 未含も確認 → 約 4.5 分待機 ×2 を
挟んで 3 回確認したが merge 無し。verify 1〜3 を再実測 green (15 tests OK)。
本ファイル追記 + commit して終了。

**盤面の実測 (11:37Z / 11:46Z / 11:52Z の 3 回)**:

- `continuation_count` の出現は 0 のまま (merge 前なので当然)
- heart は生存: beat 192 @ 11:36:54Z → beat 200 @ 11:45:50Z → beat 205 @ 11:51:24Z
  (ビート約 70 秒間隔を維持)
- **P-0175 が soaking になった**: delivered の後、state が `soaking` へ遷移しているのを
  projects.json snapshot で実視 (merge_pr 11:23:36Z 以降、audit に P-0175 の追加 action 無し —
  delivered→soaking は heart の自動遷移と整合)。soak 完了でどうなるかは本案件と無関係だが、
  「actives が減るもう一つの経路」として観測メモ
- **今日の予算死は増えていない**: 遡及列挙を再実行し、08-23 分は 11 件のまま
  (最新 = P-0161 @ 09:41:14Z)。つまり merge から 2 時間以上経過しても旧挙動の死は
  追加されず、証跡機会はまだ失われていない (死が起きない=証拠も取れない)
- **spawn_curriculum @ 11:39:07Z を audit で実視** — 採択が出れば観測候補が補充される
- actives 実測は自プロジェクト P-0182 のみで変化なし。P-0092 は announced (3M) のまま
- 人間の活動兆候: 今日の merge が #527〜#536 に増加 (#535 feat/core 常駐コア、
  #536 chore/core digest pin が監視前に通った)。ただし本 PR のレビューは未開のまま

**次のセッションへの一言**: 変更なし — merge 待ち。手順は一切変わらない:
冒頭 fetch → refs/pull 照合 (ブランチ head SHA で。`refs/heads` 自身に引っかけないこと) →
merge 済みなら遡及レシピ (上記「DoD (4) 証跡の取り方」) で continuation_count 出現を探す、
未 merge なら待機 1〜2 回して観測事実だけ追記して終了。予算死の供給源は現状ゼロ
(actives = 自分のみ・P-0092 は announced 待ち) なので、証跡取得は curriculum 新規採択か
P-0092 の active 化後になる見込み。

## セッション10 の記録 (2026-08-23 11:54–12:07Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch → head 1ffb0e53a を refs/pull 照合
(0 件 = PR 未開) → 実装コミット c353eca55 が origin/main 未含も確認 → 約 4.5 分待機 ×2 を
挟んで 3 回確認したが merge 無し。verify 1〜3 を再実測 green (15 tests OK)。
本ファイル追記 + commit して終了。

**盤面の実測 (11:53Z / 12:02Z / 12:06Z の 3 回)**:

- `continuation_count` の出現は 0 のまま (merge 前なので当然)
- heart は生存: beat 207 @ 11:53:40Z → beat 215 @ 12:02:30Z → beat 219 @ 12:06:52Z
  (ビート約 70 秒間隔を維持)
- **今日の予算死は増えていない**: 遡及列挙を --since で当日分に絞って再実行し、
  08-23 分は 11 件のまま (最新 = P-0161 @ 09:41:14Z)。merge 待ちが 2.5 時間を超えても
  旧挙動の死は追加されず、証跡機会はまだ失われていない
- **P-0175 の soak 完了**: soaking → delivered へ遷移 (11:55:48Z `deliver` を audit で実視)。
  これで「死にうる」他者プロジェクトは 0、actives 実測は自プロジェクト P-0182 のみ
- **curriculum の新提案が出た**: ブランチ heart/curriculum-20260823-120734 を fetch で検知、
  「7 案 (採択 4)」。merge されれば観測候補が最大 4 案補充される見込み。
  P-0092 は announced のまま変化無し
- 人間の活動兆候: 変化なし (本 PR のレビューは未開)

**次のセッションへの一言**: 変更なし — merge 待ち。手順は一切変わらない:
冒頭 fetch → refs/pull 照合 (ブランチ head SHA で) → merge 済みなら遡及レシピで
continuation_count 出現を探す、未 merge なら待機 1〜2 回して観測事実だけ追記して終了。
証跡機会の供給源は curriculum 採択 4 案 (heart/curriculum-20260823-120734, 未 merge) と
P-0092 の active 化。なお遡及列挙は全履歴ループだとタイムアウトするので
`--since="2026-08-23T00:00:00Z"` で絞ること (セッション10 実測)。

## セッション11 の記録 (2026-08-23 12:11–12:25Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch → head c371b87b2 を refs/pull 照合
(0 件 = PR 未開) → 実装コミット c353eca55 が origin/main 未含も確認 → 約 4.5 分待機 ×2 を
挟んで 3 回確認したが merge 無し。verify 1〜3 を再実測 green (15 tests OK)。
本ファイル追記 + commit して終了。

**盤面の実測 (12:11Z / 12:18Z / 12:23Z の 3 回)**:

- `continuation_count` の出現は 0 のまま (merge 前なので当然)
- **curriculum 採択 4 案が projects.json に登録済みを確認**: P-0187 restic 完整性
  (1.5M, confident) / P-0188 証明書期限台帳 (800k, confident) / P-0192 Telegram 欲望種蒔き
  (500k, unsure) / P-0193 ダッシュボード検眼 (1M, confident)。いずれも proposed。
  merge 済み PR #537「curriculum: 7 案 (採択 4)」(d43c61be7)、同 #538 fix/core-prompt-204 も
  当監視前に通過。heart/curriculum-20260823-120734 は merge 後削除を fetch で実視
- **P-0164 の死因は error (予算死では無い)**: in_review から stalled(error) へ遷移。
  P-0174/0181 は stalled(review_rejected)。つまり直近の停滞は予算死ゼロで、
  今日の budget_exhausted 死は 11 件のまま (最新 = P-0161 @ 09:41:14Z、09:41Z 以降新規無し)
- actives 実測は自プロジェクト P-0182 のみで変化なし。P-0092 は announced (3M) のまま
- open_prs は beat 220 時点で 4→6 に増加 (curriculum/fix 分。自 PR は未開のまま)
- heart は生存: beat 220 @ 12:07:58Z (merge_pr = #537 を audit 実視) → beat 229 @ 12:22:50Z
  (ビート約 70 秒間隔を維持)

**次のセッションへの一言**: 変更なし — merge 待ち。手順は一切変わらない:
冒頭 fetch → refs/pull 照合 (ブランチ head SHA で) → merge 済みなら遡及レシピで
continuation_count 出現を探す、未 merge なら待機 1〜2 回して観測事実だけ追記して終了。
証跡機会の供給源は更新: proposed 4 案 (P-0187/0188/0192/0193 — 採択・着手後の長尺死待ち) と
P-0092 の active 化。actives が自分しかいない状態が続くため、次の予算死は
新規採択が active 化してから数十分钟後になる見込み。遡及列挙時は
`--since="2026-08-23T00:00:00Z"` で絞ること (セッション10 実測)。

## セッション12 の記録 (2026-08-23 12:26–12:40Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch → head d032ecbd4 を refs/pull 照合
(0 件 = PR 未開) → 実装コミット c353eca55 が origin/main 未含も確認 → 約 4.5 分待機 ×2 を
挟んで 3 回確認したが merge 無し。verify 1〜3 を再実測 green (15 tests OK)。
本ファイル追記 + commit して終了。

**盤面の実測 (12:24Z / 12:32Z / 12:38Z の 3 回)**:

- `continuation_count` の出現は 0 のまま (merge 前なので当然)
- heart は生存: beat 230 @ 12:24:04Z → beat 236 @ 12:32:02Z → beat 239 @ 12:37:26Z
  (ビート約 70 秒間隔を維持)
- **観測候補が大幅補充 — curriculum 採択 4 案が active 化**: P-0187/0188/0192/0193 が
  12:09:09Z run_adopt_gate → 12:13:20Z announce + spawn_runner を audit で実視。
  actives 実測は自枠含め 5 に増加
- **さらに P-0196 が新規 active 化**: 監視中の 12:33:14Z merge_pr (PR #540「curriculum: 8 案
  (採択 1)」) の後、12:34:28Z adopt_gate → 12:37:26Z announce + spawn_runner を実視。
  「application-controller の OOMKill 解析」cap **4.5M** — 大型長尺 Job で将来の予算死候補筆頭。
  これで actives は 6 (自枠 + 5)
- **今日の予算死は増えていない**: 遡及列挙を `git log -S'"budget_exhausted"' origin/ops-state --
  projects.json` で実施 (全履歴ループより速く確実。**この方法が現レシピ**)。最終出現は
  ff300b0bf @ 09:35:24Z (= P-0161 の死、audit consume_result 09:35:18Z と整合) で、
  以後 3 時間以上新規無し。なお現行 projects.json の stalled_reason=budget_exhausted は
  9 件 (P-0080/0102/0116/0139/0142/0143/0144/0157/0161) — 手動継続済みの P-0114/0115 は
  stalled 集合から抜けているため、セッション9 までの「11 件」という今日分カウントとは
  総数の取り方が異なる (遡及は git 履歴ベースで行うこと)
- 人間の活動兆候: 監視中に #539 chore/repin-core、#540 curriculum が merge (#527〜#540)。
  ただし本 PR のレビューは未開のまま

**次のセッションへの一言**: 変更なし — merge 待ち。手順は一切変わらない:
冒頭 fetch → refs/pull 照合 (ブランチ head SHA で) → merge 済みなら遡及レシピで
continuation_count 出現を探す、未 merge なら待機 1〜2 回して観測事実だけ追記して終了。
証跡機会の供給源は更新: active 5 案 (P-0187 1.5M / P-0188 800k / P-0192 500k /
P-0193 1M / **P-0196 4.5M**) の長尺死待ちと P-0092 (announced, 3M) の active 化。
特に P-0196 は cap が大きく解析系なので最有力候補。予算死の遡及列挙は
`git log -S'"budget_exhausted"' origin/ops-state -- projects.json` で行うこと
(セッション12 実測。--since ループより速い)。

## セッション13 の記録 (2026-08-23 12:42–12:53Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch → head 4050c3e29 を refs/pull 照合
(0 件 = PR 未開) → 実装コミット c353eca55 が origin/main 未含も確認 → 約 4.5 分待機 ×2 を
挟んで 3 回確認したが merge 無し。verify 1〜3 を再実測 green (15 tests OK)。
本ファイル追記 + commit して終了。

**盤面の実測 (12:41Z / 12:46Z / 12:51Z の 3 回)**:

- `continuation_count` の出現は projects.json 全 70 エントリで 0 のまま (merge 前なので当然)
- heart は生存: beat 242 @ 12:41:34Z → beat 246 @ 12:46:28Z → beat 250 @ 12:51:15Z
  (ビート約 70 秒間隔を維持)
- **今日の予算死は増えていない**: `git log -S'"budget_exhausted"' origin/ops-state --
  projects.json` の最終出現は ff300b0bf @ 09:35:24Z (= P-0161 の死) のまま、以後 3 時間超新規無し
- states 実測: stalled 32 / delivered 29 / active 6 / vetoed 2 / announced 1。
  actives = P-0182 (自枠, 1.5M) + P-0187 1.5M / P-0188 800k / P-0192 500k / P-0193 1M /
  P-0196 4.5M。budget-dead stalled 集合も 9 件のまま不変 (P-0080/0102/0116/0139/0142/
  0143/0144/0157/0161)
- fetch 時に origin/project/p-0196 が new branch 出現 — P-0196 の runner が着手した実視。
  cap 4.5M の解析系なので、merge 後最初の自然予算死の最有力候補であり続ける
- 人間の活動兆候: 変化なし (本 PR のレビューは未開)

**次のセッションへの一言**: 変更なし — merge 待ち。手順は一切変わらない:
冒頭 fetch → refs/pull 照合 (ブランチ head SHA で) → merge 済みなら遡及レシピで
continuation_count 出現を探す、未 merge なら待機 1〜2 回して観測事実だけ追記して終了。
証跡機会の供給源は更新なし: active 5 案 (P-0187 1.5M / P-0188 800k / P-0192 500k /
P-0193 1M / P-0196 4.5M — 既に全員着手済み) の長尺死待ちと P-0092 (announced, 3M) の
active 化。特に P-0196 (cap 4.5M, 解析系, ブランチ出現済み) が最有力。予算死の遡及列挙は
`git log -S'"budget_exhausted"' origin/ops-state -- projects.json` で行うこと
(セッション13 再実測。--since ループより速い)。

## セッション14 の記録 (2026-08-23 12:53–13:06Z)

**やったこと**: ops-state 監視のみ。冒頭 fetch → head f1640456c を refs/pull 照合
(0 件 = PR 未開) → 実装コミット c353eca55 が origin/main 未含も確認 → 約 4.5 分待機 ×2 を
挟んで 3 回確認したが merge 無し。verify 1〜3 を再実測 green (15 tests OK)。
本ファイル追記 + commit して終了。

**盤面の実測 (12:52Z / 12:59Z / 13:04Z の 3 回)**:

- `continuation_count` の出現は projects.json 全 70 エントリで 0 のまま (merge 前なので当然)
- heart は生存: beat 251 @ 12:52:18Z → beat 257 @ 12:59:42Z → beat 261 @ 13:04:39Z
  (ビート約 70 秒間隔を維持)
- **今日の予算死は増えていない**: `git log -S'"budget_exhausted"' origin/ops-state --
  projects.json` の最終出現は ff300b0bf @ 09:35:24Z (= P-0161 の死) のまま、以後 3.5 時間超新規無し
- states 実測: stalled 32 / delivered 29 / active 6 / vetoed 2 / announced 1。
  actives = P-0182 (自枠, 1.5M) + P-0187 1.5M / P-0188 800k / P-0192 500k / P-0193 1M /
  P-0196 4.5M。budget-dead stalled 集合も 9 件のまま不変
  (P-0080/0102/0116/0139/0142/0143/0144/0157/0161)
- fetch 時に project/p-0187 / p-0188 / p-0193 ブランチが前進 — 各 runner が作業中の実視。
  P-0193 (cap 1M) は監視中に 2 回前進した
- 人間の活動兆候: 変化なし (本 PR のレビューは未開。main も #540 のまま)

**次のセッションへの一言**: 変更なし — merge 待ち。手順は一切変わらない:
冒頭 fetch → refs/pull 照合 (ブランチ head SHA で) → merge 済みなら遡及レシピで
continuation_count 出現を探す、未 merge なら待機 1〜2 回して観測事実だけ追記して終了。
証跡機会の供給源は更新なし: active 5 案 (P-0187 1.5M / P-0188 800k / P-0192 500k /
P-0193 1M / P-0196 4.5M — 全員着手済み・複数ブランチが動き続けている) の長尺死待ちと
P-0092 (announced, 3M) の active 化。特に P-0196 (cap 4.5M, 解析系) が最有力。
予算死の遡及列挙は `git log -S'"budget_exhausted"' origin/ops-state -- projects.json`
で行うこと (セッション14 再実測。--since ループより速い)。
