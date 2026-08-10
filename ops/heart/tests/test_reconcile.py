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


def gate(*records):
    """heart.execute() の run_adopt_gate が書き戻す形 (P-0015)。
    引数が無ければ all_fail (= 予告してよい健全な spec)。"""
    verify = list(records) or [{"cmd": "test -f x", "ok": False, "rc": 1}]
    return {"at": "2026-08-07T11:59:00Z", "verify": verify}


class TestAnnounce(unittest.TestCase):
    def test_proposed_with_zero_window_activates_same_beat(self):
        """アイドルかつ可逆 → 窓 0 で、予告と同じビートで着手まで進む
        (2026-08-09 テンポ改善。1 ビートを空費しない)。"""
        d, actions = reconcile.decide(
            doc(project(adopt_gate=gate())), facts(), RULES, NOW
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "active")
        self.assertIn("announce", kinds(actions))
        self.assertIn("spawn_runner", kinds(actions))
        self.assertEqual(p["veto_deadline"], "2026-08-07T12:00:00Z")

    def test_zero_window_respects_concurrency_cap(self):
        """同一ビートで複数の窓ゼロ案が湧いても cap を超えない。"""
        p1 = project(adopt_gate=gate())
        p2 = project(id="P-0002", branch="project/p-0002", adopt_gate=gate())
        p3 = project(id="P-0003", branch="project/p-0003", adopt_gate=gate())
        d, actions = reconcile.decide(doc(p1, p2, p3), facts(), RULES, NOW)
        spawns = [a for a in actions if a["type"] == "spawn_runner"]
        self.assertEqual(len(spawns), RULES["runner"]["max_concurrent"])
        states = [q["state"] for q in d["projects"]]
        self.assertEqual(states.count("active"), RULES["runner"]["max_concurrent"])
        self.assertEqual(states.count("announced"), 3 - RULES["runner"]["max_concurrent"])

    def test_irreversible_always_waits_window(self):
        d, _ = reconcile.decide(
            doc(project(irreversible=True, adopt_gate=gate())), facts(), RULES, NOW
        )
        deadline = reconcile.parse_iso(d["projects"][0]["veto_deadline"])
        self.assertEqual(deadline - NOW, timedelta(hours=RULES["veto"]["window_hours"]))

    def test_full_slots_wait_window_even_if_reversible(self):
        """窓が付く条件は「満席」(空きスロット無し)。2026-08-10 に完全アイドル基準から変更。"""
        import copy
        rules1 = copy.deepcopy(RULES)
        rules1["runner"]["max_concurrent"] = 1
        d, _ = reconcile.decide(
            doc(project(adopt_gate=gate())), facts(running_runners=1), rules1, NOW
        )
        deadline = reconcile.parse_iso(d["projects"][0]["veto_deadline"])
        self.assertGreater(deadline, NOW)

    def test_free_slot_gives_zero_window_even_if_others_run(self):
        """cap に空きがあれば走行中でも窓ゼロ (稼働率基準の一貫化)。"""
        d, _ = reconcile.decide(
            doc(project(adopt_gate=gate())), facts(running_runners=1), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["veto_deadline"], "2026-08-07T12:00:00Z")

    def test_breaker_blocks_new_announce(self):
        d, actions = reconcile.decide(
            doc(project(adopt_gate=gate())), facts(breaker_tripped=True), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "proposed")
        self.assertNotIn("announce", kinds(actions))


