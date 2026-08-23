# P-0210 — 立案→判定の輪を閉じる — 棄却理由を台帳に刻ませ、提案を CI が検査する

## 目的

生成と判定を分離した副作用として、**判定の教師信号が生成に戻る経路だけが構造的に切れている**。
archive.jsonl には全案が記録されるのに棄却理由は 1 件も残っておらず (2026-08-23 実測で
217 行中 reject_reason 0 件)、生成役は自分の案がなぜ死んだかを知らず、同型再提案が常態化している
(immich postgres 更新系 7 度、ops-state 間引き 3 度、skills ライブラリ 3 度)。棄却理由と改善ヒントを
台帳に刻み、提案の機械検査を CI に置くことで、curriculum の質を運任業から構造で守る。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0210` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 -m unittest ops.tests.test_curriculum_feedback`
  — 判定出力契約 (reject_reason/improve_hint) → archive.jsonl への転記と、提案チェッカーの
    合否判定が、合成入力で落ちること/通ることの両方向に固定されていること。
    実測 rc=1 (ModuleNotFoundError — テストモジュール未存在、FAILED errors=1)。
- [ ] `python3 ops/check_proposals.py ops/tests/fixtures/proposals/bad.json; test $? -eq 1`
  — チェッカーが不正な提案列 (schema 欠落・verify 空・cell 語彙外・探索枠不足) を
    **ちょうど exit code 1** で落とすこと。実測: `check_proposals.py` 未存在のため python が
    rc=2 で終わり `test $? -eq 1` が失敗。
- [ ] `python3 ops/check_proposals.py ops/tests/fixtures/proposals/good.json`
  — チェッカーが正当な提案列を誤って落とさないこと (exit 0)。
    実測 rc=2 (`check_proposals.py` 未存在)。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- 判定役の出力契約は `ops/prompts/curriculum-judge.md`: 現在
  `{"scores": [{"id","total","breakdown"}], "adopted": [...]}`。reject_reason/improve_hint は
  この scores 各要素に載せる形が最小差分
- consume 側は `ops/runner/runner.py` の `fix_to_archive()` (runner.py:1039 付近)。proposals 全案を
  archive.jsonl に追記する唯一の場所で、scores を id で突き合わせて棄却案へ転記する
- cell 語彙は `ops/prompts/curriculum-generate.md` にしか無い (領域: k8s/storage/observability/
  security/life-prep/self × 種類: repair/prevent/feature/investigate/experiment)。
  探索枠比率は単一情報源 `ops/rules.json` の `curriculum.exploration_quota` (= 0.25) を読む
- 既存 check_*.py の流儀: 標準ライブラリのみ・引数で対象ファイルを受ける
  (`check_manifest_deletions.py` 同様)・exit code で合否。CI は 2026-08-22 に単一 `ci` job に
  統合されており、「ops job」= 常時実行される純 Python ステップ群
  (validate / unittest discover ops/tests / consistency checks)。unittest は
  `discover -s ops/tests` で自動回収なので配線不要
- `ops/validate.py` は採択案のみ検査する (verify 非空・cell 形)。追記キー (reject_reason 等) との
  干渉はなく、archive.jsonl は append-only (origin/main との先頭一致を validate が担保)

### 作り方

1. `ops/prompts/curriculum-judge.md` — 出力契約に reject_reason (**棄却した候補の id には必須**) と
   improve_hint (任意) を追加する文を書く
2. `ops/runner/runner.py` `fix_to_archive()` — scores を id で引き、棄却案のレコードへ
   reject_reason/improve_hint を転記する (採択案は触らない)
3. `ops/check_proposals.py` — 引数の proposals JSON (生成役出力形式) を検査し、違反で exit 1:
   schema 必須項目 / verify 非空 / cell 語彙 / 探索枠比率 ≥ rules.json の exploration_quota
4. `.github/workflows/ci.yml` の ops 部分 (consistency checks または直後の新ステップ) に
   good fixture を通す実行と bad fixture が rc=1 になる実行を足す
5. `ops/tests/test_curriculum_feedback.py` + `ops/tests/fixtures/proposals/{good,bad}.json` —
   転記ロジックとチェッカーを両方向で固定
6. `ops/prompts/curriculum-generate.md` — archive.jsonl を読む節に「前回の reject_reason/
   improve_hint を参照して同型再提案を避ける」義務を明記する

## やらないこと

- **archive.jsonl 既存行の遡及書き込み**。append-only が validate.py の不変条件であり、
  過去 217 行に棄却理由を後付けしない (効くのは次回生成以降)
- **生成役・判定役以外のプロンプト変更**、models.json や spawn 配線への手入れ
- **heart 側の再提案抑止ロジックの新設** (cooldown 等)。教師信号を返す経路を作るだけに留め、
  減点・遮断の機械強化は別論点
- **提案の内容そのものの改善** (immich postgres 系の代替案を考える等)。このプロジェクトは輪を作る装置であり、流す水ではない
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の編集**。autopilot が直接 push する
  領域でコンフリクトする (CLAUDE.md)
