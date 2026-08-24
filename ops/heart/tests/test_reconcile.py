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
        "budget": {"used_tokens": 0},
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


def merge_error(reason, status=405, pr=42):
    """heart.execute() の merge_pr が失敗時に書き戻す形 (adopt_gate と同じ流儀)。"""
    return {"at": "2026-08-07T11:59:00Z", "pr": pr, "status": status, "reason": reason}


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
        """同一ビートで複数の窓ゼロ案が湧いても cap を超えない。
        運用値 (rules.json) に依存しないよう cap=2 を注入する。"""
        import copy
        rules2 = copy.deepcopy(RULES)
        rules2["runner"]["max_concurrent"] = 2
        p1 = project(adopt_gate=gate())
        p2 = project(id="P-0002", branch="project/p-0002", adopt_gate=gate())
        p3 = project(id="P-0003", branch="project/p-0003", adopt_gate=gate())
        d, actions = reconcile.decide(doc(p1, p2, p3), facts(), rules2, NOW)
        spawns = [a for a in actions if a["type"] == "spawn_runner"]
        self.assertEqual(len(spawns), 2)
        states = [q["state"] for q in d["projects"]]
        self.assertEqual(states.count("active"), 2)
        self.assertEqual(states.count("announced"), 1)

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
        """cap に空きがあれば走行中でも窓ゼロ (稼働率基準の一貫化)。
        運用値 (rules.json の max_concurrent) に依存しないよう cap=2 を注入する。"""
        import copy
        rules2 = copy.deepcopy(RULES)
        rules2["runner"]["max_concurrent"] = 2
        d, _ = reconcile.decide(
            doc(project(adopt_gate=gate())), facts(running_runners=1), rules2, NOW
        )
        self.assertEqual(d["projects"][0]["veto_deadline"], "2026-08-07T12:00:00Z")