class TestAdoptGate(unittest.TestCase):
    """採択と予告の間のゲート (P-0015)。壊れた spec は予告の前に殺す —
    announce も veto 窓も Job も一切消費しない。"""

    def test_unmeasured_spec_is_not_announced(self):
        d, actions = reconcile.decide(doc(project()), facts(), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["state"], "proposed")
        self.assertIn("run_adopt_gate", kinds(actions))
        self.assertNotIn("announce", kinds(actions))
        self.assertNotIn("veto_deadline", p)

    def test_breaker_blocks_the_gate_too(self):
        """breaker 中は新しい仕事を作らない。clone も走らせない。"""
        _, actions = reconcile.decide(
            doc(project()), facts(breaker_tripped=True), RULES, NOW
        )
        self.assertNotIn("run_adopt_gate", kinds(actions))

    def test_gate_is_measured_only_once(self):
        """測定済みなら再実行しない (毎ビート clone しない)。"""
        _, actions = reconcile.decide(
            doc(project(adopt_gate=gate())), facts(), RULES, NOW
        )
        self.assertNotIn("run_adopt_gate", kinds(actions))

    def test_some_pass_is_bounced_without_announcing(self):
        p = project(adopt_gate=gate(
            {"cmd": "test -f a", "ok": False, "rc": 1},
            {"cmd": "test -d .", "ok": True, "rc": 0},
        ))
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p["stalled_reason"], "adopt_gate_some_pass")
        self.assertNotIn("announce", kinds(actions))
        self.assertNotIn("veto_deadline", p)
        # incident ではなく「採択の不良」として question で渡す
        notifies = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(notifies[0]["ntype"], "question")
        self.assertIn("test -d .", notifies[0]["text"])

    def test_broken_command_is_bounced(self):
        p = project(adopt_gate=gate(
            {"cmd": "no_such_cmd", "ok": False, "rc": 127, "not_found": True},
        ))
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertEqual(
            d["projects"][0]["stalled_reason"], "adopt_gate_broken_command"
        )
        self.assertNotIn("announce", kinds(actions))

    def test_unmeasured_gate_counts_its_attempts(self):
        """恒久的に黙って待つ状態を作らない (冒頭の不変条件)。試行を数えること。"""
        d, _ = reconcile.decide(doc(project()), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["adopt_gate_attempts"], 1)

    def test_unmeasurable_gate_is_handed_to_a_human(self):
        """測定が N 回続けて書き戻されなければ stalled + incident。

        これが無いと clone 失敗や /tmp の枯渇で proposed が無期限・無通知の待ちになり、
        non_terminal が空にならず curriculum_idle も False に固定されて、ビートは
        回っているのに仕事が一切進まない沈黙状態になる。
        """
        p = project(adopt_gate_attempts=reconcile.ADOPT_GATE_MAX_ATTEMPTS)
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p["stalled_reason"], "adopt_gate_unmeasurable")
        self.assertNotIn("run_adopt_gate", kinds(actions))
        self.assertNotIn("announce", kinds(actions))
        # spec の不良ではなく仕組みの故障なので incident
        notifies = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(notifies[0]["ntype"], "incident")

    def test_attempts_short_of_the_limit_still_measure(self):
        p = project(adopt_gate_attempts=reconcile.ADOPT_GATE_MAX_ATTEMPTS - 1)
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "proposed")
        self.assertIn("run_adopt_gate", kinds(actions))

    def test_measured_gate_ignores_the_attempt_counter(self):
        """測れてしまえば試行回数は関係ない (上限を超えていても判定に進む)。"""
        p = project(
            adopt_gate_attempts=reconcile.ADOPT_GATE_MAX_ATTEMPTS + 5,
            adopt_gate=gate({"cmd": "test -f x", "ok": False, "rc": 1}),
        )
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        # 窓ゼロ連鎖 (2026-08-09) により予告と同じビートで active まで進む
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertIn("announce", kinds(actions))

    def test_bounced_spec_frees_the_curriculum(self):
        """差し戻しは終端 (stalled) なので、同じビートで次の立案に進める。"""
        p = project(adopt_gate=gate({"cmd": "test -d .", "ok": True, "rc": 0}))
        _, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertIn("spawn_curriculum", kinds(actions))


