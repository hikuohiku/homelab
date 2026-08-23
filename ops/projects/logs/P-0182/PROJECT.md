# P-0182 — budget_exhausted は終端ではなく休憩にする — checkpoint のある停滞を heart が自分で再予告する継続レーンを作り、手動継続の三重奏 (P-0114/115/116) を二度と起きなくさせる

## 目的

projects.json の stalled のうち budget_exhausted 死が直近だけで 10 件。復元演習・健康診断のような
長尺 Job は soft_cap に構造的に当たりやすく、人間は P-0114/115/116 と同じ案を手動で再採択して
継続させた — 望ましい挙動の実例が 3 回演済みということは、これは仕組みの不備である
(VISION「同じ失敗を 2 回したら」)。P-0106/P-0138 のメーター正確化とは別レイヤーで、
止め方そのものを「終端」から「予告つき自動継続」に変える。ループ最大の単一損失点への
直接回答であり、VISION 最優先「ループが止まらないこと」を守る。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0182` の checkout、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 -c "import json,sys; r=json.load(open('ops/rules.json')); sys.exit(0 if r.get('runner',{}).get('continuation') else 1)"`
  — rules.json の runner ブロックに continuation 設定 (enabled / extra_budget_tokens / max_continuations) が
  置かれていること。実測 rc=1 (`runner` ブロックはあるが `continuation` キーは未存在)。
- [ ] `grep -q 'continuation' ops/heart/reconcile.py`
  — 状態機械が継続レーンを持つこと。実測 rc=1 (reconcile.py に文言なし。
  budget_exhausted は ops/heart/reconcile.py:325-331 で無条件に stalled 化している)。
- [ ] `python3 -m unittest ops.tests.test_budget_continuation`
  — 遷移テスト 4 ケースが存在し通ること。実測 rc=1 (ModuleNotFoundError — モジュール未作成)。
- [ ] `test -s ops/projects/logs/P-0182/resume-evidence.json`
  — 実績: 実際の budget_exhausted 1 件以上がこの経路で再走した証跡 (状態遷移) が
  空ファイルでなく残っていること。実測 rc=1 (ファイル未存在)。

verify は DoD の下限。verify が直接見ないもの — (a) `max_continuations` を超えた再走をしないこと、
(b) checkpoint も PROGRESS.md も無い死 (初期化中の予算死) は stalled のままなこと、
(c) resume-evidence.json の中身が**実際の遷移の記録**であって捏造でないこと — は
worker が PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- **止まる場所は 1 行だけ**: reconcile.py の active 処理で result state が `budget_exhausted` のとき
  consume_result → `_stall(..., "budget_exhausted", "question", 「予算を積んで再開を指示してください」)`
  (ops/heart/reconcile.py:325-331)。ここを分岐させるのが本体。decide() は純関数なので、
  存在確認などの I/O は heart.beat() 側で fact として集めて渡す形にする (観測失敗 = None の規約)
- **budget_exhausted result の出生が 2 種類ある** (ops/runner/runner.py):
  :814 = initializer ループ中の予算死 (**checkpoint セッションも push も無い。何も始まっていない**)、
  :869 = worker ループの予算死 (直前に checkpoint セッションを走らせ、
  `## checkpoint (予算上限)` 節を PROGRESS.md に書いて push 済み — ops/prompts/checkpoint.md)。
  「checkpoint または PROGRESS.md の存在」確認は、この 2 つを区別するゲートとして機能する。
  PROGRESS.md はプロジェクトブランチ上 `ops/projects/logs/<PID>/PROGRESS.md` にある
- **観測経路は既にある**: heart の repo_dir は毎ビート `git fetch --prune origin` で全ブランチを
  取得する (gitutil.sync_main) ので、`gitutil.show(repo_dir, f"origin/{branch}", <PROGRESS.md path>)`
  でブランチ上の PROGRESS.md / checkpoint 節の有無を読める (戻り None = 無い)。
  gitutil.show / ls_remote_branch は facts から既に使用済みのパターン
- **手動継続の前例が仕様の雛形**: archive.jsonl の P-0114/115/116 はいずれも
  「(P-XXXX の継続)」題で soft_cap を 5M/5M/4M に増額し、why に
  「checkpoint と PROGRESS.md から再開すること」と明記している。自動継続 =
  proposed へ戻す + soft_cap へ extra_budget_tokens 加算 + 通常どおりの予告、でこの三重奏を機械化する
