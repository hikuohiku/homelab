# P-0182 PROGRESS

## 現在地

- 2026-08-23 initializer: PROJECT.md を作成し commit。実装は未着手。
- 受入チェックリスト 4 項目とも failing を実測済み (PROJECT.md 参照)。
- 2026-08-23 セッション2: **mechanism 実装完了**。verify 1〜3 が green (自己実測)。
  verify 4 (resume-evidence.json) のみ意図的に未達 — 下記「DoD (4) 証跡の取り方」参照。

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

## 次の一手

merge 後の本番ビートで実遷移 (active → proposed + continuation_count) を観測し、
出典つきで resume-evidence.json に記録する。
