import tempfile
import unittest
from pathlib import Path

from ops.heart import statefiles


def project(**kw):
    base = {
        "id": "P-0001",
        "title": "t",
        "state": "active",
        "branch": "project/p-0001",
        "irreversible": False,
        "capabilities": [],
        "budget": {"used_tokens": 0},
        "created": "2026-08-07",
    }
    base.update(kw)
    return base


class TestValidateProjects(unittest.TestCase):
    def test_valid_doc_passes(self):
        doc = {"version": 1, "projects": [project()], "chores": []}
        self.assertEqual(statefiles.validate_projects(doc), [])

    def test_missing_field(self):
        p = project()
        del p["budget"]
        errors = statefiles.validate_projects({"projects": [p]})
        self.assertTrue(any("budget" in e for e in errors))

    def test_bad_state(self):
        errors = statefiles.validate_projects({"projects": [project(state="running")]})
        self.assertTrue(any("state" in e for e in errors))

    def test_duplicate_id(self):
        errors = statefiles.validate_projects(
            {"projects": [project(), project()]}
        )
        self.assertTrue(any("重複" in e for e in errors))

    def test_branch_prefix_enforced_for_live_projects(self):
        errors = statefiles.validate_projects(
            {"projects": [project(branch="autopilot/x")]}
        )
        self.assertTrue(any("project/" in e for e in errors))

    def test_save_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            sf = statefiles.StateFiles(d)
            with self.assertRaises(ValueError):
                sf.save_projects({"projects": [project(state="bogus")]})

    def test_roundtrip_and_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            sf = statefiles.StateFiles(d)
            doc = {"version": 1, "projects": [project()], "chores": []}
            sf.save_projects(doc)
            self.assertEqual(sf.load_projects()["projects"][0]["id"], "P-0001")
            sf.append_jsonl("metrics.jsonl", {"a": 1})
            sf.append_jsonl("metrics.jsonl", {"a": 2})
            self.assertEqual([r["a"] for r in sf.read_jsonl("metrics.jsonl")], [1, 2])
            sf.rewrite_jsonl("metrics.jsonl", [{"a": 3}])
            self.assertEqual([r["a"] for r in sf.read_jsonl("metrics.jsonl")], [3])


if __name__ == "__main__":
    unittest.main()
