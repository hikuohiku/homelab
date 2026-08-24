"""clone の打ち方を固定する。**ネットワークに出ない** — コマンド列だけを見る。

素の clone はこの repo で 65s / 124MB (2026-08-24 実測、状態ブランチ 4 本の履歴)。
採択ゲートの clone はこれで 120s 上限を越えて落ち、P-0341 を stalled に落とした。
blobless (2s / 9.2MB) に戻さない歯止めとしてここに置く。
"""

import unittest
from pathlib import Path
from unittest import mock

from ops.heart import adoptgate, config, gitutil, spawn

REPO = Path(__file__).resolve().parents[3]
URL = "https://github.com/example/repo.git"


class TestCloneArgs(unittest.TestCase):
    def test_blobless_by_default(self):
        args = gitutil.clone_args(URL, "/work/repo")
        self.assertIn("--filter=blob:none", args)
        self.assertEqual(args[-2:], [URL, "/work/repo"])

    def test_not_shallow(self):
        """shallow は push と merge-base で罠を踏む。深さは削らない。"""
        args = gitutil.clone_args(URL, "/work/repo")
        self.assertFalse([a for a in args if a.startswith("--depth")])

    def test_all_refs_unless_asked(self):
        """--single-branch は remote.origin.fetch を 1 本に固定し、以後 fetch しても
        origin/ops-state が生えない (P-0014)。既定では付けない。"""
        self.assertNotIn("--single-branch", gitutil.clone_args(URL, "/work/repo"))

    def test_single_branch_when_asked(self):
        args = gitutil.clone_args(URL, "/state", branch="ops-state", single_branch=True)
        self.assertIn("--single-branch", args)
        self.assertEqual(args[args.index("--branch") + 1], "ops-state")
        self.assertIn("--filter=blob:none", args)


class TestCloneCallSites(unittest.TestCase):
    """clone を打つ経路が clone_args を通っていること。"""

    def calls(self, fn):
        with mock.patch.object(gitutil, "run") as run:
            fn()
        return [c.args[0] for c in run.call_args_list]

    def test_sync_main_clones_blobless(self):
        with mock.patch.object(Path, "is_dir", return_value=False), \
                mock.patch.object(Path, "mkdir"):
            args = self.calls(lambda: gitutil.sync_main("/work/repo", URL))
        self.assertEqual(args[0][:3], ["clone", "--quiet", "--filter=blob:none"])
        self.assertNotIn("--single-branch", args[0])

    def test_state_branch_clones_blobless_single(self):
        with mock.patch.object(Path, "is_dir", return_value=False), \
                mock.patch.object(Path, "mkdir"):
            args = self.calls(
                lambda: gitutil.sync_state_branch("/work/state", URL, "ops-state")
            )
        self.assertIn("--filter=blob:none", args[0])
        self.assertIn("--single-branch", args[0])

    def test_adopt_gate_clones_blobless_with_all_refs(self):
        """採択ゲートは origin/main と origin/ops-state の両方が見えること
        (verify に `git show origin/ops-state:projects.json` を持つ spec がある)。"""
        args = self.calls(lambda: adoptgate.clone_fresh(URL, "/tmp/x/repo"))
        self.assertIn("--filter=blob:none", args[0])
        self.assertNotIn("--single-branch", args[0])
        self.assertFalse([a for a in args[0] if a.startswith("--depth")])

    def test_runner_job_bootstrap_clones_blobless(self):
        """全 Job が着手前にこの clone を払う。"""
        cfg = config.load(REPO, env={"AUTOPILOT_IMAGE": "example.invalid/a:test"})
        job = spawn.build_job(cfg, "runner", project_id="P-0001")
        bootstrap = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        self.assertIn("git clone --quiet --filter=blob:none", bootstrap)


if __name__ == "__main__":
    unittest.main()
