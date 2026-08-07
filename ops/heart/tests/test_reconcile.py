"""reconcile.decide() の遷移表テスト。

このテーブルが仕様。実装を変えるときはまずここを変える (プラン検証 #1)。
2026-08-07 のレビューで確定した規約 (観測失敗 = None、consume_*、prs の伝搬、
soak の baseline 比較、curriculum の取り込み) を含む。
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ops.heart import reconcile

RULES_PATH = Path(__file__).resolve().parents[2] / "rules.json"
with open(RULES_PATH) as f:
    RULES = json.load(f)

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def project(**kw):
    base = {
        "id": "P-0001",
        "title": "テスト",
        "state": "proposed",
        "branch": "project/p-0001",
        "irreversible": False,
        "capabilities": [],
        "budget": {"used_tokens": 0, "soft_cap": 1000},
        "created": "2026-08-07",
    }
    base.update(kw)
    return base


def doc(*projects, **kw):
    d = {"version": 1, "projects": list(projects), "chores": []}
    d.update(kw)
    return d


def facts(**kw):
    base = {
        "jobs": {},
        "results": {},
        "reviews": {},
        "open_prs": {},
        "merged_prs": {},
        "unhealthy_apps": [],
        "health_green": True,
        "health_fresh": True,
        "vetoes": [],
        "stop_all": False,
        "breaker_tripped": False,
        "running_runners": 0,
        "curriculum": None,
    }
    base.update(kw)
    return base


def kinds(actions):
    return [a["type"] for a in actions]


class TestAnnounce(unittest.TestCase):
    def test_proposed_becomes_announced_with_zero_window_when_idle(self):
        d, actions = reconcile.decide(doc(project()), facts(), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["state"], "announced")
        self.assertIn("announce", kinds(actions))
        # アイドルかつ非不可逆 → 窓 0 (即着手可能な deadline)
        self.assertEqual(p["veto_deadline"], "2026-08-07T12:00:00Z")

    def test_irreversible_always_waits_window(self):
        d, _ = reconcile.decide(doc(project(irreversible=True)), facts(), RULES, NOW)
        deadline = reconcile.parse_iso(d["projects"][0]["veto_deadline"])
        self.assertEqual(deadline - NOW, timedelta(hours=RULES["veto"]["window_hours"]))

    def test_busy_waits_window_even_if_reversible(self):
        d, _ = reconcile.decide(
            doc(project()), facts(running_runners=1), RULES, NOW
        )
        deadline = reconcile.parse_iso(d["projects"][0]["veto_deadline"])
        self.assertGreater(deadline, NOW)

    def test_breaker_blocks_new_announce(self):
        d, actions = reconcile.decide(
            doc(project()), facts(breaker_tripped=True), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "proposed")
        self.assertNotIn("announce", kinds(actions))


class TestActivate(unittest.TestCase):
    def test_announced_spawns_after_deadline(self):
        p = project(state="announced", veto_deadline="2026-08-07T11:00:00Z")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertIn("spawn_runner", kinds(actions))

    def test_announced_waits_before_deadline(self):
        p = project(state="announced", veto_deadline="2026-08-07T13:00:00Z")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "announced")
        self.assertEqual(kinds(actions), [])

    def test_concurrency_cap(self):
        ready = project(id="P-0002", state="announced",
                        branch="project/p-0002",
                        veto_deadline="2026-08-07T11:00:00Z")
        running = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(
            doc(running, ready),
            facts(running_runners=1, jobs={"runner-p-0001-a1": {"active": True}}),
            RULES, NOW,
        )
        self.assertEqual(d["projects"][1]["state"], "announced")
        self.assertNotIn("spawn_runner", kinds(actions))


class TestVeto(unittest.TestCase):
    def test_veto_kills_running_job(self):
        p = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(
            doc(p), facts(vetoes=["P-0001"],
                          jobs={"runner-p-0001-a1": {"active": True}}),
            RULES, NOW,
        )
        self.assertEqual(d["projects"][0]["state"], "vetoed")
        self.assertIn("kill_job", kinds(actions))

    def test_stop_all_stalls_everything(self):
        p1 = project(state="active", job="runner-p-0001-a1")
        p2 = project(id="P-0002", state="announced", branch="project/p-0002",
                     veto_deadline="2026-08-07T11:00:00Z")
        d, actions = reconcile.decide(doc(p1, p2), facts(stop_all=True), RULES, NOW)
        self.assertEqual({p["state"] for p in d["projects"]}, {"stalled"})
        self.assertIn("kill_job", kinds(actions))
        # 停止中に curriculum を回さない
        self.assertNotIn("spawn_curriculum", kinds(actions))


class TestActiveObservation(unittest.TestCase):
    def test_jobs_none_means_unobservable_not_missing(self):
        """観測失敗 (jobs=None) を Job 消失と混同しない (レビュー指摘 [3])。"""
        p = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(doc(p), facts(jobs=None), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertEqual(kinds(actions), [])
        self.assertNotIn("drift_count", d["projects"][0])

    def test_missing_job_respawns_then_stalls(self):
        p = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["drift_count"], 1)
        self.assertIn("spawn_runner", kinds(actions))
        p3 = project(state="active", job="runner-p-0001-a1", drift_count=2)
        d, actions = reconcile.decide(doc(p3), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "stalled")

    def test_drift_resets_when_job_observed(self):
        p = project(state="active", job="runner-p-0001-a1", drift_count=2)
        d, _ = reconcile.decide(
            doc(p), facts(jobs={"runner-p-0001-a1": {"active": True}}), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["drift_count"], 0)

    def test_active_without_job_field_respawns(self):
        """spawn 失敗で job 未記録のまま stuck しない (レビュー指摘 [4])。"""
        p = project(state="active")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertIn("spawn_runner", kinds(actions))

    def test_budget_exhausted_stalls_with_question_and_consumes(self):
        p = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": {"state": "budget_exhausted"}}), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertIn("consume_result", kinds(actions))
        notifies = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(notifies[0]["ntype"], "question")

    def test_runner_error_result_stalls(self):
        p = project(state="active", job="j")
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": {"state": "error", "error": "x"}}),
            RULES, NOW,
        )
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertIn("consume_result", kinds(actions))


class TestReviewFlow(unittest.TestCase):
    def test_ready_for_review_records_pr_and_spawns_reviewer(self):
        p = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(
            doc(p),
            facts(results={"P-0001": {"state": "ready_for_review", "pr": 42}},
                  jobs={"runner-p-0001-a1": {"active": True}}),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "in_review")
        self.assertEqual(p["prs"], [42])  # merging が読む (レビュー指摘 [1])
        self.assertIn("spawn_reviewer", kinds(actions))
        self.assertIn("kill_job", kinds(actions))
        self.assertIn("consume_result", kinds(actions))

    def test_ready_for_review_without_pr_stalls(self):
        p = project(state="active", job="j")
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": {"state": "ready_for_review"}}), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertEqual(d["projects"][0]["stalled_reason"], "no_pr_reported")

    def test_review_pass_moves_to_merging_and_consumes(self):
        p = project(state="in_review", prs=[42])
        d, actions = reconcile.decide(
            doc(p), facts(reviews={"P-0001": {"verdict": "pass"}}), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "merging")
        self.assertIn("consume_review", kinds(actions))

    def test_review_fail_returns_to_active_with_findings(self):
        p = project(state="in_review", prs=[42])
        d, actions = reconcile.decide(
            doc(p),
            facts(reviews={"P-0001": {"verdict": "fail", "findings": ["f1"]}}),
            RULES, NOW,
        )
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertEqual(d["projects"][0]["review_cycles"], 1)
        spawns = [a for a in actions if a["type"] == "spawn_runner"]
        self.assertEqual(spawns[0]["findings"], ["f1"])
        self.assertIn("consume_review", kinds(actions))

    def test_review_fail_over_limit_stalls(self):
        p = project(state="in_review", prs=[42],
                    review_cycles=RULES["review"]["max_cycles"] - 1)
        d, actions = reconcile.decide(
            doc(p), facts(reviews={"P-0001": {"verdict": "fail"}}), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "stalled")
        notifies = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(notifies[0]["ntype"], "review")

    def test_review_silence_retries_then_stalls(self):
        """reviewer が黙って死んでも恒久に待たない (レビュー指摘 [4])。"""
        old = "2026-08-07T09:00:00Z"  # 3h 前 > REVIEW_TIMEOUT_HOURS
        p = project(state="in_review", prs=[42], review_requested_at=old,
                    review_retries=0)
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["review_retries"], 1)
        self.assertIn("spawn_reviewer", kinds(actions))
        p2 = project(state="in_review", prs=[42], review_requested_at=old,
                     review_retries=reconcile.REVIEW_MAX_RETRIES)
        d, actions = reconcile.decide(doc(p2), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertEqual(d["projects"][0]["stalled_reason"], "review_timeout")


class TestMergeAndSoak(unittest.TestCase):
    def test_merge_only_when_checks_green(self):
        p = project(state="merging", prs=[42], merging_since="2026-08-07T11:00:00Z")
        d, actions = reconcile.decide(
            doc(p), facts(open_prs={42: {"head": "project/p-0001",
                                         "checks_green": False}}),
            RULES, NOW,
        )
        self.assertNotIn("merge_pr", kinds(actions))
        d, actions = reconcile.decide(
            doc(project(state="merging", prs=[42],
                        merging_since="2026-08-07T11:00:00Z")),
            facts(open_prs={42: {"head": "project/p-0001", "checks_green": True}}),
            RULES, NOW,
        )
        self.assertIn("merge_pr", kinds(actions))

    def test_merging_without_pr_record_stalls_with_notify(self):
        p = project(state="merging")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertIn("notify", kinds(actions))  # 黙って止まらない

    def test_pr_closed_unmerged_stalls(self):
        """merge されず close された PR を永遠に待たない。"""
        p = project(state="merging", prs=[42], merging_since="2026-08-07T11:00:00Z")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["stalled_reason"], "pr_closed")

    def test_merging_timeout_stalls_with_question(self):
        old = "2026-08-05T11:00:00Z"  # 2 日前 > MERGING_TIMEOUT_HOURS
        p = project(state="merging", prs=[42], merging_since=old)
        d, actions = reconcile.decide(
            doc(p), facts(open_prs={42: {"head": "project/p-0001",
                                         "checks_green": False}}),
            RULES, NOW,
        )
        self.assertEqual(d["projects"][0]["stalled_reason"], "merge_timeout")

    def test_merged_apps_change_starts_soak_with_baseline(self):
        p = project(state="merging", prs=[42], touches_apps=True,
                    merging_since="2026-08-07T11:00:00Z")
        d, actions = reconcile.decide(
            doc(p), facts(merged_prs={42: True},
                          unhealthy_apps=["coder", "immich"]),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "soaking")
        # merge 時点の unhealthy を baseline に記録 (既知 Degraded を soak 失敗にしない)
        self.assertEqual(p["soak"]["baseline_unhealthy"], ["coder", "immich"])
        self.assertNotIn("deliver", kinds(actions))

    def test_merged_non_apps_delivers_directly(self):
        p = project(state="merging", prs=[42], merging_since="2026-08-07T11:00:00Z")
        d, actions = reconcile.decide(doc(p), facts(merged_prs={42: True}), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "delivered")
        self.assertIn("deliver", kinds(actions))

    def test_soak_no_new_unhealthy_delivers(self):
        p = project(state="soaking",
                    soak={"until": "2026-08-07T11:30:00Z",
                          "baseline_unhealthy": ["coder"]})
        d, actions = reconcile.decide(
            doc(p), facts(unhealthy_apps=["coder"]), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "delivered")

    def test_soak_new_unhealthy_stalls_with_incident(self):
        p = project(state="soaking",
                    soak={"until": "2026-08-07T11:30:00Z",
                          "baseline_unhealthy": ["coder"]})
        d, actions = reconcile.decide(
            doc(p), facts(unhealthy_apps=["coder", "immich"]), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "stalled")
        notifies = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(notifies[0]["ntype"], "incident")
        self.assertIn("immich", notifies[0]["text"])

    def test_soak_waits_when_health_unobservable(self):
        p = project(state="soaking",
                    soak={"until": "2026-08-07T11:30:00Z", "baseline_unhealthy": []})
        d, _ = reconcile.decide(doc(p), facts(unhealthy_apps=None), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "soaking")

    def test_soak_waits_until_deadline(self):
        p = project(state="soaking",
                    soak={"until": "2026-08-07T12:30:00Z", "baseline_unhealthy": []})
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "soaking")


class TestCurriculum(unittest.TestCase):
    def test_idle_spawns_curriculum(self):
        d, actions = reconcile.decide(doc(), facts(), RULES, NOW)
        self.assertIn("spawn_curriculum", kinds(actions))
        self.assertEqual(d["last_curriculum_at"], "2026-08-07T12:00:00Z")

    def test_curriculum_rate_limited(self):
        d, actions = reconcile.decide(
            doc(last_curriculum_at="2026-08-07T11:00:00Z"), facts(), RULES, NOW
        )
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_breaker_blocks_curriculum(self):
        d, actions = reconcile.decide(doc(), facts(breaker_tripped=True), RULES, NOW)
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_pending_result_blocks_next_curriculum(self):
        """未処理の立案結果がある間は次の立案をしない (PR 無限蓄積の防止 [9])。"""
        d, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_open": True, "checks_green": False,
                                     "pr_merged": False}),
            RULES, NOW,
        )
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_curriculum_pr_merges_when_green(self):
        d, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_open": True, "checks_green": True,
                                     "pr_merged": False}),
            RULES, NOW,
        )
        merges = [a for a in actions if a["type"] == "merge_pr"]
        self.assertEqual(merges[0]["pr"], 7)

    def test_curriculum_merged_registers_projects_and_consumes(self):
        spec = {"id": "P-0009", "title": "t", "verify": ["false"],
                "irreversible": True, "capabilities": ["kubectl-write"],
                "touches_apps": True, "budget": {"soft_cap_tokens": 99},
                "confidence": "confident"}
        d, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_merged": True, "adopted_specs": [spec]}),
            RULES, NOW,
        )
        self.assertIn("consume_curriculum", kinds(actions))
        p = d["projects"][0]
        self.assertEqual(p["id"], "P-0009")
        self.assertEqual(p["state"], "proposed")
        self.assertEqual(p["branch"], "project/p-0009")
        self.assertTrue(p["irreversible"])
        self.assertEqual(p["budget"]["soft_cap"], 99)

    def test_curriculum_pr_rejected_discards_result(self):
        d, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_open": False, "pr_merged": False}),
            RULES, NOW,
        )
        self.assertIn("consume_curriculum", kinds(actions))
        self.assertEqual(d["projects"], [])
        # 破棄した後は次の立案が可能 (このビートで即 spawn)
        self.assertIn("spawn_curriculum", kinds(actions))

    def test_curriculum_pr_unknown_waits(self):
        d, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_unknown": True}),
            RULES, NOW,
        )
        self.assertEqual(kinds(actions), [])

    def test_active_project_blocks_curriculum(self):
        d, actions = reconcile.decide(
            doc(project(state="active", job="j"),),
            facts(jobs={"j": {"active": True}}), RULES, NOW,
        )
        self.assertNotIn("spawn_curriculum", kinds(actions))


if __name__ == "__main__":
    unittest.main()
