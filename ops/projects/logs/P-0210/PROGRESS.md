# P-0210 — PROGRESS

## セッション記録

### セッション 1 (2026-08-23)

**やったこと**: DoD 3 項目すべてを実装し、受入 verify 3 項目を実測 green。
- `ops/prompts/curriculum-judge.md` — 出力契約に reject_reason (棄却時必須) /
  improve_hint (任意) を追加。「刻まれた理由が次回生成役へ流れる」ことを明記
- `ops/runner/runner.py` — fix_to_archive() のレコード整形を純関数
  `build_archive_records()` へ抽出し、scores を id で突き合わせて**棄却案のみ**
  reject_reason/improve_hint を転記 (採択案は触らない。空白のみ・非文字列値はスキップ)
- `ops/check_proposals.py` 新設 — schema 必須項目 / title・why・dod 空 / verify 非空 /
  cell 語彙 / confidence 語彙 / budget・bool 型 / human-request⇔request_id 対応 /
  探索枠比率 (rules.json curriculum.exploration_quota=0.25 を読む、写さない) を検査し
  違反でちょうど rc=1 (使い方誤りのみ rc=2)
- `ops/tests/fixtures/proposals/{good,bad}.json` 新設。bad は受入文言の 4 形状
  (schema 欠落・verify 空・cell 語彙外・探索枠不足) を 1 ファイルで全部踏む
- `ops/tests/test_curriculum_feedback.py` 新設 — 転記 6 本 + チェッカー合成入力両方向 +
  実 fixture + main() の rc 契約、計 19 本
- `.github/workflows/ci.yml` — consistency checks に good 通過と bad が rc=1 になる
  実行を追加 (`if` 条件内なら bash -e でも落ちない)
- `ops/prompts/curriculum-generate.md` — archive.jsonl を読む節に reject_reason/
  improve_hint 参照と同型再提案禁止の義務を明記

**verify 実測 (リポジトリルート)**:
1. `python3 -m unittest ops.tests.test_curriculum_feedback` → OK (19 tests)
2. bad.json → rc=1 (10 error、4 形状全部が出た)
3. good.json → rc=0
加算: discover 3 種 (ops/tests 373・heart 206・runner 36) 全 green、validate.py 0 error、
py_compile OK。

**分かったこと / 罠**:
- 判定役が reject_reason を書かなくても pipeline は落ちない (転記はベストエフォート)。
  scores の欠落は旧契約の出力も壊さないため。強制したい場合は次の curriculum 起動後に
  archive.jsonl の転記率を見てから昇格させること
- 探索枠比率の分母は「dict として valid な全案」。cell が語彙外・ malformed な案は
  非 repair に数えない (fail-closed 方向に倒してある)
- bool は int の子孫なので budget.soft_cap_tokens だけ `isinstance(x, bool)` を明示排除
- sandbox に pip/ruff が無く F821 はローカル実測できていない (py_compile で代替)。
  CI の ruff step が初回の真実
- `ops/tests/fixtures/proposals/` には __init__.py を置いていない (既存 fixtures 同様。
  unittest discover は test*.py のみ回収するので問題なし)

**次のセッションへの一言**: 実装は完了済み。レビュー指摘があればその解消のみ。
指摘が無い場合やることは無い — wrapper が verify 全 green を実測してレビューへ進むのを待つ。

