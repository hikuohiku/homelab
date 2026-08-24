"""spec の読み先の契約テスト。

通常の spec は main の ops/projects/archive.jsonl にある (ブランチからは
改竄できない)。コアが即時 dispatch したプロジェクト (P-9NNN、設計 rev3
Phase D) は main の PR/CI/merge を経由しないので、heart が Job の env に
載せた spec を読む。**env は Job の spec で固定されるので、改竄不能という
性質は変わらない。**
"""

import json
import unittest
from unittest import mock

from ops.runner.runner import Runner


class FakeRunner:
    """Runner.load_spec / spec_from_env を素で呼ぶための最小の器。"""

    def __init__(self, repo_dir, project_id):
        self.repo_dir = repo_dir
        self.project_id = project_id

    load_spec = Runner.load_spec
    spec_from_env = Runner.spec_from_env


class LoadSpec(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "ops" / "projects").mkdir(parents=True)
        self.archive = self.repo / "ops" / "projects" / "archive.jsonl"

    def runner(self, project_id="P-9000"):
        return FakeRunner(self.repo, project_id)

    def env_spec(self, **kw):
        base = {"id": "P-9000", "title": "直す", "verify": ["test -f x"]}
        base.update(kw)
        return json.dumps(base, ensure_ascii=False)

    def test_archive_wins_when_it_has_the_spec(self):
        self.archive.write_text(
            json.dumps({"id": "P-0001", "adopted": True, "title": "台帳から"}) + "\n"
        )
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec(id="P-0001")}):
            got = self.runner("P-0001").load_spec()
        self.assertEqual(got["title"], "台帳から")

    def test_env_spec_is_used_when_archive_has_none(self):
        self.archive.write_text(
            json.dumps({"id": "P-0001", "adopted": True, "title": "別の案"}) + "\n"
        )
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec()}):
            got = self.runner().load_spec()
        self.assertEqual(got["title"], "直す")

    def test_env_spec_is_used_when_archive_is_missing(self):
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec()}):
            got = self.runner().load_spec()
        self.assertEqual(got["verify"], ["test -f x"])

    def test_spec_for_another_project_is_refused(self):
        """取り違えた spec で実装する方が、spec_error で止まるより高くつく。"""
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec(id="P-9999")}):
            got = self.runner().load_spec()
        self.assertEqual(got, {})

    def test_broken_env_spec_is_refused(self):
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": "{not json"}):
            self.assertEqual(self.runner().load_spec(), {})

    def test_no_env_spec_is_still_empty(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.runner().load_spec(), {})


if __name__ == "__main__":
    unittest.main()
