"""台帳 (archive.jsonl) への書き込みが止まったこと (設計 state-out-of-git 4b-2b)。

固定するもの:

1. curriculum Job は台帳 PR を作らない (`fix_to_archive` はもう無い)
2. その代わり全案が result.json の `proposal_records` に載る。**棄却案の
   reject_reason / improve_hint が生成役へ戻る唯一の経路がここに移った**
3. heart はそれを読んで `state: rejected` の Project CR にする。
   取り込みは `consume_curriculum` が result.json を退避する **前** に走る
4. 台帳は読むだけ。過去分の埋め直しは続く (後方互換)
"""

import json
import tempfile
import unittest
from pathlib import Path

from ops.heart import facts, projectcr
from ops.runner import runner as runner_mod

REPO = Path(__file__).resolve().parents[3]
NS = "autopilot"


class TheCurriculumJobNoLongerWritesTheLedger(unittest.TestCase):
    def test_the_ledger_pr_helper_is_gone(self):
        """残っていると「まだ書ける」経路が残る。名前で固定する。"""
        self.assertFalse(hasattr(runner_mod.Runner, "fix_to_archive"))
        self.assertFalse(hasattr(runner_mod.Runner, "archive_backfill_records"))

    def test_the_runner_source_does_not_append_to_the_ledger(self):
        source = (REPO / "ops" / "runner" / "runner.py").read_text()
        self.assertNotIn("projects\" / \"archive.jsonl", source)
        self.assertNotIn("ARCHIVE_BACKFILL_JSON", source)

    def test_all_proposals_ride_on_the_result(self):
        """採択も棄却も 1 つの配列に載る (build_archive_records の出力そのまま)。"""
        proposals = {"proposals": [
            {"id": "P-9001", "title": "採る", "verify": ["true"]},
            {"id": "P-9002", "title": "落とす", "verify": ["true"]},
        ]}
        adopted = {"adopted": [{"id": "P-9001"}],
                   "scores": [{"id": "P-9002", "reject_reason": "既出",
                               "improve_hint": "別の切り口で"}]}
        records = runner_mod.build_archive_records(proposals, adopted)
        by_id = {r["id"]: r for r in records}
        self.assertTrue(by_id["P-9001"]["adopted"])
        self.assertFalse(by_id["P-9002"]["adopted"])
        self.assertEqual(by_id["P-9002"]["reject_reason"], "既出")
        self.assertEqual(by_id["P-9002"]["improve_hint"], "別の切り口で")


class HeartReadsTheProposalsFromTheResult(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.result = self.data_dir / "projects" / "system" / "result.json"
        self.result.parent.mkdir(parents=True)

    def write(self, doc):
        self.result.write_text(json.dumps(doc, ensure_ascii=False))

    def test_records_are_read(self):
        self.write({"state": "curriculum_done", "proposal_records": [
            {"id": "P-9002", "adopted": False, "reject_reason": "既出"},
        ]})
        self.assertEqual(
            [r["id"] for r in facts.load_proposal_records(self.data_dir)], ["P-9002"]
        )

    def test_a_missing_or_broken_result_is_empty_not_an_exception(self):
        """棄却案の取り込みのためにビートを落とさない。"""
        self.assertEqual(facts.load_proposal_records(self.data_dir), [])
        self.result.write_text("{not json")
        self.assertEqual(facts.load_proposal_records(self.data_dir), [])
        self.write({"state": "curriculum_done"})
        self.assertEqual(facts.load_proposal_records(self.data_dir), [])

    def test_rejected_records_become_rejected_crs(self):
        records = [
            {"id": "P-9001", "adopted": True, "title": "採った"},
            {"id": "P-9002", "adopted": False, "title": "落ちた",
             "reject_reason": "既出", "improve_hint": "別の切り口で"},
        ]
        crs = projectcr.plan_rejected(records, [], NS, live_ids=set())
        self.assertEqual([c["metadata"]["name"] for c in crs], ["p-9002"])
        spec = crs[0]["spec"]
        self.assertEqual(spec["state"], "rejected")
        self.assertEqual(crs[0]["metadata"]["labels"]["lifecycle"], "terminal")
        # 教師信号が spec.spec に載っている。ここが痩せると同型再提案が常態化する
        self.assertEqual(spec["spec"]["reject_reason"], "既出")
        self.assertEqual(spec["spec"]["improve_hint"], "別の切り口で")

    def test_a_live_project_is_never_overwritten_by_a_rejection(self):
        """同じ id が doc に居るなら、走行中の状態を棄却で潰さない。"""
        records = [{"id": "P-9001", "adopted": False, "title": "落ちた"}]
        self.assertEqual(
            projectcr.plan_rejected(records, [], NS, live_ids={"P-9001"}), []
        )


class TheLedgerIsStillReadable(unittest.TestCase):
    """後方互換。過去 250 件超の埋め直しは台帳からしか来ない。"""

    def test_the_real_ledger_still_parses(self):
        records = facts.load_archive_records(REPO)
        self.assertGreater(len(records), 300, "台帳が読めていない")

    def test_the_ledger_file_is_still_there(self):
        """**この段では消さない**。実物の削除は Phase 7。"""
        self.assertTrue((REPO / "ops" / "projects" / "archive.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
