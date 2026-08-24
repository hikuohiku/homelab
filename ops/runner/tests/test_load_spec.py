"""spec の読み先の契約テスト。

**読み先は Job の env (HEART_SPEC_JSON) だけ** (設計 state-out-of-git 4b-2a)。
以前は ops-state の projects.json → main の archive.jsonl → env の 3 段だったが、
状態が git から Project CR へ出たので前の 2 つは畳んだ。CR を直接読む形は
採らない — worker Job は `automountServiceAccountToken: false` で、クラスタ API
に触れないこと自体が決定 #5 の境界だから (ops/heart/spawn.py)。

env は Job の spec に固定されるので、**runner のブランチからは書き換えられない**。
ここが崩れると、worker が自分のブランチに置いた偽 spec で自分を採点できる。
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.runner.runner import Runner


class FakeRunner:
    """Runner の spec 読み出しだけを素で呼ぶための器。"""

    def __init__(self, repo_dir, project_id):
        self.repo_dir = repo_dir
        self.project_id = project_id

    load_spec = Runner.load_spec
    spec_from_env = Runner.spec_from_env


def git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class LoadSpec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "ops" / "projects").mkdir(parents=True)
        self.archive = self.repo / "ops" / "projects" / "archive.jsonl"

    def runner(self, project_id="P-9000"):
        return FakeRunner(self.repo, project_id)

    def env_spec(self, **kw):
        base = {"id": "P-9000", "title": "env から", "verify": ["test -f x"]}
        base.update(kw)
        return json.dumps(base, ensure_ascii=False)

    def test_env_is_the_source_of_truth(self):
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec()}):
            got = self.runner().load_spec()
        self.assertEqual(got["title"], "env から")
        self.assertEqual(got["verify"], ["test -f x"])

    def test_spec_for_another_project_is_refused(self):
        """id の食い違う spec は受け取らない (取り違えた実装の方が高くつく)。"""
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec(id="P-9999")}):
            self.assertEqual(self.runner().load_spec(), {})

    def test_broken_env_spec_is_refused(self):
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": "{not json"}):
            self.assertEqual(self.runner().load_spec(), {})

    def test_nothing_anywhere_is_empty(self):
        """読めなければ空。呼び出し側 (mode_worker) が spec_error で止まる。"""
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.runner().load_spec(), {})

    def test_files_on_the_project_branch_are_ignored(self):
        """ブランチに偽の台帳・状態ファイルを置いても spec は変わらない (回帰)。

        worker はブランチの中身を自由に書ける。ここが読まれていないことが、
        読み先を env 1 本に畳んだ後も守られている性質。
        """
        git(["init", "--quiet", "-b", "main"], self.repo)
        git(["config", "user.email", "t@example.com"], self.repo)
        git(["config", "user.name", "t"], self.repo)
        self.archive.write_text(
            json.dumps({"id": "P-9000", "adopted": True, "title": "偽物",
                        "verify": ["true"]}) + "\n"
        )
        (self.repo / "projects.json").write_text(
            json.dumps({"projects": [{"id": "P-9000",
                                      "spec": {"id": "P-9000", "title": "偽物"}}]})
        )
        git(["add", "-A"], self.repo)
        git(["commit", "--quiet", "-m", "fake"], self.repo)
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec()}):
            self.assertEqual(self.runner().load_spec()["title"], "env から")


if __name__ == "__main__":
    unittest.main()
