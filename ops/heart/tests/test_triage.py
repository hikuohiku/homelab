import json
import unittest
from pathlib import Path

from ops.heart import triage

with open(Path(__file__).resolve().parents[2] / "rules.json") as f:
    RULES = json.load(f)


class TestTriage(unittest.TestCase):
    def test_veto_with_project_id(self):
        v = triage.classify("veto P-0012 お願いします", RULES)
        self.assertEqual(v["kind"], "veto")
        self.assertEqual(v["projects"], ["P-0012"])

    def test_veto_case_insensitive_and_multiple(self):
        v = triage.classify("VETO p-0001 と veto P-0002", RULES)
        self.assertEqual(v["kind"], "veto")
        self.assertEqual(v["projects"], ["P-0001", "P-0002"])

    def test_stop_keywords(self):
        for text in ("止めてください", "一旦 stop で", "全部やめて"):
            self.assertEqual(triage.classify(text, RULES)["kind"], "stop_all", text)

    def test_other_text_needs_review(self):
        v = triage.classify("ダッシュボードの配色が見づらい", RULES)
        self.assertEqual(v["kind"], "review_needed")

    def test_resume_keywords(self):
        for text in ("再開してください", "resume"):
            self.assertEqual(triage.classify(text, RULES)["kind"], "resume_all", text)

    def test_narrative_resume_mention_is_not_resume(self):
        long = "この間の障害のあと自動で再開する仕組みが欲しいという話を" * 3
        self.assertEqual(triage.classify(long, RULES)["kind"], "review_needed")

    def test_veto_takes_precedence_over_stop(self):
        # 「P-0003 は止めて」のような文は全停止でなく該当プロジェクトの veto
        v = triage.classify("veto P-0003 (これは止めて、他は続けて)", RULES)
        self.assertEqual(v["kind"], "veto")

    def test_empty_and_none(self):
        self.assertEqual(triage.classify("", RULES)["kind"], "review_needed")
        self.assertEqual(triage.classify(None, RULES)["kind"], "review_needed")

    def test_narrative_mention_is_not_stop(self):
        """長文フィードバック中の叙述的な出現で全停止を誤発火しない (指摘 [26])。"""
        text = (
            "ダッシュボードの自動更新が途中で止めてしまう問題があります。"
            "原因は fetcher の再試行が無いことだと思うので、直してもらえると嬉しいです。"
            "あと backstop の仕組みは良いと思いました。"
        )
        self.assertEqual(triage.classify(text, RULES)["kind"], "review_needed")

    def test_ascii_word_boundary(self):
        self.assertEqual(
            triage.classify("the backstop mechanism is nice", RULES)["kind"],
            "review_needed",
        )

    def test_imperative_line_start_is_stop(self):
        self.assertEqual(
            triage.classify("作業ありがとう。\n止めてください、今すぐ。", RULES)["kind"],
            "stop_all",
        )


if __name__ == "__main__":
    unittest.main()
