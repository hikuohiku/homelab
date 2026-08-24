"""読み手を Project CR に切り替えた契約 (設計 state-out-of-git 4b-2a)。

固定するもの:

1. **git 版と CR 版が同じ答えを返す** — projects.json の 1 エントリを CR にして
   読み戻すと、reconcile に渡していた spec がそのまま出る
2. **live set は selector で切る** — 読み手は API 側で棄却案を落とす。
   終端 250 件超を毎回引いて手元で捨てる読み手を作らない
3. **棄却案を読むのは curriculum の入力だけ** — 他の読み手に混ぜると
   reconcile もダッシュボードも終端の山に埋まる
4. **CR が読めないときに黙って空を返さない** — ビートは進むが観測は
   「わからない」として扱い、curriculum は spawn しない (fail-closed)
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.heart import facts, projectcr, spawn, statefiles
from ops.heart.heart import Heart

REPO = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "projects.json"
NS = "autopilot"
TERMINAL = statefiles.TERMINAL_STATES


def fixture_doc():
    return json.loads(FIXTURE.read_text())


def crs(doc):
    return [projectcr.to_cr(p, NS, TERMINAL) for p in doc["projects"]]


class SpyK8s:
    """list_custom の引数を記録する k8s。"""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def list_custom(self, api_version, namespace, plural, label_selector=None):
        self.calls.append(label_selector)
        if label_selector == projectcr.NOT_REJECTED_SELECTOR:
            return [
                i for i in self.items
                if (i.get("spec") or {}).get("state") != "rejected"
            ]
        if label_selector == projectcr.LIVE_SELECTOR:
            return [
                i for i in self.items
                if (i.get("spec") or {}).get("state") not in TERMINAL
            ]
        return list(self.items)


class BrokenK8s:
    def list_custom(self, *a, **k):
        raise RuntimeError("k8s API 403: projects is forbidden")


class SameAnswerAsGit(unittest.TestCase):
    """CR に載せて読み戻しても、reconcile に渡る spec は変わらない。"""

    def test_real_doc_round_trips_through_the_cr(self):
        doc = fixture_doc()
        got = projectcr.adopted_specs_from_items(crs(doc))
        want = {
            p["spec"]["id"]: p["spec"]
            for p in doc["projects"]
            if isinstance(p.get("spec"), dict) and p["spec"].get("id")
        }
        self.assertEqual(got, want)
        self.assertTrue(want, "fixture に spec 持ちのプロジェクトが無い")

    def test_projects_round_trip(self):
        doc = fixture_doc()
        got = projectcr.projects_from_items(crs(doc))
        self.assertEqual(got, sorted(doc["projects"], key=lambda p: p["id"]))

    def test_the_nested_spec_is_not_mistaken_for_the_entry(self):
        """CR の spec が projects.json の 1 エントリ、その中の spec が立案時の spec。"""
        entry = {
            "id": "P-0001", "title": "エントリ", "state": "active",
            "branch": "project/p-0001", "irreversible": False, "capabilities": [],
            "budget": {"used_tokens": 3}, "created": "2026-08-24",
            "spec": {"id": "P-0001", "title": "立案時", "verify": ["false"]},
        }
        items = [projectcr.to_cr(entry, NS, TERMINAL)]
        self.assertEqual(projectcr.projects_from_items(items)[0]["title"], "エントリ")
        self.assertEqual(
            projectcr.adopted_specs_from_items(items)["P-0001"]["title"], "立案時"
        )


class RejectedStaysOutOfTheWorkingSet(unittest.TestCase):
    """棄却案は curriculum の入力にだけ出る。"""

    def rejected(self, pid="P-0900", **kw):
        rec = {"id": pid, "adopted": False, "title": "棄却された案",
               "reject_reason": "verify が骨抜き", "improve_hint": "測れる形に",
               "cell": ["self", "repair"], "proposed_at": "2026-08-20T00:00:00Z"}
        rec.update(kw)
        return projectcr.to_cr(
            projectcr.to_rejected_project(rec), NS, projectcr.REJECTED_TERMINAL
        )

    def test_adopted_specs_never_contain_rejected(self):
        items = crs(fixture_doc()) + [self.rejected()]
        self.assertNotIn("P-0900", projectcr.adopted_specs_from_items(items))

    def test_the_reader_asks_the_api_to_drop_them(self):
        k8s = SpyK8s(crs(fixture_doc()) + [self.rejected()])
        specs = facts.load_adopted_specs(k8s, NS)
        self.assertEqual(k8s.calls, [projectcr.NOT_REJECTED_SELECTOR])
        self.assertNotIn("P-0900", specs)

    def test_the_digest_keeps_the_teacher_signal(self):
        """reject_reason / improve_hint は判定の教師信号が生成に戻る唯一の経路。"""
        rows = projectcr.proposal_digest(crs(fixture_doc()) + [self.rejected()])
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id["P-0900"]["reject_reason"], "verify が骨抜き")
        self.assertEqual(by_id["P-0900"]["improve_hint"], "測れる形に")
        self.assertFalse(by_id["P-0900"]["adopted"])
        self.assertEqual(by_id["P-0900"]["cell"], ["self", "repair"])

    def test_the_digest_is_newest_first(self):
        """収束には時間がかかる。一番効く信号 (直近の死因) が先に読まれること。"""
        rows = projectcr.proposal_digest(
            [self.rejected("P-0900"), self.rejected("P-0910")]
        )
        self.assertEqual([r["id"] for r in rows], ["P-0910", "P-0900"])

    def test_the_digest_does_not_carry_the_whole_spec(self):
        rows = projectcr.proposal_digest([self.rejected(why="長い理由", dod="長い")])
        self.assertEqual(
            sorted(rows[0]),
            ["adopted", "cell", "id", "improve_hint", "proposed_at",
             "proposed_by", "reject_reason", "state", "title"],
        )


class LiveSelectorDropsTerminals(unittest.TestCase):
    def test_live_selector_excludes_terminal_states(self):
        doc = fixture_doc()
        k8s = SpyK8s(crs(doc))
        live = projectcr.projects_from_items(
            k8s.list_custom(projectcr.API_VERSION, NS, projectcr.PLURAL,
                            label_selector=projectcr.LIVE_SELECTOR)
        )
        self.assertTrue(live == [] or all(p["state"] not in TERMINAL for p in live))
        self.assertLess(len(live), len(doc["projects"]),
                        "fixture に終端が 1 件も無い (テストが効いていない)")


class CurriculumInput(unittest.TestCase):
    """立案役に渡す過去案のファイル。棄却案を含む唯一の読み手。"""

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

    def test_it_writes_every_proposal_including_rejected(self):
        rejected = projectcr.to_cr(
            projectcr.to_rejected_project(
                {"id": "P-0900", "adopted": False, "reject_reason": "同型"}
            ),
            NS, projectcr.REJECTED_TERMINAL,
        )
        doc = fixture_doc()
        k8s = SpyK8s(crs(doc) + [rejected])
        self.h.k8s = k8s
        env = self.h.prepare_curriculum_input()

        # selector を渡さない = 棄却案も含めて全件。ここだけがそうしてよい
        self.assertEqual(k8s.calls, [None])
        rows = [
            json.loads(line)
            for line in Path(env["PROPOSALS_HISTORY"]).read_text().splitlines()
        ]
        self.assertEqual(len(rows), len(doc["projects"]) + 1)
        self.assertIn("P-0900", [r["id"] for r in rows])
        self.assertEqual(rows[0]["id"], max(r["id"] for r in rows))

    def test_an_unreadable_cr_raises_instead_of_writing_an_empty_file(self):
        """空の台帳で立案させない (死因を知らない案が採択まで通る)。"""
        self.h.k8s = BrokenK8s()
        with self.assertRaises(RuntimeError):
            self.h.prepare_curriculum_input()
        self.assertFalse((self.data_dir / "curriculum" / "proposals.jsonl").exists())

    def test_an_unreadable_cr_does_not_spawn_the_job(self):
        """Job が走ってしまえば立案は止められない。走らせる前に落とす。"""
        env = mock.patch.dict(os.environ, {"HEART_MODE": "active"})
        env.start()
        self.addCleanup(env.stop)
        h = Heart(REPO)
        h.k8s = BrokenK8s()
        created = []
        with mock.patch.object(
            spawn, "create", lambda *a, **k: created.append(k) or "job"
        ):
            h.execute(
                [{"type": "spawn_curriculum", "adopt_limit": 2}],
                {"projects": []}, None, None, None,
            )
        self.assertEqual(created, [])
        audit = h.work.read_jsonl("audit.jsonl")
        self.assertIn("error", audit[-1])


class CurriculumPromptsReadTheDigest(unittest.TestCase):
    """本番の curriculum Job が読む先。ここが台帳を指したままだと、
    4b-2b で書き込みを止めた瞬間に教師信号が静かに切れる。"""

    PROMPTS = ("curriculum-generate", "curriculum-judge")

    def prompt(self, name):
        return (REPO / "ops" / "prompts" / f"{name}.md").read_text()

    def test_they_point_at_the_digest(self):
        for name in self.PROMPTS:
            self.assertIn("{{PROPOSALS_HISTORY}}", self.prompt(name),
                          f"{name} が過去案の読み先を持っていない")

    def test_they_no_longer_read_the_ledger_file(self):
        for name in self.PROMPTS:
            for line in self.prompt(name).splitlines():
                if "archive.jsonl" in line:
                    self.assertIn("読まないこと", line,
                                  f"{name} がまだ台帳を読ませている: {line}")

    def test_the_runner_fills_the_placeholder(self):
        """置換されないと `{{PROPOSALS_HISTORY}}` という文字列がそのまま渡る。"""
        source = (REPO / "ops" / "runner" / "runner.py").read_text()
        self.assertIn('"PROPOSALS_HISTORY": os.environ.get(', source)


if __name__ == "__main__":
    unittest.main()
