"""git への書き込みを止めたこと (設計 state-out-of-git 4b-2b) の不変条件。

守りたいのは 4 つ。どれも「静かに記録が消える」への歯止め:

1. **ビートが git に 1 度も書かない。** commit も push も打たない
2. **CR に取りこぼしがある間は doc を PVC へ移さない。** 移した瞬間、CR に
   ならなかったプロジェクトは restic のバックアップにも乗らなくなる
3. **棄却案の死因が消えない。** 台帳 PR を止めた代わりに、result.json の
   棄却行を PVC へ写し、そこから Project CR にする
4. **PVC ごと失っても空の doc で走り出さない。** CR から復元する
"""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.heart import facts, gitutil, heart as heart_module, projectcr, spawn
from ops.heart.heart import Heart
from ops.heart.notify import Notifier

REPO = Path(__file__).resolve().parents[3]

EMPTY_DOC = {"version": 1, "projects": [], "chores": []}


def project_entry(pid, state="active"):
    return {
        "id": pid, "title": "t", "state": state,
        "branch": f"project/{pid.lower()}", "irreversible": False,
        "capabilities": [], "budget": {"used_tokens": 0}, "created": "2026-08-24",
    }


class FakeK8s:
    """CR を辞書で持つだけの k8s。ここでの関心は「何が書かれたか」だけ。"""

    def __init__(self, names=()):
        self.items = {
            name: {"metadata": {"name": name}, "spec": {"id": name.upper()}}
            for name in names
        }
        self.applied = []
        self.leases = []

    def list_custom(self, api_version, namespace, plural, label_selector=None):
        items = list(self.items.values())
        if label_selector == projectcr.NOT_REJECTED_SELECTOR:
            items = [
                i for i in items if (i.get("spec") or {}).get("state") != "rejected"
            ]
        return items

    def apply_custom(self, api_version, namespace, plural, name, body):
        self.applied.append(name)
        self.items[name] = body
        return body

    def apply_lease(self, namespace, name, body):
        self.leases.append(body)
        return body


