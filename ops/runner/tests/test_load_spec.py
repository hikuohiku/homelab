"""spec の読み先の契約テスト。

**dispatch の正は ops-state ブランチの projects.json** (設計 rev3 D32)。
書き手は heart だけなので、runner のブランチからは改竄できない。読み先は
この順に落ちる:

  (1) ops-state の projects.json — 正
  (2) origin/main の ops/projects/archive.jsonl — 台帳 (後方互換)
  (3) Job の env (HEART_SPEC_JSON) — 即時 dispatch の走り出しの瞬間だけ

どれもプロジェクトブランチ上のファイルを読まない。ここが崩れると、worker が
自分のブランチに置いた偽 spec で自分を採点できるようになる。
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.runner.runner import Runner


class FakeGh:
    """ops-state の projects.json を返す最小の GitHub クライアント。"""

    def __init__(self, doc=None, error=None):
        self.doc = doc
        self.error = error
        self.calls = []

    def file_at_ref(self, path, ref):
        self.calls.append((path, ref))
        if self.error:
            raise self.error
        return None if self.doc is None else json.dumps(self.doc, ensure_ascii=False)


class FakeRunner:
    """Runner の spec 読み出しだけを素で呼ぶための器。"""

    def __init__(self, repo_dir, project_id, gh):
        self.repo_dir = repo_dir
        self.project_id = project_id
        self.gh = gh

    load_spec = Runner.load_spec
    spec_from_ops_state = Runner.spec_from_ops_state
    spec_from_archive = Runner.spec_from_archive
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

    # --- 器 ---
    def fix_main(self, *records):
        """origin/main に archive.jsonl を固定する (runner の clone と同じ形)。"""
        git(["init", "--quiet", "-b", "main"], self.repo)
        git(["config", "user.email", "t@example.com"], self.repo)
        git(["config", "user.name", "t"], self.repo)
        self.archive.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        )
        git(["add", "-A"], self.repo)
        git(["commit", "--quiet", "-m", "archive"], self.repo)
        git(["update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)

    def runner(self, project_id="P-9000", doc=None, error=None):
        return FakeRunner(self.repo, project_id, FakeGh(doc, error))

    def state_doc(self, **kw):
        spec = {"id": "P-0001", "title": "ops-state から", "verify": ["test -f x"]}
        spec.update(kw.pop("spec", {}))
        project = {"id": spec["id"], "state": "proposed", "spec": spec}
        project.update(kw)
        return {"version": 1, "projects": [project]}

    def env_spec(self, **kw):
        base = {"id": "P-9000", "title": "env から", "verify": ["test -f x"]}
        base.update(kw)
        return json.dumps(base, ensure_ascii=False)

    # --- (1) ops-state が正 ---
    def test_ops_state_is_the_source_of_truth(self):
        self.fix_main({"id": "P-0001", "adopted": True, "title": "台帳から",
                       "verify": ["true"]})
        r = self.runner("P-0001", doc=self.state_doc())
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec(id="P-0001")}):
            got = r.load_spec()
        self.assertEqual(got["title"], "ops-state から")
        self.assertEqual(r.gh.calls, [("projects.json", "ops-state")])

    def test_spec_for_another_project_in_ops_state_is_refused(self):
        """id の食い違う spec は受け取らない (取り違えた実装の方が高くつく)。"""
        doc = self.state_doc()
        doc["projects"][0]["spec"]["id"] = "P-0002"
        self.assertEqual(self.runner("P-0001", doc=doc).load_spec(), {})

    # --- (2) 後方互換: 台帳にしかない過去の spec ---
    def test_archive_is_read_when_ops_state_has_no_spec(self):
        """この変更より前に登録された走行中プロジェクト (spec 欄が無い)。"""
        self.fix_main({"id": "P-0001", "adopted": True, "title": "台帳から",
                       "verify": ["true"]})
        doc = self.state_doc()
        del doc["projects"][0]["spec"]
        self.assertEqual(
            self.runner("P-0001", doc=doc).load_spec()["title"], "台帳から"
        )

    def test_archive_is_read_when_project_is_not_in_ops_state(self):
        self.fix_main({"id": "P-0001", "adopted": True, "title": "台帳から",
                       "verify": ["true"]})
        got = self.runner("P-0001", doc={"version": 1, "projects": []}).load_spec()
        self.assertEqual(got["title"], "台帳から")

    def test_ops_state_unreadable_falls_back_to_archive(self):
        """API が読めないビートでも走り出せる (fail-open ではなく読み先の縮退)。"""
        self.fix_main({"id": "P-0001", "adopted": True, "title": "台帳から",
                       "verify": ["true"]})
        got = self.runner("P-0001", error=OSError("boom")).load_spec()
        self.assertEqual(got["title"], "台帳から")

    # --- 改竄耐性 ---
    def test_spec_on_the_project_branch_is_ignored(self):
        """プロジェクトブランチに置いた偽 spec を読まない (回帰テスト)。

        worker はブランチの中身を自由に書ける。作業ツリーの archive.jsonl と
        projects.json を偽装しても、読まれるのは ops-state と origin/main。
        """
        self.fix_main({"id": "P-0001", "adopted": True, "title": "台帳から",
                       "verify": ["true"]})
        git(["checkout", "--quiet", "-b", "project/p-0001"], self.repo)
        self.archive.write_text(
            json.dumps({"id": "P-0001", "adopted": True, "title": "偽物",
                        "verify": ["true"]}) + "\n"
        )
        (self.repo / "projects.json").write_text(
            json.dumps({"projects": [{"id": "P-0001",
                                      "spec": {"id": "P-0001", "title": "偽物"}}]})
        )
        doc = self.state_doc()
        del doc["projects"][0]["spec"]
        self.assertEqual(
            self.runner("P-0001", doc=doc).load_spec()["title"], "台帳から"
        )

    # --- (3) env: 即時 dispatch の走り出し ---
    def test_env_spec_is_used_before_the_beat_writes_ops_state(self):
        """gate が Job を作る時点では ops-state にまだ載っていない。"""
        self.fix_main()
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec()}):
            got = self.runner(doc={"version": 1, "projects": []}).load_spec()
        self.assertEqual(got["verify"], ["test -f x"])

    def test_spec_for_another_project_in_env_is_refused(self):
        self.fix_main()
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": self.env_spec(id="P-9999")}):
            self.assertEqual(self.runner().load_spec(), {})

    def test_broken_env_spec_is_refused(self):
        self.fix_main()
        with mock.patch.dict("os.environ", {"HEART_SPEC_JSON": "{not json"}):
            self.assertEqual(self.runner().load_spec(), {})

    def test_nothing_anywhere_is_empty(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.runner().load_spec(), {})


if __name__ == "__main__":
    unittest.main()