class TestActivate(unittest.TestCase):
    def test_announced_spawns_after_deadline(self):
        p = project(state="announced", veto_deadline="2026-08-07T11:00:00Z")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertIn("spawn_runner", kinds(actions))

    def test_announced_reversible_catches_up_when_idle(self):
        """走行中に予告されて窓が付いた可逆案は、アイドルになったら繰り上げて即着手
        (窓の基準は稼働率 — 決定 #3)。"""
        p = project(state="announced", veto_deadline="2026-08-07T13:00:00Z")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertIn("spawn_runner", kinds(actions))

    def test_announced_irreversible_never_catches_up(self):
        p = project(state="announced", irreversible=True,
                    veto_deadline="2026-08-07T13:00:00Z")
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "announced")
        self.assertNotIn("spawn_runner", kinds(actions))
        # (窓待ち中の立案 spawn_curriculum は 2026-08-10 以降は正常。ここでは runner だけ見る)

    def test_announced_waits_while_slots_full(self):
        import copy
        rules1 = copy.deepcopy(RULES)
        rules1["runner"]["max_concurrent"] = 1
        waiting = project(id="P-0002", state="announced", branch="project/p-0002",
                          veto_deadline="2026-08-07T13:00:00Z")
        busy = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(
            doc(busy, waiting),
            facts(running_runners=1, jobs={"runner-p-0001-a1": {"active": True}}),
            rules1, NOW,
        )
        self.assertEqual(d["projects"][1]["state"], "announced")
        self.assertNotIn("spawn_runner", kinds(actions))

    def test_announced_catches_up_when_slot_frees(self):
        """merging 詰まり 1 件 + 空きスロットでも待たされない (2026-08-10 の渋滞バグ修正)。"""
        stuck = project(state="merging", prs=[42],
                        merging_since="2026-08-07T11:30:00Z")
        waiting = project(id="P-0002", state="announced", branch="project/p-0002",
                          veto_deadline="2026-08-08T11:00:00Z")
        d, actions = reconcile.decide(
            doc(stuck, waiting),
            facts(running_runners=1,
                  open_prs={42: {"head": "project/p-0001", "checks_green": False}}),
            RULES, NOW,
        )
        self.assertEqual(d["projects"][1]["state"], "active")
        self.assertIn("spawn_runner", kinds(actions))

    def test_concurrency_cap(self):
        # cap の境界そのものを検査するため、設定値に依存せず cap=1 を注入する
        import copy
        rules1 = copy.deepcopy(RULES)
        rules1["runner"]["max_concurrent"] = 1
        ready = project(id="P-0002", state="announced",
                        branch="project/p-0002",
                        veto_deadline="2026-08-07T11:00:00Z")
        running = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(
            doc(running, ready),
            facts(running_runners=1, jobs={"runner-p-0001-a1": {"active": True}}),
            rules1, NOW,
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


class TestQuotaWait(unittest.TestCase):
    """アカウント利用上限 (P-0026) は器の外側の事実であって停滞ではない。

    runner が waiting_quota で rc=0 終了した回を stalled にせず、resume_after まで
    active のまま待って再開する遷移表。
    """

    def result(self, resume_after):
        return {
            "state": "waiting_quota",
            "failure_kind": "usage_limit",
            "stderr_tail": "Claude AI usage limit reached",
            "resume_after": resume_after,
        }

    def test_waiting_quota_does_not_stall(self):
        p = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": self.result("2026-08-07T14:00:00Z")}),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "active")
        self.assertNotIn("stalled_reason", p)
        self.assertEqual(p["quota_wait_until"], "2026-08-07T14:00:00Z")
        self.assertIn("consume_result", kinds(actions))
        # 上限待ちは障害ではないので通知しない
        self.assertNotIn("notify", kinds(actions))
        self.assertNotIn("spawn_runner", kinds(actions))

    def test_waiting_quota_without_resume_after_resumes_immediately(self):
        p = project(state="active", job="runner-p-0001-a1")
        d, _ = reconcile.decide(
            doc(p), facts(results={"P-0001": self.result(None)}), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["quota_wait_until"], "2026-08-07T12:00:00Z")

    def test_waits_silently_until_the_deadline(self):
        # 待っている間に Job が succeeded で消えても drift を数えない
        # (数えると 3 ビートで stalled になり、上限対策がループを止める)
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_until="2026-08-07T13:00:00Z",
        )
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertEqual(kinds(actions), [])
        self.assertNotIn("drift_count", d["projects"][0])

    def test_respawns_when_the_deadline_arrives(self):
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_until="2026-08-07T11:59:00Z",
        )
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["state"], "active")
        self.assertNotIn("quota_wait_until", p)
        spawns = [a for a in actions if a["type"] == "spawn_runner"]
        self.assertEqual(len(spawns), 1)
        self.assertTrue(spawns[0]["respawn"])

    def test_breaker_holds_the_ticket_instead_of_dropping_it(self):
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_until="2026-08-07T11:59:00Z",
        )
        d, actions = reconcile.decide(doc(p), facts(breaker_tripped=True), RULES, NOW)
        self.assertEqual(kinds(actions), [])
        # 札を落とすと次のビートで job 梯子に落ちて即 respawn してしまう
        self.assertEqual(
            d["projects"][0]["quota_wait_until"], "2026-08-07T11:59:00Z"
        )

    def test_human_stop_still_wins_over_quota_wait(self):
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_until="2026-08-07T11:59:00Z",
        )
        d, actions = reconcile.decide(doc(p), facts(stop_all=True), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertNotIn("spawn_runner", kinds(actions))

    def test_repeated_quota_waits_are_bounded(self):
        # 「恒久的に黙って待つ状態を作らない」は上限待ちにも掛かる。runner の
        # 待機予算は 1 プロセス内の上限にすぎず、waiting_quota → respawn →
        # また waiting_quota の周回には時限が無い。max_concurrent=1 では
        # この 1 件が他の全プロジェクトのスロットを塞ぎ続ける (レビュー指摘 [1])
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_count=reconcile.QUOTA_WAIT_MAX_ROUNDS,
        )
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": self.result("2026-08-07T14:00:00Z")}),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p["stalled_reason"], "quota_wait_exhausted")
        # 札も回数も落とす。残すと人間が active に戻した次の waiting_quota で
        # 即また stalled になり、再開できない停止になる
        self.assertNotIn("quota_wait_until", p)
        self.assertNotIn("quota_wait_count", p)
        self.assertIn("consume_result", kinds(actions))
        notes = [a for a in actions if a["type"] == "notify"]
        self.assertEqual([n["ntype"] for n in notes], ["question"])

    def test_a_non_quota_result_resets_the_round_count(self):
        # 数えるのは「連続」の待ち。間にセッションが動いた回があれば数え直す
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_count=reconcile.QUOTA_WAIT_MAX_ROUNDS,
            prs=[42],
        )
        d, _ = reconcile.decide(
            doc(p),
            facts(results={"P-0001": {"state": "ready_for_review", "pr": 42}}),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "in_review")
        self.assertNotIn("quota_wait_count", p)

    def test_rounds_below_the_limit_keep_waiting(self):
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_count=reconcile.QUOTA_WAIT_MAX_ROUNDS - 1,
        )
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": self.result("2026-08-07T14:00:00Z")}),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "active")
        self.assertEqual(p["quota_wait_count"], reconcile.QUOTA_WAIT_MAX_ROUNDS)
        self.assertEqual(p["quota_wait_until"], "2026-08-07T14:00:00Z")
        self.assertNotIn("notify", kinds(actions))


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