class TestCostIsNotAGate(unittest.TestCase):
    """消費量 (金額・トークン) では止まらない。2026-08-24 にサーキットブレーカーと
    soft cap を廃止した。廃止済みの fact (breaker_tripped) を渡しても、器は
    従来どおり仕事を作り続けること — 再導入をここで固定する。"""

    def test_cost_does_not_block_announce(self):
        d, actions = reconcile.decide(
            doc(project(adopt_gate=gate())), facts(breaker_tripped=True), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertIn("announce", kinds(actions))
        self.assertIn("spawn_runner", kinds(actions))

    def test_cost_does_not_block_the_adopt_gate(self):
        _, actions = reconcile.decide(
            doc(project()), facts(breaker_tripped=True), RULES, NOW
        )
        self.assertIn("run_adopt_gate", kinds(actions))

    def test_cost_does_not_hold_the_quota_ticket(self):
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_until="2026-08-07T11:59:00Z",
        )
        d, actions = reconcile.decide(doc(p), facts(breaker_tripped=True), RULES, NOW)
        self.assertNotIn("quota_wait_until", d["projects"][0])
        self.assertIn("spawn_runner", kinds(actions))

    def test_cost_does_not_block_curriculum(self):
        _, actions = reconcile.decide(doc(), facts(breaker_tripped=True), RULES, NOW)
        self.assertIn("spawn_curriculum", kinds(actions))

    def test_cost_does_not_block_critic(self):
        d, actions = reconcile.decide(
            doc(last_activity_at="2026-08-07T11:00:00Z"),
            facts(breaker_tripped=True), RULES, NOW,
        )
        self.assertIn("spawn_critic", kinds(actions))
        self.assertEqual(d["last_critic_at"], "2026-08-07T12:00:00Z")


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

    # --- dispatch 由来 (P-9NNN) はゲートを通さない (2026-08-24 の所有者判断) ---
    def test_dispatch_project_without_verify_skips_the_gate(self):
        """所有者の依頼は測らずに進める。verify を書くのも LLM なので、
        機械の判定として意味を成さないという判断。"""
        p = project(id="P-9000", branch="project/p-9000",
                    dispatch_id="d-abc", requested_by="core", verify=[])
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertNotIn("run_adopt_gate", kinds(actions))
        # 窓ゼロ連鎖でそのまま着手まで進む
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertIn("spawn_runner", kinds(actions))
        self.assertNotIn("adopt_gate_attempts", d["projects"][0])

    def test_curriculum_project_still_goes_through_the_gate(self):
        """回帰: curriculum 由来 (verify を持つ) は今までどおり測る。"""
        p = project(verify=["test -f x"])
        d, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "proposed")
        self.assertIn("run_adopt_gate", kinds(actions))

    def test_dispatch_project_with_verify_still_goes_through_the_gate(self):
        """dispatch でも verify を持つ古いレコードは測る (後方互換)。"""
        p = project(id="P-9000", branch="project/p-9000",
                    dispatch_id="d-abc", verify=["test -f x"])
        _, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        self.assertIn("run_adopt_gate", kinds(actions))

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
        import copy
        rules2 = copy.deepcopy(RULES)
        rules2["runner"]["max_concurrent"] = 2
        d, actions = reconcile.decide(
            doc(stuck, waiting),
            facts(running_runners=1,
                  open_prs={42: {"head": "project/p-0001", "checks_green": False}}),
            rules2, NOW,
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

    def test_ack_marks_terminal_project_acknowledged(self):
        """ack は終端の墓標を既読化するだけ。状態は変えず、非終端には効かない。"""
        d, actions = reconcile.decide(
            doc(project(state="stalled", stalled_reason="spec_error"),
                project(id="P-0002", branch="project/p-0002", state="active", job="j")),
            facts(acks=["P-0001", "P-0002"], jobs={"j": {"active": True}}),
            RULES, NOW,
        )
        self.assertTrue(d["projects"][0]["acknowledged"])
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertNotIn("acknowledged", d["projects"][1])
        self.assertEqual(d["projects"][1]["state"], "active")

    def test_stop_engages_and_persists_across_beats(self):
        """「止めて」は受信ビート限りで消えない。全 stalled 化の次のビートは
        アイドルに見えるが、人間が再開と言うまで spawn を一切しない
        (2026-08-10 の全停止要求で curriculum が再点火しかけた再発防止)。"""
        # 受信ビート: フラグが doc に永続化される
        d, _ = reconcile.decide(doc(), facts(stop_all=True), RULES, NOW)
        self.assertTrue(d["stop_engaged"])
        # 次のビート: stop_all=False でもアイドルで spawn しない
        d2, actions = reconcile.decide(d, facts(), RULES, NOW)
        self.assertNotIn("spawn_curriculum", kinds(actions))
        self.assertTrue(d2["stop_engaged"])
        # 停止中に登録された採択 spec も走り出さず stalled に落ちる
        spec = {"id": "P-0090", "title": "t", "verify": ["false"]}
        d3, actions = reconcile.decide(d2, facts(adopted_specs=[spec]), RULES, NOW)
        self.assertEqual(
            [p["state"] for p in d3["projects"] if p["id"] == "P-0090"], ["stalled"]
        )
        self.assertNotIn("spawn_runner", kinds(actions))

    def test_resume_clears_engaged_stop(self):
        d, actions = reconcile.decide(
            doc(stop_engaged=True), facts(resume_all=True), RULES, NOW
        )
        self.assertFalse(d["stop_engaged"])
        # 解除されたビートから通常運転 (アイドルなら立案してよい)
        self.assertIn("spawn_curriculum", kinds(actions))

    def test_stop_wins_over_resume_in_same_beat(self):
        d, _ = reconcile.decide(
            doc(), facts(stop_all=True, resume_all=True), RULES, NOW
        )
        self.assertTrue(d["stop_engaged"])


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

    def test_session_limit_stalls_with_question_and_consumes(self):
        p = project(state="active", job="runner-p-0001-a1")
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": {"state": "session_limit"}}), RULES, NOW
        )
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertEqual(d["projects"][0]["stalled_reason"], "session_limit")
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
        # 待機中プロジェクト自身への action は無い (2026-08-22 以降、空きスロット
        # 起点の spawn_curriculum / それに伴う critic はビート全体としては出うる)
        self.assertEqual(
            [a for a in actions if a.get("project") == "P-0001"], []
        )
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

    def test_human_stop_still_wins_over_quota_wait(self):
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_until="2026-08-07T11:59:00Z",
        )
        d, actions = reconcile.decide(doc(p), facts(stop_all=True), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertNotIn("spawn_runner", kinds(actions))

    def test_first_wait_records_when_it_started(self):
        # 打ち切りの基準は回数ではなく実時間。起点をここで刻む
        p = project(state="active", job="runner-p-0001-a1")
        d, _ = reconcile.decide(
            doc(p), facts(results={"P-0001": self.result("2026-08-07T14:00:00Z")}),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["quota_wait_since"], "2026-08-07T12:00:00Z")
        self.assertEqual(p["quota_wait_count"], 1)

    def test_waiting_under_the_time_limit_keeps_waiting(self):
        # 何回待とうと、実時間が閾値未満なら止めない。上限待ちは予算方式の廃止で
        # 「異常」ではなく通常の運転状態になった (2026-08-24)
        started = NOW - timedelta(hours=reconcile.QUOTA_WAIT_MAX_HOURS - 1)
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_count=99,
            quota_wait_since=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": self.result("2026-08-07T14:00:00Z")}),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "active")
        self.assertEqual(p["quota_wait_until"], "2026-08-07T14:00:00Z")
        self.assertNotIn("notify", kinds(actions))

    def test_waiting_past_the_time_limit_asks_the_human(self):
        # 「恒久的に黙って待つ状態を作らない」は上限待ちにも掛かる。
        # 連続 QUOTA_WAIT_MAX_HOURS を超えたら人間に判断を渡す
        started = NOW - timedelta(hours=reconcile.QUOTA_WAIT_MAX_HOURS + 1)
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_count=3,
            quota_wait_since=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        d, actions = reconcile.decide(
            doc(p), facts(results={"P-0001": self.result("2026-08-07T14:00:00Z")}),
            RULES, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p["stalled_reason"], "quota_wait_exhausted")
        # 札も回数も起点も落とす。残すと人間が active に戻した次の waiting_quota で
        # 即また stalled になり、再開できない停止になる
        self.assertNotIn("quota_wait_until", p)
        self.assertNotIn("quota_wait_count", p)
        self.assertNotIn("quota_wait_since", p)
        self.assertIn("consume_result", kinds(actions))
        notes = [a for a in actions if a["type"] == "notify"]
        self.assertEqual([n["ntype"] for n in notes], ["question"])

    def test_a_non_quota_result_resets_the_clock(self):
        # 数えるのは「連続」の待ち。間にセッションが動いた回があれば起点を捨てる
        started = NOW - timedelta(hours=reconcile.QUOTA_WAIT_MAX_HOURS + 1)
        p = project(
            state="active", job="runner-p-0001-a1",
            quota_wait_count=9,
            quota_wait_since=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        self.assertNotIn("quota_wait_since", p)


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

    def test_ready_for_review_without_pr_stalls_even_without_verify(self):
        """回帰: verify を外した dispatch 経路でも PR は緩めない。
        PR は機械が確認できる事実なので、無ければ止めて人間に見せる。"""
        p = project(id="P-9000", branch="project/p-9000", state="active", job="j",
                    dispatch_id="d-abc", verify=[])
        d, _ = reconcile.decide(
            doc(p), facts(results={"P-9000": {"state": "ready_for_review"}}), RULES, NOW
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

    def test_merge_conflict_stalls_with_question(self):
        """コンフリクトは再試行で直らない。毎ビート叩き続けず人間に渡す
        (2026-08-23: P-0216 が 405 "merge conflicts" で無限再試行した)。"""
        p = project(state="merging", prs=[42], merging_since="2026-08-07T11:00:00Z",
                    merge_error=merge_error("Pull Request has merge conflicts"))
        d, actions = reconcile.decide(
            doc(p), facts(open_prs={42: {"head": "project/p-0001",
                                         "checks_green": True}}),
            RULES, NOW,
        )
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertEqual(d["projects"][0]["stalled_reason"], "merge_conflict")
        self.assertNotIn("merge_pr", kinds(actions))  # もう叩かない
        notifies = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(len(notifies), 1)
        self.assertEqual(notifies[0]["ntype"], "question")

    def test_merge_conflict_notifies_only_once(self):
        """stalled は終端なので、同じ失敗が残っていても次のビートで再通知しない。"""
        p = project(state="merging", prs=[42], merging_since="2026-08-07T11:00:00Z",
                    merge_error=merge_error("Pull Request has merge conflicts"))
        d, _ = reconcile.decide(
            doc(p), facts(open_prs={42: {"head": "project/p-0001",
                                         "checks_green": True}}),
            RULES, NOW,
        )
        d, actions = reconcile.decide(
            d, facts(open_prs={42: {"head": "project/p-0001", "checks_green": True}}),
            RULES, NOW,
        )
        self.assertEqual([a for a in actions if a["type"] == "notify"], [])

    def test_merge_conflict_frees_the_slot(self):
        """コンフリクトで stalled になったら running から外れ、次の案件が着手できる。

        走行数はビート冒頭で数えるので、空くのは次のビート (他の stalled 遷移と同じ)。
        永久に塞がないことがここの要点。"""
        import copy
        rules1 = copy.deepcopy(RULES)
        rules1["runner"]["max_concurrent"] = 1
        stuck = project(state="merging", prs=[42],
                        merging_since="2026-08-07T11:00:00Z",
                        merge_error=merge_error("Pull Request has merge conflicts"))
        waiting = project(id="P-0002", state="announced", branch="project/p-0002",
                          veto_deadline="2026-08-07T11:00:00Z")
        d, actions = reconcile.decide(
            doc(stuck, waiting),
            facts(running_runners=1,
                  open_prs={42: {"head": "project/p-0001", "checks_green": True}}),
            rules1, NOW,
        )
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertEqual(d["projects"][1]["state"], "announced")
        d, actions = reconcile.decide(
            d,
            facts(running_runners=0,
                  open_prs={42: {"head": "project/p-0001", "checks_green": True}}),
            rules1, NOW,
        )
        self.assertEqual(d["projects"][1]["state"], "active")
        self.assertIn("spawn_runner", kinds(actions))

    def test_transient_merge_failure_retries(self):
        """ネットワーク断・5xx は直りうる。従来どおり再試行する。"""
        for err in (
            merge_error("Server Error", status=500),
            merge_error("<urlopen error timed out>", status=None),
        ):
            with self.subTest(err=err):
                p = project(state="merging", prs=[42],
                            merging_since="2026-08-07T11:00:00Z", merge_error=err)
                d, actions = reconcile.decide(
                    doc(p), facts(open_prs={42: {"head": "project/p-0001",
                                                 "checks_green": True}}),
                    RULES, NOW,
                )
                self.assertEqual(d["projects"][0]["state"], "merging")
                self.assertIn("merge_pr", kinds(actions))

    def test_other_405_reasons_retry(self):
        """405 は理由まで見る。base 更新や必須チェック待ちは再試行で直る。"""
        for reason in (
            "Base branch was modified. Review and try the merge again.",
            "Required status check \"ci\" is expected.",
        ):
            with self.subTest(reason=reason):
                p = project(state="merging", prs=[42],
                            merging_since="2026-08-07T11:00:00Z",
                            merge_error=merge_error(reason))
                d, actions = reconcile.decide(
                    doc(p), facts(open_prs={42: {"head": "project/p-0001",
                                                 "checks_green": True}}),
                    RULES, NOW,
                )
                self.assertEqual(d["projects"][0]["state"], "merging")
                self.assertIn("merge_pr", kinds(actions))

    def test_merge_conflict_of_older_pr_is_ignored(self):
        """別 PR (作り直し前) の失敗記録で、今の PR の merge を止めない。"""
        p = project(state="merging", prs=[41, 42],
                    merging_since="2026-08-07T11:00:00Z",
                    merge_error=merge_error("Pull Request has merge conflicts", pr=41))
        d, actions = reconcile.decide(
            doc(p), facts(open_prs={42: {"head": "project/p-0001",
                                         "checks_green": True}}),
            RULES, NOW,
        )
        self.assertEqual(d["projects"][0]["state"], "merging")
        self.assertIn("merge_pr", kinds(actions))

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
            "confidence": "confident"}

    def test_adopted_spec_registers_and_gates_before_announcing(self):
        """main の archive で採択済み・projects 未登録の spec は登録され、
        同じビートで**ゲートの実測まで**進む (手動採択のパイロット経路)。
        予告はその次のビート — 採択と予告の間に実測を挟むのが P-0015 の要件。"""
        d, actions = reconcile.decide(doc(), facts(adopted_specs=[self.SPEC]), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["id"], "P-0001")
        self.assertEqual(p["state"], "proposed")
        # 消費量は計測として 0 から始まるだけ (上限は持たない)
        self.assertEqual(p["budget"], {"used_tokens": 0})
        self.assertIn("run_adopt_gate", kinds(actions))
        self.assertNotIn("announce", kinds(actions))
        # 登録された spec は同ビートの立案の adopt_limit を 1 減らす
        # (2026-08-22 空きスロット基準化。完全アイドル時代は「登録ビートで
        # 立案しない」だったが、いまは空きが残っていれば回してよい)
        spawns = [a for a in actions if a["type"] == "spawn_curriculum"]
        if spawns:
            self.assertEqual(
                spawns[0]["adopt_limit"], RULES["runner"]["max_concurrent"] - 1
            )

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


class TestRejected(unittest.TestCase):
    """rejected は「動かすべきもの」ではない (設計 state-out-of-git 4b-1)。

    棄却案は Project CR にだけ置き、projects.json には載せない。それでも万一
    ここへ流れ込んだときに一斉着手が起きないことを遷移表として固定しておく —
    間違えたときの被害が「250 件の Job が同時に立つ」なので、実装の都合
    (TERMINAL_STATES に入れた) ではなく仕様として押さえる。
    """

    def rejected(self, **kw):
        # 棄却案は branch を持たない。空文字が通るのは終端だからで、
        # そこが崩れると validate_projects が先に落ちる
        return project(state="rejected", branch="", **kw)

    def test_rejected_is_terminal(self):
        from ops.heart import statefiles

        self.assertIn("rejected", statefiles.PROJECT_STATES)
        self.assertIn("rejected", statefiles.TERMINAL_STATES)

    def test_rejected_never_moves_and_spawns_nothing(self):
        d, actions = reconcile.decide(doc(self.rejected()), facts(), RULES, NOW)
        self.assertEqual(d["projects"][0]["state"], "rejected")
        self.assertNotIn("spawn", kinds(actions))
        self.assertNotIn("announce", kinds(actions))
        self.assertNotIn("run_adopt_gate", kinds(actions))

    def test_rejected_does_not_occupy_a_slot(self):
        """棄却案が並列度を食うと、台帳が増えるほど器が止まる。"""
        rejects = [
            self.rejected(id=f"P-0{n:03d}")
            for n in range(RULES["runner"]["max_concurrent"] + 5)
        ]
        _, actions = reconcile.decide(doc(*rejects), facts(), RULES, NOW)
        self.assertIn("spawn_curriculum", kinds(actions))

    def test_rejected_with_an_empty_branch_passes_validation(self):
        from ops.heart import statefiles

        self.assertEqual(statefiles.validate_projects(doc(self.rejected())), [])


class TestCurriculum(unittest.TestCase):
    def test_idle_spawns_curriculum(self):
        d, actions = reconcile.decide(doc(), facts(), RULES, NOW)
        self.assertIn("spawn_curriculum", kinds(actions))
        self.assertEqual(d["last_curriculum_at"], "2026-08-07T12:00:00Z")

    def test_curriculum_rate_limited(self):
        d, actions = reconcile.decide(
            doc(last_curriculum_at="2026-08-07T11:30:00Z"), facts(), RULES, NOW
        )
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_result_of_this_beat_blocks_the_next_curriculum(self):
        """消費と次の立案を同じビートに重ねない。"""
        d, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done",
                                     "at": "2026-08-07T11:59:00Z"}),
            RULES, NOW,
        )
        self.assertIn("consume_curriculum", kinds(actions))
        self.assertNotIn("spawn_curriculum", kinds(actions))

    def test_curriculum_registers_projects_and_consumes(self):
        """**git は一切見ない** (4b-2b)。採択は result.json だけで動き出す。"""
        spec = {"id": "P-0009", "title": "t", "verify": ["false"],
                "irreversible": True, "capabilities": ["kubectl-write"],
                "touches_apps": True, "confidence": "confident"}
        d, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done",
                                     "at": "2026-08-07T11:59:00Z",
                                     "adopted_specs": [spec]}),
            RULES, NOW,
        )
        self.assertIn("consume_curriculum", kinds(actions))
        p = d["projects"][0]
        self.assertEqual(p["id"], "P-0009")
        self.assertEqual(p["state"], "proposed")
        self.assertEqual(p["branch"], "project/p-0009")
        self.assertTrue(p["irreversible"])
        self.assertEqual(p["budget"], {"used_tokens": 0})

    def test_same_result_is_registered_once(self):
        """同じ result.json を読み直しても二度登録しない (鍵は書き込み時刻)。"""
        spec = {"id": "P-0009", "title": "t", "verify": ["false"]}
        cur = {"state": "curriculum_done", "at": "2026-08-07T11:59:00Z",
               "adopted_specs": [spec]}
        d, _ = reconcile.decide(doc(), facts(curriculum=cur), RULES, NOW)
        self.assertEqual(len(d["projects"]), 1)
        d2, _ = reconcile.decide(d, facts(curriculum=cur), RULES, NOW)
        self.assertEqual(len(d2["projects"]), 1)

    def test_a_later_result_is_registered_again(self):
        """**次の立案が落ちない。** PR 番号で畳んでいた頃、番号が消えた後は
        None 同士が一致して 1 ラウンド丸ごと無視される穴があった。"""
        first = {"state": "curriculum_done", "at": "2026-08-07T11:00:00Z",
                 "adopted_specs": [{"id": "P-0009", "title": "t"}]}
        second = {"state": "curriculum_done", "at": "2026-08-07T11:59:00Z",
                  "adopted_specs": [{"id": "P-0011", "title": "u"}]}
        d, _ = reconcile.decide(doc(), facts(curriculum=first), RULES, NOW)
        d2, _ = reconcile.decide(d, facts(curriculum=second), RULES, NOW)
        self.assertEqual(sorted(p["id"] for p in d2["projects"]), ["P-0009", "P-0011"])

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
            doc(last_curriculum_at="2026-08-07T11:30:00Z", last_curriculum_dry=True),
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

    def test_human_request_adoption_marks_requests_processed(self):
        """採択された依頼由来の案 (request_id 持ち) があれば、その依頼を処理済みに
        する action を出す (P-0091)。実行は heart.execute()。"""
        spec = {"id": "P-0009", "title": "t", "verify": ["false"],
                "request_id": "bbb", "proposed_by": "human-request"}
        d, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_merged": True, "adopted_specs": [spec]}),
            RULES, NOW,
        )
        marks = [a for a in actions if a["type"] == "mark_task_requests_done"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["ids"], ["bbb"])
        self.assertIn("consume_curriculum", kinds(actions))

    def test_mark_ids_are_unique_and_sorted(self):
        specs = [{"id": "P-0002", "verify": [], "request_id": "bbb"},
                 {"id": "P-0003", "verify": [], "request_id": "aaa"},
                 {"id": "P-0004", "verify": [], "request_id": "bbb"}]
        _, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_merged": True, "adopted_specs": specs}),
            RULES, NOW,
        )
        marks = [a for a in actions if a["type"] == "mark_task_requests_done"]
        self.assertEqual(marks[0]["ids"], ["aaa", "bbb"])

    def test_plain_adoption_emits_no_task_request_action(self):
        """通常の案 (request_id 無し) の採択では依頼キューに触れない。"""
        spec = {"id": "P-0009", "title": "t", "verify": ["false"]}
        _, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_merged": True, "adopted_specs": [spec]}),
            RULES, NOW,
        )
        self.assertNotIn("mark_task_requests_done", kinds(actions))

    def test_rejected_curriculum_leaves_requests_pending(self):
        """棄却・破棄された案の依頼は pending のまま (まだ叶えられていないので
        再挑戦してよい)。処理済み化は merge 実測のビートだけで行う。"""
        _, actions = reconcile.decide(
            doc(), facts(curriculum={"state": "curriculum_done", "pr": 7,
                                     "pr_open": False, "pr_merged": False}),
            RULES, NOW,
        )
        self.assertNotIn("mark_task_requests_done", kinds(actions))

    def test_active_below_cap_still_spawns_curriculum(self):
        """空きスロットがあれば走行中でも立案する (2026-08-22「がっつり並列」改定)。
        adopt_limit には空き数が載る。"""
        d, actions = reconcile.decide(
            doc(project(state="active", job="j"),),
            facts(jobs={"j": {"active": True}}), RULES, NOW,
        )
        spawns = [a for a in actions if a["type"] == "spawn_curriculum"]
        self.assertEqual(len(spawns), 1)
        self.assertEqual(
            spawns[0]["adopt_limit"], RULES["runner"]["max_concurrent"] - 1
        )

    def test_full_pipeline_blocks_curriculum(self):
        """パイプライン (窓待ちを除く非終端) が cap に達したら立案しない。"""
        import copy
        rules1 = copy.deepcopy(RULES)
        rules1["runner"]["max_concurrent"] = 2
        d, actions = reconcile.decide(
            doc(project(state="active", job="j"),
                project(id="P-0002", branch="project/p-0002", state="in_review")),
            facts(jobs={"j": {"active": True}}), rules1, NOW,
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
        # min_interval の運用値に依存しないよう「30 分前に空振り」で静穏を作る
        base = {"last_curriculum_at": "2026-08-07T11:30:00Z", "last_curriculum_dry": True}
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


class TestApprove(unittest.TestCase):
    """approve P-NNNN — 拒否権窓を人間の意思で畳む (2026-08-23)。

    可逆案の窓は空きスロットがあれば自動で繰り上がるので、これが効くのは実質
    不可逆案と満席のとき。承認は「窓を畳む」だけで、着手の可否は従来の判断に委ねる。
    """

    def _announced(self, **kw):
        base = dict(
            id="P-0001",
            state="announced",
            irreversible=True,
            veto_deadline=(NOW + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        base.update(kw)
        return project(**base)

    def test_approve_collapses_window_and_starts_irreversible(self):
        d = doc(self._announced())
        out, actions = reconcile.decide(d, facts(approves=["P-0001"]), RULES, NOW)

        self.assertEqual(out["projects"][0]["state"], "active")
        self.assertIn("spawn_runner", [a["type"] for a in actions])
        self.assertIn("approved_by_human_at", out["projects"][0])

    def test_without_approve_irreversible_keeps_waiting(self):
        # 承認が無ければ不可逆案は窓が明けるまで着手しない (従来どおり)
        d = doc(self._announced())
        out, actions = reconcile.decide(d, facts(), RULES, NOW)

        self.assertEqual(out["projects"][0]["state"], "announced")
        self.assertNotIn("spawn_runner", [a["type"] for a in actions])

    def test_approve_does_not_bypass_concurrency_limit(self):
        # 並列数は doc 上の active/in_review/merging の数で決まる (facts ではない)
        busy = [
            project(id=f"P-01{i:02d}", state="active", job=f"runner-p-01{i:02d}")
            for i in range(RULES["runner"]["max_concurrent"])
        ]
        d = doc(self._announced(), *busy)
        out, actions = reconcile.decide(d, facts(approves=["P-0001"]), RULES, NOW)

        approved = next(p for p in out["projects"] if p["id"] == "P-0001")
        self.assertEqual(approved["state"], "announced")
        # 他プロジェクトの spawn は無関係なので、承認した 1 件だけを見る
        spawned = [
            a for a in actions
            if a["type"] == "spawn_runner" and a.get("project") == "P-0001"
        ]
        self.assertEqual(spawned, [])
        # 窓は畳まれている (スロットが空いた次のビートで着手できる)
        self.assertIn("approved_by_human_at", approved)

    def test_veto_wins_over_approve_in_same_beat(self):
        d = doc(self._announced())
        out, _ = reconcile.decide(
            d, facts(approves=["P-0001"], vetoes=["P-0001"]), RULES, NOW
        )
        self.assertEqual(out["projects"][0]["state"], "vetoed")

    def test_stop_all_wins_over_approve(self):
        d = doc(self._announced())
        out, _ = reconcile.decide(
            d, facts(approves=["P-0001"], stop_all=True), RULES, NOW
        )
        self.assertEqual(out["projects"][0]["state"], "stalled")

    def test_approve_ignores_unknown_and_non_announced(self):
        # 走行中のものに approve が来ても状態を触らない
        d = doc(project(id="P-0002", state="active", job="runner-p-0002"))
        out, _ = reconcile.decide(d, facts(approves=["P-0002", "P-9999"]), RULES, NOW)
        self.assertEqual(out["projects"][0]["state"], "active")


class TestCoreCommands(unittest.TestCase):
    """コア発の command (設計 D3/D7/D21) の遷移表。

    常駐コアは git にも K8s にも書かない。実装依頼は bus に publish され、
    サイドカーがファイルに落とし、heart がここで初めて仕事にする。

    このテーブルが守るのは 4 つ:
      - task-request は tasks キューへの取り込み (ingest_command) になる
      - command_id の台帳で二重実行しない (同じ依頼で 2 つプロジェクトを立てない)
      - 停止中は 1 件も実行しない。台帳にも刻まないので再開後に拾い直す
      - 知らない種別は実行せず、しかし台帳には刻む (毎ビート通知が発振しない)
    """

    def command(self, **kw):
        base = {
            "command_id": "core-abc123",
            "type": "task-request",
            "source": "core",
            "issued_at": "2026-08-23T12:00:00Z",
            "title": "nats の掃除",
            "body": "ストリームが太っている",
        }
        base.update(kw)
        return base

    def ingests(self, actions):
        return [a for a in actions if a["type"] == "ingest_command"]

    def test_task_request_becomes_ingest_action(self):
        _, actions = reconcile.decide(
            doc(), facts(commands=[self.command()]), RULES, NOW
        )
        got = self.ingests(actions)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["command_id"], "core-abc123")
        self.assertEqual(got[0]["status"], "accepted")
        self.assertEqual(got[0]["command_type"], "task-request")
        self.assertEqual(got[0]["title"], "nats の掃除")
        self.assertEqual(got[0]["body"], "ストリームが太っている")

    def test_known_command_is_not_ingested_twice(self):
        """台帳に載っている id は二度と実行しない。ここが二重着手の唯一の歯止め。"""
        _, actions = reconcile.decide(
            doc(),
            facts(commands=[self.command()], processed_commands=["core-abc123"]),
            RULES, NOW,
        )
        self.assertEqual(self.ingests(actions), [])

    def test_duplicate_in_same_beat_is_ingested_once(self):
        _, actions = reconcile.decide(
            doc(), facts(commands=[self.command(), self.command()]), RULES, NOW
        )
        self.assertEqual(len(self.ingests(actions)), 1)

    def test_stop_engaged_blocks_execution(self):
        """人間が止めているのに新しい仕事を始めない。"""
        d = doc(stop_engaged=True)
        _, actions = reconcile.decide(d, facts(commands=[self.command()]), RULES, NOW)
        self.assertEqual(self.ingests(actions), [])

    def test_stop_all_in_same_beat_blocks_execution(self):
        _, actions = reconcile.decide(
            doc(), facts(commands=[self.command()], stop_all=True), RULES, NOW
        )
        self.assertEqual(self.ingests(actions), [])

    def test_command_survives_the_stop_and_runs_after_resume(self):
        """停止中は台帳にも刻まないので、再開したビートで同じ command を拾う。"""
        d = doc(stop_engaged=True)
        d, actions = reconcile.decide(d, facts(commands=[self.command()]), RULES, NOW)
        self.assertEqual(self.ingests(actions), [])

        d, actions = reconcile.decide(
            d, facts(commands=[self.command()], resume_all=True), RULES, NOW
        )
        self.assertEqual(len(self.ingests(actions)), 1)

    def test_unknown_type_is_recorded_but_not_executed(self):
        _, actions = reconcile.decide(
            doc(), facts(commands=[self.command(type="spawn-investigation")]), RULES, NOW
        )
        got = self.ingests(actions)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["status"], "unsupported")
        notes = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(len(notes), 1)
        self.assertIn("spawn-investigation", notes[0]["text"])

    def test_command_without_id_is_ignored(self):
        """id が無いものは台帳の鍵にできない = 二重実行の芽。実行しない。"""
        _, actions = reconcile.decide(
            doc(), facts(commands=[self.command(command_id="")]), RULES, NOW
        )
        self.assertEqual(self.ingests(actions), [])

    def test_no_commands_is_a_quiet_beat(self):
        _, actions = reconcile.decide(doc(), facts(), RULES, NOW)
        self.assertEqual(self.ingests(actions), [])


class AdmissionGateDecision(unittest.TestCase):
    """即時 dispatch の admission gate (設計 rev3 Phase D) の判定表。

    ここがコアに「いま着手してよいか」を数秒で答える純関数の仕様。
    強制は heart に残す、という設計判断 1 の実体なので、拒否の条件は
    すべてこの表に載せる。
    """

    def snapshot(self, **kw):
        base = {
            "at": reconcile.now_iso(NOW),
            "stop_engaged": False,
            "shadow": False,
            "running": 0,
            "max_concurrent": RULES["runner"]["max_concurrent"],
            "dispatch_ids": {},
        }
        base.update(kw)
        return base

    def request(self, **kw):
        base = {
            "title": "ops-dashboard の 500 を直す",
            "body": "snapshot API が 500 を返している。原因を特定して直す",
        }
        base.update(kw)
        return base

    def admit(self, request=None, snapshot="(default)", **kw):
        return reconcile.admit(
            request if request is not None else self.request(),
            self.snapshot() if snapshot == "(default)" else snapshot,
            RULES, NOW, **kw,
        )

    # --- 受理 ---
    def test_healthy_request_is_accepted(self):
        got = self.admit()
        self.assertEqual(got["status"], reconcile.ADMIT_ACCEPTED)
        self.assertTrue(got["dispatch_id"].startswith("d-"))

    # --- 人間の停止意思 ---
    def test_stop_engaged_denies_before_anything_else(self):
        """「止めて」は他のどの判定よりも先。満席でも空でも同じ理由で断る。"""
        for extra in ({}, {"running": 99}):
            got = self.admit(snapshot=self.snapshot(stop_engaged=True, **extra))
            self.assertEqual(got["status"], reconcile.ADMIT_DENIED)
            self.assertEqual(got["reason"], "stop_engaged")
            self.assertIn("再開", got["message"])

    def test_stop_engaged_wins_over_invalid_request(self):
        got = self.admit(self.request(title=""), self.snapshot(stop_engaged=True))
        self.assertEqual(got["reason"], "stop_engaged")

    # --- 並列上限 ---
    def test_capacity_denies_at_max_concurrent(self):
        limit = RULES["runner"]["max_concurrent"]
        got = self.admit(snapshot=self.snapshot(running=limit))
        self.assertEqual(got["status"], reconcile.ADMIT_DENIED)
        self.assertEqual(got["reason"], "capacity")
        self.assertIn(str(limit), got["message"])

    def test_inflight_counts_toward_capacity(self):
        """受理済みでまだビートに反映されていないぶんも走行数に数える。
        数えないと 1 ビート (60s) の間に上限を超えて Job が作れてしまう。"""
        limit = RULES["runner"]["max_concurrent"]
        got = self.admit(
            snapshot=self.snapshot(running=limit - 1), inflight={"d-other"},
        )
        self.assertEqual(got["reason"], "capacity")

    # --- capability の宣言連鎖 ---
    def test_capability_request_is_denied(self):
        got = self.admit(self.request(capabilities=["kubectl-write"]))
        self.assertEqual(got["status"], reconcile.ADMIT_DENIED)
        self.assertEqual(got["reason"], "capability_not_declared")

    # --- 冪等 ---
    def test_same_request_is_a_duplicate_not_a_new_project(self):
        first = self.admit()
        got = self.admit(
            snapshot=self.snapshot(dispatch_ids={first["dispatch_id"]: "P-9000"}),
        )
        self.assertEqual(got["status"], reconcile.ADMIT_DUPLICATE)
        self.assertIn("P-9000", got["message"])

    def test_same_request_in_flight_is_a_duplicate(self):
        first = self.admit()
        got = self.admit(inflight={first["dispatch_id"]})
        self.assertEqual(got["status"], reconcile.ADMIT_DUPLICATE)

    def test_dispatch_id_ignores_whitespace_but_not_content(self):
        a = self.admit(self.request(title="  同じ  "))
        b = self.admit(self.request(title="同じ"))
        c = self.admit(self.request(title="ちがう"))
        self.assertEqual(a["dispatch_id"], b["dispatch_id"])
        self.assertNotEqual(a["dispatch_id"], c["dispatch_id"])

    # --- レート制限 ---
    def test_rate_limit_denies_a_burst(self):
        recent = [
            reconcile.now_iso(NOW - timedelta(minutes=1))
            for _ in range(reconcile.DISPATCH_RATE_LIMIT)
        ]
        got = self.admit(recent=recent)
        self.assertEqual(got["reason"], "rate_limited")

    def test_rate_limit_forgets_outside_the_window(self):
        old = reconcile.now_iso(
            NOW - timedelta(minutes=reconcile.DISPATCH_RATE_WINDOW_MINUTES + 1)
        )
        got = self.admit(recent=[old] * (reconcile.DISPATCH_RATE_LIMIT * 2))
        self.assertEqual(got["status"], reconcile.ADMIT_ACCEPTED)

    # --- heart 側の状態が信用できないとき ---
    def test_no_snapshot_denies(self):
        got = self.admit(snapshot=None)
        self.assertEqual(got["reason"], "heart_not_ready")

    def test_stale_snapshot_denies(self):
        stale = reconcile.now_iso(
            NOW - timedelta(seconds=reconcile.DISPATCH_SNAPSHOT_MAX_AGE_SECONDS + 1)
        )
        got = self.admit(snapshot=self.snapshot(at=stale))
        self.assertEqual(got["reason"], "state_stale")

    def test_shadow_mode_denies(self):
        got = self.admit(snapshot=self.snapshot(shadow=True))
        self.assertEqual(got["reason"], "shadow_mode")

    # --- 要求そのものの不備 ---
    def test_empty_fields_are_denied_with_a_human_reason(self):
        for kw in ({"title": ""}, {"body": ""}):
            got = self.admit(self.request(**kw))
            self.assertEqual(got["reason"], "invalid")
            self.assertTrue(got["message"])

    def test_verify_is_not_required_and_does_not_change_the_id(self):
        """受入検証は取らない (2026-08-24 の所有者判断)。付いて来ても無視する。"""
        got = self.admit(self.request(verify=["test -f x"]))
        self.assertEqual(got["status"], reconcile.ADMIT_ACCEPTED)
        self.assertEqual(got["dispatch_id"], self.admit()["dispatch_id"])

    def test_oversized_fields_are_denied(self):
        got = self.admit(self.request(body="あ" * 5000))
        self.assertEqual(got["reason"], "invalid")


class DispatchFolding(unittest.TestCase):
    """gate が置いた dispatch の結末を projects.json に取り込む遷移。"""

    def record(self, **kw):
        base = {
            "dispatch_id": "d-abc123",
            "project_id": "P-9000",
            "requested_by": "core",
            "accepted_at": reconcile.now_iso(NOW),
            "title": "ops-dashboard の 500 を直す",
            "body": "直す",
            "verify": ["test -f ops/dashboard-fix.md"],
            "spec": {"id": "P-9000", "verify": ["test -f ops/dashboard-fix.md"]},
            "status": "dispatched",
            "job": "runner-p-9000-a1",
        }
        base.update(kw)
        return base

    def test_dispatched_record_becomes_an_active_project(self):
        d, actions = reconcile.decide(doc(), facts(dispatches=[self.record()]), RULES, NOW)
        self.assertEqual([p["id"] for p in d["projects"]], ["P-9000"])
        p = d["projects"][0]
        self.assertEqual(p["state"], "active")
        self.assertEqual(p["job"], "runner-p-9000-a1")
        self.assertEqual(p["capabilities"], [])
        self.assertEqual(p["requested_by"], "core")
        kinds = [a["type"] for a in actions]
        self.assertIn("consume_dispatch", kinds)
        # gate が既に Job を作っている。ここで二重に作らない
        self.assertNotIn("spawn_runner", kinds)

    def test_folding_is_idempotent(self):
        d, _ = reconcile.decide(doc(), facts(dispatches=[self.record()]), RULES, NOW)
        d, actions = reconcile.decide(d, facts(dispatches=[self.record()]), RULES, NOW)
        self.assertEqual(len(d["projects"]), 1)
        self.assertEqual(
            len([a for a in actions if a["type"] == "consume_dispatch"]), 1
        )

    def test_dispatched_project_counts_toward_max_concurrent(self):
        """折り込みは running を数える前。数えた後だと同じビートの spawn が
        上限を 1 本ぶん超える。"""
        limit = RULES["runner"]["max_concurrent"]
        waiting = [
            project(id=f"P-000{i}", state="announced",
                    veto_deadline=reconcile.now_iso(NOW - timedelta(hours=1)))
            for i in range(1, limit + 1)
        ]
        d, actions = reconcile.decide(
            doc(*waiting), facts(dispatches=[self.record()]), RULES, NOW
        )
        spawned = [a for a in actions if a["type"] == "spawn_runner"]
        self.assertEqual(len(spawned), limit - 1)

    def test_gate_rejected_record_lands_as_stalled(self):
        rec = self.record(status="gate_rejected", reason="adopt_gate_some_pass",
                          job=None, detail="開始前に pass していた")
        d, actions = reconcile.decide(doc(), facts(dispatches=[rec]), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p["stalled_reason"], "adopt_gate_some_pass")
        notes = [a for a in actions if a["type"] == "notify"]
        self.assertEqual(notes[0]["ntype"], "question")

    def test_stop_engaged_kills_a_dispatched_job(self):
        """ゲート通過後に「止めて」が来たら、折り込んだそのビートで殺す。"""
        d, actions = reconcile.decide(
            doc(stop_engaged=True), facts(dispatches=[self.record()]), RULES, NOW
        )
        kinds = [a["type"] for a in actions]
        self.assertIn("kill_job", kinds)
        self.assertEqual(d["projects"][0]["state"], "stalled")
        self.assertEqual(d["projects"][0]["stalled_reason"], "human_stop")

    def test_audit_records_who_asked(self):
        _, actions = reconcile.decide(doc(), facts(dispatches=[self.record()]), RULES, NOW)
        consume = [a for a in actions if a["type"] == "consume_dispatch"][0]
        self.assertEqual(consume["requested_by"], "core")
        self.assertTrue(
            all(line["requested_by"] == "core" for line in consume["audit"])
        )
        self.assertIn("spawn_runner", [line["action"] for line in consume["audit"]])


class TestDispatchSourceOfTruth(unittest.TestCase):
    """採択から着手までの経路に git が 1 つも無いこと (D32 / 4b-2b)。

    main への PR・CI・merge は消え、台帳 (archive.jsonl) への追記も止まった。
    棄却案の行き先は Project CR で、写しを取るのは heart.record_rejected。
    """

    SPEC = {"id": "P-0009", "title": "t", "why": "なぜ", "dod": "どこまで",
            "verify": ["false"], "cell": ["self", "repair"], "irreversible": False,
            "capabilities": [], "touches_apps": False, "confidence": "confident",
            "adopted": True}

    def cur(self, **kw):
        base = {"state": "curriculum_done", "at": "2026-08-07T11:59:00Z",
                "adopted_specs": [self.SPEC], "records": [self.SPEC]}
        base.update(kw)
        return base

    # --- 登録は git の何も待たない ---
    def test_registers_and_starts_moving_in_the_same_beat(self):
        d, actions = reconcile.decide(doc(), facts(curriculum=self.cur()), RULES, NOW)
        p = d["projects"][0]
        self.assertEqual(p["id"], "P-0009")
        self.assertEqual(p["state"], "proposed")
        # 着手に向けた歩みも同じビートで始まる (採択ゲートの実測)
        self.assertIn("run_adopt_gate", kinds(actions))

    def test_registration_happens_once_per_result(self):
        """同じ result を毎ビート読み直しても二重登録・二重通知をしない。"""
        d, _ = reconcile.decide(doc(), facts(curriculum=self.cur()), RULES, NOW)
        d, actions = reconcile.decide(d, facts(curriculum=self.cur()), RULES, NOW)
        self.assertEqual(len(d["projects"]), 1)
        self.assertNotIn("mark_task_requests_done", kinds(actions))

    def test_result_is_consumed_without_waiting_for_anything(self):
        d, actions = reconcile.decide(doc(), facts(curriculum=self.cur()), RULES, NOW)
        self.assertIn("consume_curriculum", kinds(actions))
        self.assertEqual(len(d["projects"]), 1)

    # --- runner が読む spec は doc に載る ---
    def test_spec_is_stored_in_projects_json(self):
        d, _ = reconcile.decide(doc(), facts(curriculum=self.cur()), RULES, NOW)
        self.assertEqual(d["projects"][0]["spec"], self.SPEC)

    def test_specs_from_the_cr_are_registered(self):
        """CR に居るのに doc に無い採択は登録される (復元後の埋め直し)。"""
        d, _ = reconcile.decide(doc(), facts(adopted_specs=[self.SPEC]), RULES, NOW)
        self.assertEqual(d["projects"][0]["spec"], self.SPEC)

    # --- 台帳への遅延追記は無くなった (4b-2b) ---
    def test_spawn_carries_no_ledger_backfill(self):
        """立案 Job に git の台帳を埋めさせる仕事はもう無い。"""
        p = project(id="P-9001", branch="project/p-9001", state="active",
                    spec={"id": "P-9001", "verify": ["false"]})
        _, actions = reconcile.decide(doc(p), facts(), RULES, NOW)
        spawn = [a for a in actions if a["type"] == "spawn_curriculum"][0]
        self.assertNotIn("archive_backfill", spawn)