- **proposed への戻し方は新規登録ではない**: `_register_spec` は使わない (終端エントリの
  再登録は reconcile 冒頭の id 重複チェックがそもそも弾く)。既存エントリをその場で
  変更する: state→proposed、`continuation_count` += 1、`budget.soft_cap` += extra_budget_tokens、
  job / drift_count / restart_count 等の一過性フィールドを落とす。prs 履歴は残してよい
  (ensure_pr はブランチ head の open PR を再利用する)
- **adopt_gate をどうするか要決定**: proposed に戻ると gate 判定が再び走る。旧 gate レコード
  (ALL_FAIL 済み) を残せば即 announce だが測定は初回採択時のまま。`p.pop("adopt_gate")` すれば
  run_adopt_gate が再実測する (継続 1 回につき clone+verify 1 度のコスト)。**pop して再実測を推奨**
  (「信念でなく実測」の原則。その際 adopt_gate_attempts カウンタの扱いにも注意)
- **human_stop との優先順位は既に正しい**: stop/veto チェックは全遷移より先
  (reconcile.py:184-199) にあり、同ビートに budget_exhausted result が届いていても
  human_stop → stalled が勝つ。テスト 4 がこの順序契約を固定する
- **rules.json への追加は CI 安全**: validate.py check_heart_config は既存キーの型検査のみで
  追加キーを禁じない。`/ops/rules.json` と `/ops/heart/` は CODEOWNERS の人間レビュー必須パス
  (.github/CODEOWNERS:11,13) — **この PR の merge は人間待ちになる** (spec 注記のとおり)
- **証跡の取り方**: 遷移の記録は ops-state ブランチの audit.jsonl (action ごとに append)、
  projects.json スナップショット (active→proposed への逆行と continuation_count)、
  Discord announce に残る。resume-evidence.json はこれらを引用する
- **テストの流儀**: `python3 -m unittest ops.tests.test_budget_continuation`。
  ops/heart/tests/test_reconcile.py の project()/doc()/facts() ビルダ + RULES ロードの
  パターンを踏襲する (「このテーブルが仕様」)

### 作り方 (遷移表)

result `budget_exhausted` を消費するビートで、順に:

1. `stop_all` or veto → 既存どおり human_stop/vetoed (絶対に継続しない。冒頭条項が既に担保)
2. `enabled == False`、または `continuation_count >= max_continuations` → stalled (現状どおり)
3. checkpoint 節または PROGRESS.md がプロジェクトブランチ上に確認できない → stalled
   (何も継続するものが無い死。initializer 中の予算死がこれ)
4. 以上を通った場合のみ: stalled にせず proposed へ戻す (継続発火。以後は通常の
   採択ゲート→予告→veto 窓→着手の流れに合流し、Discord の announce が予告になる)

## やらないこと

- **終端 (stalled) エントリの復活**。decide() は終端をスキップするので、本案件は「判定時に
  止めないようにする」だけであり、既に stalled の P-0080/P-0102/P-0161 等を起き上がらせる
  レーンは作らない (それは別の論点。手動再採択 = 新 id の現行流儀が当面続く)
- **runner.py / prompts の変更**。checkpoint セッションは既に「PROGRESS.md への marker 書き込み +
  push」という、継続判定が読むに足る証拠を産んでいる。出生側の契約は変えない
- **waiting_quota (利用上限待ち) レーンへの介入**。あちらは P-0026 が作った「停滞ではない待ち」の
  別機構 (quota_wait_until / QUOTA_WAIT_MAX_ROUNDS) があり、死因が違う。触れない
- **通知形式・notify 予算の変更**。継続時の人間への可視化は通常の announce に乗る。追加の
  通知型は作らない
- **apps/ 配下の変更** (spec `touches_apps: false`)。
- **backlog.json / state.json / journal の編集**。autopilot 直接 push 領域でコンフリクトする (CLAUDE.md)

### DoD (4) の証跡について (worker への引き継ぎ)

mechanism は人間 merge 後に main で生きる。現在 active なプロジェクトは複数ある
(P-0164/0175/0181/0185、soft_cap 800k〜3M、2026-08-23 実測) ので自然発生は十分見込めるが、
タイミングは制御できない。**捏造したJSON を置かないこと**。予算内で実遷移を観測できなければ、
PROGRESS.md に「いつ・何を見れば証跡になるか (audit.jsonl の該当 action、projects.json の該当
エントリ)」を具体的に残して後続セッションに引き継ぐ。resume-evidence.json は実測した
遷移 (before/after の state、時刻、出典) を記録する。
