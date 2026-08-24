"""台帳 (archive.jsonl) の遅延追記の契約テスト (設計 rev3 D32)。

採択は ops-state の projects.json に載った時点で動き出し、台帳への追記は
次の curriculum Job の PR にまとめて乗る。heart が env で渡す spec を
runner がどう台帳の行に直すか — そこがここの範囲。

台帳の検査 (ops/validate.py check_projects_archive) を満たさない行を 1 行でも
混ぜると CI が赤になり、以後どの案も台帳に載らなくなる。落とす方を選ぶ。
"""

import json
import unittest
from unittest import mock

from ops.runner.runner import Runner


class FakeRunner:
    archive_backfill_records = Runner.archive_backfill_records


SPEC = {"id": "P-9001", "title": "コアが即時 dispatch した案", "why": "なぜ",
        "dod": "どこまで", "verify": ["test -f x"], "cell": ["self", "feature"],
        "irreversible": False, "capabilities": [], "touches_apps": False,
        "confidence": "unsure"}


def backfill(*specs):
    return {"ARCHIVE_BACKFILL_JSON": json.dumps(list(specs), ensure_ascii=False)}


class ArchiveBackfill(unittest.TestCase):
    def records(self, env):
        with mock.patch.dict("os.environ", env, clear=True):
            return FakeRunner().archive_backfill_records()

    def test_spec_becomes_an_adopted_ledger_row(self):
        recs = self.records(backfill(SPEC))
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["adopted"])
        self.assertEqual(recs[0]["id"], "P-9001")
        self.assertIn("proposed_at", recs[0])

    def test_no_backfill_is_empty(self):
        self.assertEqual(self.records({}), [])
        self.assertEqual(self.records({"ARCHIVE_BACKFILL_JSON": ""}), [])

    def test_broken_json_is_ignored(self):
        self.assertEqual(self.records({"ARCHIVE_BACKFILL_JSON": "{not json"}), [])

    def test_rows_the_ledger_check_would_reject_are_dropped(self):
        """verify 空 / cell 不正 / irreversible 欠落 / id 形式違い は落とす。"""
        for bad in (
            {**SPEC, "verify": []},
            {**SPEC, "cell": ["self"]},
            {k: v for k, v in SPEC.items() if k != "irreversible"},
            {**SPEC, "id": "P-90001"},
        ):
            self.assertEqual(self.records(backfill(bad)), [], bad.get("id"))

    def test_good_rows_survive_a_bad_neighbour(self):
        recs = self.records(backfill({**SPEC, "verify": []}, SPEC))
        self.assertEqual([r["id"] for r in recs], ["P-9001"])


if __name__ == "__main__":
    unittest.main()
