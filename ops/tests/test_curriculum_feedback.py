"""判定→台帳の教師信号経路と提案チェッカーを固定する (P-0210)。

リポジトリルートから `python3 -m unittest ops.tests.test_curriculum_feedback`
(CI は `discover -s ops/tests -t .` で自動回収)。

2 本柱:
1. 転記 — 判定役 scores の reject_reason / improve_hint が棄却案の archive
   レコードへ載り、採択案は触られないこと。これが切れると生成役は死因を
   知らず同型再提案が常態化する (immich postgres 更新系 7 度の実績あり)
2. チェッカー — ops/check_proposals.py が不正な提案列を落とし、正当な列を
   誤って落とさないこと。「今たまたま通っている」を排除するため、実 fixture
   と合成入力の両方向を見る
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ops import check_proposals as cp
from ops.runner.runner import build_proposal_records

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "proposals"

QUOTA = 0.25


def proposal(**over):
    """スキーマに完全適合する 1 案。テストはここから必要なだけ壊す。"""
    base = {
        "id": "P-9001",
        "title": "1 行タイトル",
        "why": "VISION / 現状のどの差分から来たか",
        "cell": ["storage", "prevent"],
        "dod": "何ができたら完成か",
        "verify": ["test -f README.md"],
        "irreversible": False,
        "capabilities": [],
        "touches_apps": False,
        "confidence": "confident",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}


def run_check(proposals, quota=QUOTA):
    return cp.check_proposals({"proposals": proposals}, quota)


class TestBuildArchiveRecords(unittest.TestCase):
    """scores → archive レコードへの転記。教師信号の本体。"""

    def proposals(self, *ps):
        return {"proposals": list(ps)}

    def adopted(self, ids, scores):
        return {
            "adopted": [{"id": i} for i in ids],
            "scores": scores,
        }

    def test_reject_feedback_is_transcribed(self):
        recs = build_proposal_records(
            self.proposals(proposal(id="P-0001")),
            self.adopted(
                [],
                [{"id": "P-0001", "total": 1,
                  "reject_reason": "同型再提案 (7 度目)",
                  "improve_hint": "更新系ではなく検証系に変える"}],
            ),
        )
        self.assertFalse(recs[0]["adopted"])
        self.assertEqual(recs[0]["reject_reason"], "同型再提案 (7 度目)")
        self.assertEqual(recs[0]["improve_hint"], "更新系ではなく検証系に変える")

    def test_improve_hint_is_optional(self):
        recs = build_proposal_records(
            self.proposals(proposal(id="P-0001")),
            self.adopted([], [{"id": "P-0001", "reject_reason": "verify が空"}]),
        )
        self.assertEqual(recs[0]["reject_reason"], "verify が空")
        self.assertNotIn("improve_hint", recs[0])

    def test_adopted_is_never_touched(self):
        """採択案に scores の鍵が混入すると採択 spec が汚染される。"""
        p = proposal(id="P-0002")
        recs = build_proposal_records(
            self.proposals(p),
            self.adopted(
                ["P-0002"],
                [{"id": "P-0002", "reject_reason": "採択したのに理由が付く",
                  "improve_hint": "これは起きてはいけない"}],
            ),
        )
        self.assertTrue(recs[0]["adopted"])
        self.assertNotIn("reject_reason", recs[0])
        self.assertNotIn("improve_hint", recs[0])

    def test_missing_score_leaves_record_intact(self):
        """旧契約の出力 (scores 無し) や書き忘れで落ちてはいけない。
        台帳への全案追記がこの関数の主責務なので、転記はベストエフォート。"""
        for adopted_doc in (
            {},
            {"adopted": [], "scores": []},
            {"adopted": [], "scores": [{"id": "P-9999", "reject_reason": "別の案"}]},
            {"adopted": [], "scores": [None, "broken"]},
        ):
            with self.subTest(adopted=adopted_doc):
                recs = build_proposal_records(
                    self.proposals(proposal(id="P-0003")), adopted_doc
                )
                self.assertNotIn("reject_reason", recs[0])
                self.assertFalse(recs[0]["adopted"])

    def test_blank_and_nonstring_values_are_skipped(self):
        recs = build_proposal_records(
            self.proposals(proposal(id="P-0004")),
            {"adopted": [], "scores": [
                {"id": "P-0004", "reject_reason": "   ",
                 "improve_hint": 42},
            ]},
        )
        self.assertNotIn("reject_reason", recs[0])
        self.assertNotIn("improve_hint", recs[0])

    def test_values_are_stripped_and_flag_added(self):
        p = proposal(id="P-0005")
        recs = build_proposal_records(
            self.proposals(p, proposal(id="P-0006")),
            {"adopted": [{"id": "P-0006"}],
             "scores": [{"id": "P-0005", "reject_reason": " 死因 "}]},
        )
        self.assertEqual([r["adopted"] for r in recs], [False, True])
        self.assertEqual(recs[0]["reject_reason"], "死因")
        self.assertNotIn("reject_reason", recs[1])
        for r in recs:
            self.assertIn("proposed_at", r)
        # 元の proposals を破壊しない (runner が後段で採択 id 一覧を読むため)
        self.assertNotIn("adopted", p)
        self.assertNotIn("reject_reason", p)


class TestCheckProposalsPure(unittest.TestCase):
    """チェッカー本体。違反 1 形状につき 1 テスト、両方向。"""

    def test_valid_single_proposal_passes(self):
        self.assertEqual(run_check([proposal()]), [])

    def test_exact_quota_passes(self):
        """1/4 (= 0.25) は下限ちょうど。誤って落とさない。"""
        ps = [proposal(cell=["self", "repair"], id=f"P-90{i}{j}")
              for i, j in ((0, 1), (0, 2), (0, 3))]
        ps.append(proposal(cell=["security", "experiment"], id="P-9099"))
        self.assertEqual(run_check(ps), [])

    def test_each_violation_shape_is_caught(self):
        cases = [
            ("必須キー不足", [proposal(dod=...)], "dod"),
            ("title 空", [proposal(title="")], "title"),
            ("id 形式", [proposal(id="X-42")], "P-NNNN"),
            ("id 重複", [proposal(), proposal()], "重複"),
            ("cell 領域", [proposal(cell=["kubernetes", "repair"])], "語彙外"),
            ("cell 種類", [proposal(cell=["self", "refactor"])], "語彙外"),
            ("cell 形状", [proposal(cell=["self"])], "[領域, 種類]"),
            ("verify 空", [proposal(verify=[])], "verify"),
            ("verify 空文字", [proposal(verify=[""])], "verify"),
            ("irreversible 型", [proposal(irreversible="no")], "irreversible"),
            ("capabilities 型", [proposal(capabilities={})], "capabilities"),
            ("touches_apps 型", [proposal(touches_apps=None)], "touches_apps"),
            ("confidence 語彙", [proposal(confidence="sure")], "confidence"),
            ("human-request 必須", [proposal(proposed_by="human-request")],
             "request_id"),
            ("request_id 専用", [proposal(request_id="req-x")], "専用"),
        ]
        for name, ps, needle in cases:
            with self.subTest(case=name):
                errs = run_check(ps)
                self.assertTrue(errs, f"{name} が素通しされた")
                self.assertIn(needle, "\n".join(errs))

    def test_quota_shortfall_is_caught(self):
        errs = run_check([proposal(cell=["self", "repair"], id="P-9001")])
        self.assertEqual(len(errs), 1)
        self.assertIn("探索枠不足", errs[0])

    def test_empty_proposals_fails_closed(self):
        """空の提案列は「1 案も書かずに終わる」のと同型。成功扱いにしない。"""
        for data in ({"proposals": []}, {}, {"proposals": None}, [1, 2]):
            with self.subTest(data=data):
                errs = cp.check_proposals(data, QUOTA)
                self.assertEqual(len(errs), 1)
                self.assertIn("proposals が空か配列でない", errs[0])

    def test_non_dict_element_is_reported(self):
        errs = run_check(["broken"])
        self.assertTrue(any("オブジェクトでない" in e for e in errs))


class TestFixturesOnDisk(unittest.TestCase):
    """CI と verify コマンドが実際に打つ 2 ファイルの中身を固定する。"""

    def load(self, name):
        return json.loads((FIXTURES / name).read_text())

    def test_good_fixture_passes_with_real_quota(self):
        errs = cp.check_proposals(self.load("good.json"), cp.load_quota())
        self.assertEqual(errs, [])

    def test_bad_fixture_covers_all_four_categories(self):
        errs = cp.check_proposals(self.load("bad.json"), cp.load_quota())
        text = "\n".join(errs)
        self.assertIn("必須キー不足", text)
        self.assertIn("verify", text)
        self.assertIn("語彙外", text)
        self.assertIn("探索枠不足", text)


class TestMainExitCode(unittest.TestCase):
    """main() の exit code 契約。CI と verify はこの rc しか見ない。"""

    def run_main(self, arg):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv",
                               ["check_proposals.py", *([] if arg is None else [arg])]), \
             redirect_stdout(buf):
            return cp.main()

    def test_good_fixture_exits_zero(self):
        self.assertEqual(
            self.run_main(str(FIXTURES / "good.json")), 0)

    def test_bad_fixture_exits_exactly_one(self):
        self.assertEqual(
            self.run_main(str(FIXTURES / "bad.json")), 1)

    def test_missing_file_fails_closed(self):
        """存在しない入力は rc=1。rc=0 での素通しも rc=2 の取りこぼしも避ける"""
        self.assertEqual(self.run_main("/nonexistent/proposals.json"), 1)

    def test_broken_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "broken.json"
            path.write_text("{broken")
            self.assertEqual(self.run_main(str(path)), 1)

    def test_usage_error_is_two(self):
        self.assertEqual(self.run_main(None), 2)


if __name__ == "__main__":
    unittest.main()