class BeatCase(unittest.TestCase):
    """ビートを 1 回だけ回すための土台。外部依存はすべて潰す。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        env = mock.patch.dict(
            os.environ,
            {"HEART_DATA_DIR": str(self.data_dir), "HEART_MODE": "shadow"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.h = Heart(REPO)

    def beat(self, k8s, n=1, git_run=None, curriculum=None, archive=()):
        patches = [
            mock.patch.object(Heart, "k8s_client", lambda self: k8s),
            mock.patch.object(facts, "load_health", lambda *a, **k: ([], True, None)),
            mock.patch.object(facts, "load_adopted_specs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_jobs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_prs", lambda *a, **k: ({}, {})),
            mock.patch.object(facts, "collect_curriculum", lambda *a, **k: curriculum),
            mock.patch.object(facts, "collect_critic", lambda *a, **k: None),
            mock.patch.object(
                facts, "load_archive_records", lambda *a, **k: list(archive)
            ),
            mock.patch.object(
                facts, "collect_feedback",
                lambda gh, rd, cursors, *a, **k: (
                    [], [], False, [], False, [], [], dict(cursors)
                ),
            ),
            mock.patch.object(spawn, "create", lambda *a, **k: "job-dummy"),
            mock.patch.object(Notifier, "send", lambda *a, **k: None),
            mock.patch.object(Notifier, "flush_outbox", lambda *a, **k: None),
        ]
        if git_run is None:
            patches.append(mock.patch.object(gitutil, "sync_main", lambda *a, **k: None))
        else:
            patches.append(mock.patch.object(gitutil, "run", git_run))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.h.beat(n)


class BeatDoesNotWriteToGit(BeatCase):
    def test_no_git_write_command_is_issued(self):
        """ビートが打つ git は読みだけ。commit も push も 1 度も出ない。"""
        calls = []

        def record(args, cwd=None, check=True):
            calls.append(list(args))
            return ""

        self.h.docs.save_projects(dict(EMPTY_DOC))
        self.beat(FakeK8s(), git_run=record)
        self.assertTrue(calls, "git を 1 度も呼んでいない (patch が効いていない)")
        for args in calls:
            self.assertNotIn(args[0], ("push", "commit", "add"), f"git {args} を打った")

    def test_the_push_helpers_are_gone(self):
        """関数ごと消してある。規律ではなく口が無いことで守る。"""
        self.assertFalse(hasattr(gitutil, "commit_and_push_state"))
        self.assertFalse(hasattr(gitutil, "sync_state_branch"))
        # git の書き込み動詞を組み立てる箇所が残っていないこと
        source = Path(gitutil.__file__).read_text()
        for verb in ('"push"', '"commit"', '"add"'):
            self.assertNotIn(verb, source)

    def test_state_branch_is_not_configured_anymore(self):
        self.assertFalse(hasattr(self.h.cfg, "state_branch"))


class MigrationGuardsTheRecord(BeatCase):
    """CR に取りこぼしがある間は doc を移さない。"""

    def seed_legacy(self, *pids):
        from ops.heart.statefiles import StateFiles

        legacy = StateFiles(self.h.state_dir)
        doc = dict(EMPTY_DOC)
        doc["projects"] = [project_entry(pid) for pid in pids]
        legacy.save_projects(doc)
        (self.h.state_dir / "metrics.jsonl").write_text('{"beat": 1}\n')
        return legacy

    def test_migrates_when_every_id_has_a_cr(self):
        self.seed_legacy("P-0001")
        k8s = FakeK8s(names=["p-0001", "p-0002"])
        self.beat(k8s, archive=[{"id": "P-0002", "adopted": False}])
        self.assertTrue((self.h.doc_dir / "projects.json").exists())
        self.assertEqual(
            [p["id"] for p in self.h.docs.load_projects()["projects"]], ["P-0001"]
        )
        # 経過措置で置いていた指標はファイルごと消える
        self.assertFalse((self.h.state_dir / "metrics.jsonl").exists())

    def test_does_not_migrate_when_a_project_has_no_cr(self):
        self.seed_legacy("P-0001")
        self.beat(FakeK8s())  # CR は 1 件も無い
        self.assertFalse((self.h.doc_dir / "projects.json").exists())
        # doc は移行前の置き場で読み書きされ続ける (記録は失われない)
        from ops.heart.statefiles import StateFiles

        self.assertEqual(
            [p["id"] for p in StateFiles(self.h.state_dir).load_projects()["projects"]],
            ["P-0001"],
        )

    def test_does_not_migrate_when_an_archived_id_has_no_cr(self):
        """台帳の棄却案が CR になっていないときも見送る (死因が消えるため)。"""
        self.seed_legacy("P-0001")
        self.beat(
            FakeK8s(names=["p-0001"]),
            archive=[{"id": "P-0002", "adopted": False}],
        )
        self.assertFalse((self.h.doc_dir / "projects.json").exists())

    def test_the_gap_is_reported_to_a_human(self):
        self.h.cfg.mode = "active"
        sent = []
        self.h.note_parity_gap(["p-0002"], _Notifier(sent), None)
        self.assertEqual(len(sent), 1)
        self.assertIn("p-0002", sent[0][1])

    def test_unreadable_crs_do_not_count_as_complete(self):
        """CR を読めないビートを「揃っている」に倒さない (fail-closed)。"""
        self.seed_legacy("P-0001")

        class Broken(FakeK8s):
            def list_custom(self, *a, **k):
                raise RuntimeError("api down")

        self.beat(Broken())
        self.assertFalse((self.h.doc_dir / "projects.json").exists())


class _Notifier:
    def __init__(self, sink):
        self.sink = sink

    def send(self, ntype, text, now=None):
        self.sink.append((ntype, text))


class RejectedProposalsSurvive(BeatCase):
    """棄却案の死因は git を離れても消えない。"""

    CUR = {
        "state": "curriculum_done",
        "at": "2026-08-25T00:00:00Z",
        "adopted_specs": [],
        "records": [
            {"id": "P-9001", "title": "落ちた案", "adopted": False,
             "reject_reason": "同型の再提案", "improve_hint": "別の細胞で出す"},
        ],
    }

    def test_rejected_records_reach_a_cr_through_the_pvc_ledger(self):
        self.h.docs.save_projects(dict(EMPTY_DOC))
        k8s = FakeK8s()
        self.beat(k8s, curriculum=self.CUR)
        # 1: PVC の台帳に落ちている (result.json が退避された後も残る)
        ledger = self.h.work.read_jsonl(heart_module.REJECTED_LEDGER_FILE)
        self.assertEqual([r["id"] for r in ledger], ["P-9001"])
        # 2: 同じビートで CR になっている
        self.assertIn("p-9001", k8s.applied)
        cr = k8s.items["p-9001"]
        self.assertEqual(cr["spec"]["state"], "rejected")
        self.assertEqual(cr["spec"]["spec"]["reject_reason"], "同型の再提案")

    def test_the_ledger_is_emptied_once_the_cr_exists(self):
        """CR になった行だけを落とす。台帳は毎ビート読むので伸ばさない。"""
        self.h.docs.save_projects(dict(EMPTY_DOC))
        k8s = FakeK8s()
        self.beat(k8s, curriculum=self.CUR)
        # 1 ビート目は apply した直後なので残る (存在を確かめてから消す)
        self.assertEqual(
            len(self.h.work.read_jsonl(heart_module.REJECTED_LEDGER_FILE)), 1
        )
        self.beat(k8s, n=2, curriculum=self.CUR)
        self.assertEqual(self.h.work.read_jsonl(heart_module.REJECTED_LEDGER_FILE), [])

    def test_the_ledger_keeps_records_whose_cr_never_landed(self):
        """CR に書けなかった行は残す — 消したら死因ごと消える。"""
        self.h.docs.save_projects(dict(EMPTY_DOC))

        class Broken(FakeK8s):
            def apply_custom(self, *a, **k):
                raise RuntimeError("書けない")

        k8s = Broken()
        self.beat(k8s, curriculum=self.CUR)
        self.beat(k8s, n=2, curriculum=self.CUR)
        self.assertEqual(
            [r["id"] for r in self.h.work.read_jsonl(heart_module.REJECTED_LEDGER_FILE)],
            ["P-9001"],
        )

    def test_the_frozen_git_ledger_is_still_read(self):
        """2026-08-24 までの死因は今も archive.jsonl にしか無い。"""
        self.h.docs.save_projects(dict(EMPTY_DOC))
        k8s = FakeK8s()
        self.beat(k8s, archive=[{"id": "P-0002", "adopted": False, "title": "古い案"}])
        self.assertIn("p-0002", k8s.applied)


class DocSurvivesAnEmptyPvc(BeatCase):
    def test_doc_is_restored_from_the_crs(self):
        k8s = FakeK8s()
        k8s.items["p-0001"] = {
            "metadata": {"name": "p-0001"}, "spec": project_entry("P-0001"),
        }
        with mock.patch.object(Heart, "k8s_client", lambda self: k8s):
            doc = self.h.load_doc()
        self.assertEqual([p["id"] for p in doc["projects"]], ["P-0001"])

    def test_an_unreadable_cr_list_raises_instead_of_returning_empty(self):
        """空の doc で走り出すと、その save が「1 件も無い」を正にしてしまう。"""

        class Broken(FakeK8s):
            def list_custom(self, *a, **k):
                raise RuntimeError("api down")

        with mock.patch.object(Heart, "k8s_client", lambda self: Broken()):
            with self.assertRaises(RuntimeError):
                self.h.load_doc()


class DashboardReadsTheClusterNotGit(unittest.TestCase):
    """ダッシュボードが git を触らないこと (読み先は Lease と /healthz)。"""

    SOURCE = REPO / "apps" / "ops-dashboard" / "app" / "src" / "lib" / "ops-state.ts"

    def test_no_git_command_is_left_in_the_dashboard(self):
        text = self.SOURCE.read_text()
        for needle in ("execFile", "ops-state", "git("):
            self.assertNotIn(needle, text, f"ダッシュボードに {needle} が残っている")

    def test_it_reads_the_lease_and_healthz(self):
        text = self.SOURCE.read_text()
        self.assertIn("coordination.k8s.io", text)
        self.assertIn("/healthz", text)

    def test_the_dashboard_may_read_the_lease(self):
        rbac = (REPO / "apps" / "autopilot" / "rbac.yaml").read_text()
        binding = rbac.split("name: heart-lease-reader")[-1]
        self.assertIn("name: ops-dashboard", binding)

    def test_the_network_policy_lets_the_dashboard_reach_the_gate(self):
        netpol = (REPO / "apps" / "autopilot" / "heart-service.yaml").read_text()
        self.assertIn("app: ops-dashboard", netpol)


class NothingWritesTheGitLedgerAnymore(unittest.TestCase):
    def test_the_runner_has_no_archive_pr_path(self):
        source = (REPO / "ops" / "runner" / "runner.py").read_text()
        self.assertNotIn("fix_to_archive", source)
        self.assertNotIn("ARCHIVE_BACKFILL_JSON", source)
        self.assertNotIn('"ops", "projects", "archive.jsonl"', source)

    def test_the_ledger_file_is_still_there(self):
        """**消していない。** 過去の死因の実体で、戻せる状態を保つ。"""
        path = REPO / "ops" / "projects" / "archive.jsonl"
        self.assertTrue(path.exists())
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        self.assertGreater(len(rows), 300)


if __name__ == "__main__":
    unittest.main()