class TestArchiveAdoption(unittest.TestCase):
    SPEC = {"id": "P-0001", "title": "パイロット", "verify": ["false"],
            "irreversible": False, "capabilities": [], "touches_apps": False,
            "budget": {"soft_cap_tokens": 500}, "confidence": "confident"}

    def test_adopted_spec_registers_and_gates_before_announcing(self):
        """main の archive で採択済み・projects 未登録の spec は登録され、
        同じビートで**ゲートの実測まで**進む (手動採択のパイロット経路)。
        予告はその次のビート — 採択と予告の間に実測を挟むのが P-0015 の要件。"""
        d, actions = reconcile.decide(doc(), facts(adopted_specs=[self.SPEC]), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["id"], "P-0001")
        self.assertEqual(p["state"], "proposed")
        self.assertEqual(p["budget"]["soft_cap"], 500)
        self.assertIn("run_adopt_gate", kinds(actions))
        self.assertNotIn("announce", kinds(actions))
        # 仕事が登録されたビートで curriculum は回さない
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_gated_spec_announces_on_the_next_beat(self):
        """all_fail が実測されたら、次のビートで従来通り予告に進む。"""
        d, _ = reconcile.decide(doc(), facts(adopted_specs=[self.SPEC]), RULES, NOW)
        d["projects"][0]["adopt_gate"] = gate()  # heart.execute() が書き戻す想定
        d, actions = reconcile.decide(d, facts(adopted_specs=[self.SPEC]), RULES, NOW)
        # 窓ゼロ連鎖 (2026-08-09) により予告と同じビートで active まで進む
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertIn("announce", kinds(actions))

    def test_terminal_project_is_not_resurrected(self):
        done = project(state="delivered")
        d, actions = reconcile.decide(
            doc(done), facts(adopted_specs=[self.SPEC]), RULES, NOW
        )
        self.assertEqual(len(d["projects"]), 1)
        self.assertEqual(d["projects"][0]["state"], "delivered")


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

    def test_windowed_announced_does_not_block_curriculum(self):
        """拒否権窓で待機中の案件はスロットを使っていないので、立案を塞がない
        (2026-08-10、窓 24h の 1 件が立案を丸一日止めた実害への修正)。"""
        waiting = project(state="announced", irreversible=True,
                          veto_deadline="2026-08-08T12:00:00Z")
        d, actions = reconcile.decide(
            doc(waiting, last_curriculum_at="2026-08-07T11:30:00Z",
                last_curriculum_dry=False),
            facts(), RULES, NOW,
        )
        self.assertIn("spawn_curriculum", kinds(actions))
        # 待機中の案件はそのまま (立案に巻き込まれない)
        self.assertEqual(d["projects"][0]["state"], "announced")

    def test_productive_round_allows_immediate_replan(self):
        """実りある回 (採択あり) の後のアイドルは間隔を待たず即立案。"""
        d, actions = reconcile.decide(
            doc(last_curriculum_at="2026-08-07T11:59:00Z", last_curriculum_dry=False),
            facts(), RULES, NOW,
        )
        self.assertIn("spawn_curriculum", kinds(actions))

    def test_dry_round_keeps_min_interval(self):
        """空振り (採択ゼロ / エラー) の後は min_interval を守る (連打防止)。"""
        d, actions = reconcile.decide(
            doc(last_curriculum_at="2026-08-07T11:00:00Z", last_curriculum_dry=True),
            facts(), RULES, NOW,
        )
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_curriculum_merged_records_dry_flag(self):
        d, _ = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_merged": True, "adopted_specs": []}),
            RULES, NOW,
        )
        self.assertTrue(d["last_curriculum_dry"])

    def test_active_project_blocks_curriculum(self):
        d, actions = reconcile.decide(
            doc(project(state="active", job="j"),),
            facts(jobs={"j": {"active": True}}), RULES, NOW,
        )
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_running_curriculum_job_blocks_next_spawn(self):
        """走行中の curriculum Job がある間は次を spawn しない。result.json は完走まで
        存在しないので、Job の観測で塞ぐ (2026-08-10 の毎分 1 Job 暴走の再発防止)。"""
        d, actions = reconcile.decide(
            doc(last_curriculum_at="2026-08-07T11:59:00Z", last_curriculum_dry=False),
            facts(jobs={"curriculum-system-a123": {"active": True, "failed": False,
                                                   "succeeded": False}}),
            RULES, NOW,
        )
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_completed_curriculum_job_does_not_block_spawn(self):
        """完走済み (active=False) の curriculum Job 残骸は立案を塞がない。"""
        d, actions = reconcile.decide(
            doc(),
            facts(jobs={"curriculum-system-a123": {"active": False, "failed": False,
                                                   "succeeded": True}}),
            RULES, NOW,
        )
        self.assertIn("spawn_curriculum", kinds(actions))

    def test_jobs_unobservable_blocks_curriculum_spawn(self):
        """jobs 観測に失敗したビートは「走っていない」と断定できないので spawn しない。"""
        d, actions = reconcile.decide(doc(), facts(jobs=None), RULES, NOW)
        self.assertNotIn("spawn_curriculum", kinds(actions))


