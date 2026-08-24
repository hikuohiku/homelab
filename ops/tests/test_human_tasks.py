"""ops/tools/human_tasks.py の parse を固定する (P-0272)。

リポジトリルートから `python3 -m unittest ops.tests.test_human_tasks -v`。
fixture は ops/tests/fixtures/human_tasks_seeds.md (実物の節構造を模した断片)。
TypeScript 側の mirror テスト (apps/ops-dashboard/app/tests/human-tasks.test.ts) が
**同じファイル**を読む。同入力 → 同出力を両言語で固定して drift を防ぐ。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ops.tools import human_tasks as ht

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "human_tasks_seeds.md"
TODAY = date(2026, 8, 24)


def load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class TestExtractSection(unittest.TestCase):
    def test_見出しの節だけを取り出す(self):
        section = ht.extract_section(load_fixture())
        self.assertIn("T-0107", section)
        self.assertIn("T-0148", section)
        # 節の外 (前の節・次の節) は入らない
        self.assertNotIn("T-0074", section)
        self.assertNotIn("次の節", section)

    def test_節が無ければ空文字列(self):
        self.assertEqual(ht.extract_section("# seeds\n\n- T-0107: 単なる bullet\n"), "")


class TestParseHumanTasks(unittest.TestCase):
    def test_bullet_T項目だけを文書順に抽出する(self):
        tasks = ht.parse_human_tasks(load_fixture(), {}, today=TODAY)
        # 番号付き行 (14./15.)、取り消し線 bullet (T-9001)、節の外 (T-0001/T-0074) は出ない
        self.assertEqual(
            [task["id"] for task in tasks],
            ["T-0107", "T-0140", "T-0141", "T-0148"],
        )
        for task in tasks:
            self.assertEqual(set(task), {"id", "title", "age_days"}, task)
            self.assertEqual(task["age_days"], 0)

    def test_createdをjoinして古い順に並べる(self):
        created_by_id = {
            "T-0107": "2026-07-01",  # 54 日
            "T-0141": "2026-08-06",  # 18 日
            "T-0140": "2026-08-20",  # 4 日
            # T-0148 は backlog に無い扱い → age_days 0 で最後
        }
        tasks = ht.parse_human_tasks(load_fixture(), created_by_id, today=TODAY)
        self.assertEqual(
            [(task["id"], task["age_days"]) for task in tasks],
            [("T-0107", 54), ("T-0141", 18), ("T-0140", 4), ("T-0148", 0)],
        )
        by_id = {task["id"]: task for task in tasks}
        self.assertEqual(by_id["T-0107"]["created"], "2026-07-01")
        self.assertNotIn("created", by_id["T-0148"])

    def test_backlogに無いidと変な日付はage_days_0(self):
        created_by_id = {"T-0107": "2026/08/06"}  # 形式違いは欠落扱い
        tasks = ht.parse_human_tasks(load_fixture(), created_by_id, today=TODAY)
        self.assertTrue(all(task["age_days"] == 0 for task in tasks))
        self.assertFalse(any("created" in task for task in tasks))

    def test_future_dateは0に切り詰め(self):
        tasks = ht.parse_human_tasks(load_fixture(), {"T-0107": "2099-01-01"}, today=TODAY)
        self.assertEqual(tasks[0]["age_days"], 0)

    def test_seedsが空でも落ちない(self):
        self.assertEqual(ht.parse_human_tasks("", {}, today=TODAY), [])


class TestCreatedIndex(unittest.TestCase):
    def test_backlog_jsonからid_to_createdを作る(self):
        backlog = json.dumps({"tasks": [
            {"id": "T-0107", "status": "needs-human", "created": "2026-08-06"},
            {"id": "T-0140", "created": "2026-08-06"},
        ]}, ensure_ascii=False)
        self.assertEqual(
            ht.created_index(backlog),
            {"T-0107": "2026-08-06", "T-0140": "2026-08-06"},
        )

    def test_壊れたjsonと想定外構造は空(self):
        self.assertEqual(ht.created_index("not json"), {})
        self.assertEqual(ht.created_index(json.dumps([1, 2, 3])), {})


class TestCli(unittest.TestCase):
    def test_out指定でjsonファイルを書く(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "human-tasks.json"
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "ops" / "tools" / "human_tasks.py"),
                 "--seeds", str(FIXTURE), "--backlog", str(REPO_ROOT / "ops" / "backlog.json"),
                 "--out", str(out)],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("tasks", doc)
            ids = [task["id"] for task in doc["tasks"]]
            for task_id in ("T-0107", "T-0140", "T-0141", "T-0148"):
                self.assertIn(task_id, ids)
                self.assertNotIn("T-9001", ids)
                self.assertNotIn("T-0001", ids)


if __name__ == "__main__":
    unittest.main()
