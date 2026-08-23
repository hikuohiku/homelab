"""ops/stage3/readiness.json の schema と evidence 存在検査を固定する (P-0185)。

リポジトリルートから `python3 -m unittest ops.tests.test_stage3_readiness`。
実リポジトリだけを見る検査は「今たまたま通っている」と「正しい」を区別できないので、
schema・verdict 規則・evidence 存在の純関数側は合成入力で両方向
(落ちること / 通ること) を固定してから、実台帳に対して回す (check スクリプト群の定石)。
"""

import json
import pathlib
import tempfile
import unittest

from ops.stage3 import readiness as rd

REPO = pathlib.Path(__file__).resolve().parents[2]
LEDGER = REPO / "ops" / "stage3" / "readiness.json"
README = REPO / "ops" / "stage3" / "README.md"


def criterion(**overrides) -> dict:
    """必須キーを全部持つ合成基準。テストで必要な所だけ壊す。"""
    base = {
        "id": "trifecta-separation-drill",
        "criterion": "分離プロファイルの drill 証跡がある",
        "threshold": "demo.json の 3 点が true",
        "current_value": "未整備",
        "evidence_path": "evidence.json",
        "pass": False,
    }
    base.update(overrides)
    return base


def doc_with(*criteria, verdict="blocked") -> dict:
    return {"verdict": verdict, "criteria": list(criteria)}


class TestValidateSchema(unittest.TestCase):
    def test_well_formed_doc_passes(self):
        doc = doc_with(criterion(), criterion(id="veto-channel-live"))
        self.assertEqual(rd.validate(doc), [])

    def test_non_object_is_rejected(self):
        self.assertNotEqual(rd.validate([]), [])
        self.assertNotEqual(rd.validate(None), [])

    def test_verdict_must_be_one_of_two_values(self):
        for verdict in ("blocked", "ready_for_announce_draft"):
            with self.subTest(verdict=verdict):
                doc = doc_with(criterion(id="restore-proven"), verdict=verdict)
                self.assertEqual(rd.validate(doc), [])
        doc = doc_with(criterion(id="restore-proven"), verdict="conditionally_ready")
        errors = rd.validate(doc)
        self.assertTrue(any("verdict" in e for e in errors), errors)

    def test_missing_required_key_is_rejected(self):
        broken = criterion()
        del broken["current_value"]
        errors = rd.validate(doc_with(broken))
        self.assertTrue(any("current_value" in e for e in errors), errors)

    def test_non_bool_pass_is_rejected(self):
        # pass は真偽値のみ。「たぶん true」のような文字列は schema 違反
        errors = rd.validate(doc_with(criterion(**{"pass": "yes"})))
        self.assertTrue(any("pass" in e for e in errors), errors)

    def test_empty_string_field_is_rejected(self):
        errors = rd.validate(doc_with(criterion(threshold="")))
        self.assertTrue(any("threshold" in e for e in errors), errors)

    def test_empty_criteria_is_rejected(self):
        errors = rd.validate(doc_with())
        self.assertTrue(any("criteria" in e for e in errors), errors)

    def test_duplicate_ids_are_rejected(self):
        doc = doc_with(criterion(), criterion())
        errors = rd.validate(doc)
        self.assertTrue(any("重複" in e for e in errors), errors)

    def test_missing_mandatory_perspective_is_reported(self):
        # 必須観点 5 つが id として揃わない台帳は、項目数を水増ししても落とす
        ids = ["p1", "p2", "p3", "p4", "p5"]
        absent = rd.missing_perspectives(doc_with(*[criterion(id=i) for i in ids]))
        self.assertEqual(absent, list(rd.MANDATORY_PERSPECTIVES))

    def test_all_five_perspectives_satisfy_the_requirement(self):
        doc = doc_with(*[criterion(id=i) for i in rd.MANDATORY_PERSPECTIVES])
        self.assertEqual(rd.missing_perspectives(doc), [])


class TestComputeVerdict(unittest.TestCase):
    def test_all_pass_is_ready_for_announce_draft(self):
        criteria = [
            criterion(id="trifecta-separation-drill", **{"pass": True}),
            criterion(id="veto-channel-live", **{"pass": True}),
        ]
        self.assertEqual(rd.compute_verdict(criteria), rd.READY_VERDICT)

    def test_single_failure_blocks(self):
        criteria = [criterion(**{"pass": True}), criterion(**{"pass": False})]
        self.assertEqual(rd.compute_verdict(criteria), rd.BLOCKED_VERDICT)

    def test_unknown_pass_blocks_fail_closed(self):
        # pass が欠けた基準 (None) は false 扱い。読めないものを元気気扱いにしない
        criteria = [{"id": "x", "pass": None}]
        self.assertEqual(rd.compute_verdict(criteria), rd.BLOCKED_VERDICT)

    def test_empty_criteria_blocks(self):
        self.assertEqual(rd.compute_verdict([]), rd.BLOCKED_VERDICT)


class TestMissingEvidence(unittest.TestCase):
    def run_in_tmp(self, doc):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "evidence.json").write_text("{}", encoding="utf-8")
            return rd.missing_evidence(doc, root=tmp)

    def test_existing_path_is_not_reported(self):
        missing = self.run_in_tmp(doc_with(criterion(evidence_path="evidence.json")))
        self.assertEqual(missing, [])

    def test_absent_path_is_reported(self):
        missing = self.run_in_tmp(doc_with(criterion(evidence_path="no/such/file.json")))
        self.assertEqual(missing, ["no/such/file.json"])

    def test_extra_keys_do_not_break_validation(self):
        # 備考キーの追加 (例: note) を許す — schema は必須キーの下限しか決めない
        rich = criterion(note="補足")
        self.assertEqual(rd.validate(doc_with(rich)), [])


class TestRealLedger(unittest.TestCase):
    """実台帳への検査。ここが落ちたら readiness.json 自体の形が崩れている。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(LEDGER.read_text(encoding="utf-8"))

    def test_schema_is_valid(self):
        self.assertEqual(rd.validate(self.doc), [])

    def test_covers_all_mandatory_perspectives(self):
        self.assertEqual(rd.missing_perspectives(self.doc), [])

    def test_has_at_least_five_criteria(self):
        self.assertGreaterEqual(len(self.doc["criteria"]), 5)

    def test_every_evidence_path_exists(self):
        # 存在検査を通すためのダミーファイルを作るのは証拠の捏造なので、
        # 不在なら台帳側を正直に直す (README「台帳の直し方」参照)
        self.assertEqual(rd.missing_evidence(self.doc, root=REPO), [])

    def test_verdict_matches_the_rule(self):
        self.assertEqual(
            self.doc["verdict"], rd.compute_verdict(self.doc["criteria"])
        )

    def test_readme_explains_verdict_and_each_criterion(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("verdict", text)
        for c in self.doc["criteria"]:
            with self.subTest(criterion=c["id"]):
                self.assertIn(c["id"], text)


if __name__ == "__main__":
    unittest.main()