class TestCritic(unittest.TestCase):
    """日次の自己観測 (P-0045)。24h ごと、かつ前回以降に活動があったときだけ spawn。

    「活動があったときだけ」が本体: 何も動いていない器を毎日読ませない。
    critic 自身の action は活動に数えない (数えると自分で自分の条件を作り続ける)。
    """

    def quiet(self, **kw):
        """action が 1 つも生まれないビートの doc。
        空振り直後 (min_interval 内) の完全アイドルがそれに当たる。"""
        base = {"last_curriculum_at": "2026-08-07T11:00:00Z", "last_curriculum_dry": True}
        base.update(kw)
        return doc(**base)

    def test_quiet_beat_really_has_no_actions(self):
        """以降のテストの土台。ここが崩れると活動判定のテストが全部無意味になる。"""
        d, actions = reconcile.decide(self.quiet(), facts(), RULES, NOW)
        self.assertEqual(kinds(actions), [])
        self.assertNotIn("last_activity_at", d)

    def test_no_critic_without_any_activity(self):
        d, actions = reconcile.decide(self.quiet(), facts(), RULES, NOW)
        self.assertNotIn("spawn_critic", kinds(actions))
        self.assertNotIn("last_critic_at", d)

    def test_first_activity_makes_critic_due(self):
        """初回 (last_critic_at 無し) は 24h を待たず、活動が記録され次第 spawn。"""
        d, actions = reconcile.decide(doc(), facts(), RULES, NOW)
        self.assertIn("spawn_curriculum", kinds(actions))  # これが「活動」
        self.assertEqual(d["last_activity_at"], "2026-08-07T12:00:00Z")
        self.assertIn("spawn_critic", kinds(actions))
        self.assertEqual(d["last_critic_at"], "2026-08-07T12:00:00Z")

    def test_critic_rate_limited_within_interval(self):
        """24h 未経過。活動があっても spawn しない。"""
        d, actions = reconcile.decide(
            doc(last_critic_at="2026-08-07T00:00:00Z"), facts(), RULES, NOW
        )
        self.assertIn("spawn_curriculum", kinds(actions))
        self.assertNotIn("spawn_critic", kinds(actions))
        self.assertEqual(d["last_critic_at"], "2026-08-07T00:00:00Z")

    def test_interval_elapsed_but_no_activity_since(self):
        """24h 経ったが前回 critic 以降の活動が無い = 読むものが増えていない。"""
        d, actions = reconcile.decide(
            self.quiet(last_critic_at="2026-08-06T10:00:00Z",
                       last_activity_at="2026-08-06T09:00:00Z"),
            facts(), RULES, NOW,
        )
        self.assertNotIn("spawn_critic", kinds(actions))
        self.assertEqual(d["last_critic_at"], "2026-08-06T10:00:00Z")

    def test_interval_elapsed_with_activity_since(self):
        d, actions = reconcile.decide(
            self.quiet(last_critic_at="2026-08-06T10:00:00Z",
                       last_activity_at="2026-08-06T23:00:00Z"),
            facts(), RULES, NOW,
        )
        self.assertIn("spawn_critic", kinds(actions))
        self.assertEqual(d["last_critic_at"], "2026-08-07T12:00:00Z")

    def test_critic_does_not_sustain_itself(self):
        """critic を spawn したビートの action だけでは、翌日の critic は due にならない。
        自励発振 (毎日 critic → その action が活動 → また毎日 critic) の防止。"""
        d, actions = reconcile.decide(doc(), facts(), RULES, NOW)
        self.assertIn("spawn_critic", kinds(actions))
        later = NOW + timedelta(hours=25)
        # 2 ビート目は立案側を黙らせ、critic の結果だけが来ている状態にする
        d["last_curriculum_at"] = "2026-08-08T12:30:00Z"
        d["last_curriculum_dry"] = True
        d, actions = reconcile.decide(
            d, facts(critic={"state": "done"}), RULES, later
        )
        self.assertEqual(kinds(actions), ["consume_critic", "notify_critic"])
        # consume/notify/spawn_critic は活動に数えないので、活動の刻みは 1 ビート目のまま
        self.assertEqual(d["last_activity_at"], "2026-08-07T12:00:00Z")
        # 活動が increment されていない以上、24h 経っていても次は due にならない
        self.assertEqual(d["last_critic_at"], "2026-08-07T12:00:00Z")

    def test_breaker_blocks_critic(self):
        d, actions = reconcile.decide(
            doc(last_activity_at="2026-08-07T11:00:00Z"),
            facts(breaker_tripped=True), RULES, NOW,
        )
        self.assertNotIn("spawn_critic", kinds(actions))
        self.assertNotIn("last_critic_at", d)

    def test_stop_all_blocks_critic(self):
        d, actions = reconcile.decide(
            doc(last_activity_at="2026-08-07T11:00:00Z"),
            facts(stop_all=True), RULES, NOW,
        )
        self.assertNotIn("spawn_critic", kinds(actions))

    def test_critic_result_is_consumed_and_notified(self):
        d, actions = reconcile.decide(
            self.quiet(), facts(critic={"state": "done"}), RULES, NOW
        )
        self.assertIn("consume_critic", kinds(actions))
        self.assertIn("notify_critic", kinds(actions))

    def test_critic_error_is_an_incident(self):
        """critic が黙って死ぬのを許さない。curriculum の結果置き場 (system) とは
        別ディレクトリなので、curriculum の incident と取り違えない。"""
        d, actions = reconcile.decide(
            self.quiet(), facts(critic={"state": "error", "error": "boom"}), RULES, NOW
        )
        self.assertIn("consume_critic", kinds(actions))
        notes = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(notes[0]["ntype"], "incident")
        self.assertIn("boom", notes[0]["text"])
        self.assertNotIn("notify_critic", kinds(actions))


if __name__ == "__main__":
    unittest.main()
