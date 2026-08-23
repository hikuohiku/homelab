"""P-0182 — budget_exhausted は終端ではなく休憩。遷移表テスト。

reconcile.decide() が予算死をどう扱うかを固定する:
  - checkpoint / PROGRESS.md がブランチに確認できる予算死は、継続回数の上限未満なら
    stalled 化せず proposed へ戻す (以後は通常の採択ゲート→予告→veto 窓に合流。
    勝手な再走ではなく、再予告は人間の veto 対象)
  - 上限到達・レーン無効・証拠無し (initializer 中の予算死など) は従来どおり stalled
  - human_stop / veto は絶対に継続しない (全遷移より先に評価される既存の順序契約)
  - 観測に失敗した (None) ビートでは判断しない (jobs=None と同じ規約)

観測側 (observation.collect_continuation) は実 git リポジトリで見る。runner は checkpoint
セッションの push 後に result.json を書くため、「result は届いたが証拠がまだ読めない」
取り逃しが起きないことを含めて仕様。

リポジトリルートから `python3 -m unittest ops.tests.test_budget_continuation`
(CI は discover -s ops/tests -t .)。
"""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ops.heart import gitutil, reconcile
from ops.heart import facts as observation

RULES_PATH = Path(__file__).resolve().parents[1] / "rules.json"
with open(RULES_PATH) as f:
    RULES = json.load(f)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)

BUDGET_RESULT = {"state": "budget_exhausted"}


def project(**kw):
    base = {
        "id": "P-0001",
        "title": "テスト",
        "state": "active",
        "branch": "project/p-0001",
        "irreversible": False,
        "capabilities": [],
        "budget": {"used_tokens": 900000, "soft_cap": 1000000},
        "created": "2026-08-20",
        "job": "runner-p-0001-a1",
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
        "continuation_evidence": {},
    }
    base.update(kw)
    return base


def rules_with(enabled=True, extra=2000000, max_continuations=1):
    """遷移表を運用値 (rules.json) の変更から独立させる。"""
    r = copy.deepcopy(RULES)
    r["runner"]["continuation"] = {
        "enabled": enabled,
        "extra_budget_tokens": extra,
        "max_continuations": max_continuations,
    }
    return r


def kinds(actions):
    return [a["type"] for a in actions]


def for_project(actions, pid):
    return [a for a in actions if a.get("project") == pid]


class TestConfig(unittest.TestCase):
    def test_rules_ship_the_continuation_knob(self):
        """単一情報源に器が置かれていること (DoD 1)。値の正否は運用の判断だが、
        型と下限はここで固定する。"""
        c = RULES["runner"]["continuation"]
        self.assertIsInstance(c["enabled"], bool)
        self.assertIsInstance(c["extra_budget_tokens"], int)
        self.assertGreater(c["extra_budget_tokens"], 0)
        self.assertIsInstance(c["max_continuations"], int)
        self.assertGreaterEqual(c["max_continuations"], 1)


class TestContinuationFires(unittest.TestCase):
    def test_checkpointed_death_returns_to_proposed_with_fresh_budget(self):
        """継続発火: 証拠あり・上限未満なら stalled にせず proposed へ戻す。
        通知 (question) は出さない — 人間への再予告は後続ビートの announce が担う。"""
        rules = rules_with(extra=2000000, max_continuations=1)
        p = project()
        d, actions = reconcile.decide(
            doc(p),
            facts(results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": True}),
            rules, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "proposed")
        self.assertNotIn("stalled_reason", p)
        self.assertEqual(p["continuation_count"], 1)
        self.assertEqual(p["budget"]["soft_cap"], 1000000 + 2000000)
        mine = for_project(actions, "P-0001")
        self.assertEqual(kinds(mine), ["consume_result"])

    def test_lane_rejoins_the_normal_flow_on_following_beats(self):
        """継続後は通常経路に合流する: 次ビートで採択ゲートが再実測され、
        その後に予告→着手へ進む。"""
        rules = rules_with()
        d, _ = reconcile.decide(
            doc(project()),
            facts(results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": True}),
            rules, NOW,
        )
        d, actions = reconcile.decide(d, facts(), rules, NOW)
        self.assertEqual(d["projects"][0]["state"], "proposed")
        self.assertIn("run_adopt_gate", kinds(actions))
        self.assertNotIn("spawn_runner", kinds(actions))

    def test_transient_fields_are_dropped_and_gate_is_remasured(self):
        """一過性フィールドを落とし、初回採択時のゲート測定を流用しない
        (「信念でなく実測」。試行カウンタも新規扱いに戻す)。prs 履歴は残す。"""
        rules = rules_with()
        p = project(
            job="runner-p-0001-a1",
            drift_count=2,
            restart_count=1,
            veto_deadline="2026-08-21T00:00:00Z",
            adopt_gate={"at": "2026-08-20T00:00:00Z", "verify": []},
            adopt_gate_attempts=1,
            prs=[42],
        )
        d, _ = reconcile.decide(
            doc(p),
            facts(results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": True}),
            rules, NOW,
        )
        p = d["projects"][0]
        for k in ("job", "drift_count", "restart_count",
                  "veto_deadline", "adopt_gate",
                  "adopt_gate_attempts"):
            self.assertNotIn(k, p)
        self.assertEqual(p["prs"], [42])
        self.assertEqual(p["last_continuation_at"], "2026-08-23T12:00:00Z")


class TestContinuationHolds(unittest.TestCase):
    def test_limit_reached_stays_stalled(self):
        """上限到達: 継続済みの案件は従来どおり stalled + question。"""
        rules = rules_with(max_continuations=1)
        p = project(continuation_count=1)
        d, actions = reconcile.decide(
            doc(p),
            facts(results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": True}),
            rules, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p["stalled_reason"], "budget_exhausted")
        self.assertIn("consume_result", kinds(actions))
        notes = [a for a in actions if a["type"] == "notify"]
        self.assertEqual([n["ntype"] for n in notes], ["question"])

    def test_no_checkpoint_stays_stalled(self):
        """checkpoint 無し: initializer 中の予算死など、継続するものが無い死は
        stalled のまま (何もない所から再走させない)。"""
        rules = rules_with()
        d, actions = reconcile.decide(
            doc(project()),
            facts(results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": False}),
            rules, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p["stalled_reason"], "budget_exhausted")
        self.assertIn("consume_result", kinds(actions))

    def test_disabled_lane_stays_stalled(self):
        """enabled=False なら証拠があっても従来どおり。"""
        rules = rules_with(enabled=False)
        d, _ = reconcile.decide(
            doc(project()),
            facts(results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": True}),
            rules, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p["stalled_reason"], "budget_exhausted")

    def test_unobservable_evidence_defers_judgment(self):
        """観測失敗 (None) は「無い」と区別する。判断も消費もせず次のビートに委ねる
        (誤 stalled の防止。jobs=None と同じ規約)。"""
        rules = rules_with()
        d, actions = reconcile.decide(
            doc(project()),
            facts(results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": None}),
            rules, NOW,
        )
        self.assertEqual(d["projects"][0]["state"], "active")
        self.assertEqual(for_project(actions, "P-0001"), [])


class TestHumanStopWins(unittest.TestCase):
    """human_stop / veto は絶対に継続しない。停止チェックは全遷移より先 (既存の
    順序契約) なので、同ビートに予算死の結果が届っていても停止が勝つ。"""

    def test_stop_all_stalls_instead_of_continuing(self):
        rules = rules_with()
        d, actions = reconcile.decide(
            doc(project(job="runner-p-0001-a1")),
            facts(stop_all=True,
                  results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": True}),
            rules, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "stalled")
        self.assertEqual(p.get("stalled_reason"), "human_stop")
        self.assertNotIn("continuation_count", p)
        self.assertNotIn("consume_result", for_project(actions, "P-0001"))

    def test_veto_vetoes_instead_of_continuing(self):
        rules = rules_with()
        d, actions = reconcile.decide(
            doc(project(job="runner-p-0001-a1")),
            facts(vetoes=["P-0001"],
                  results={"P-0001": BUDGET_RESULT},
                  continuation_evidence={"P-0001": True}),
            rules, NOW,
        )
        p = d["projects"][0]
        self.assertEqual(p["state"], "vetoed")
        self.assertNotIn("continuation_count", p)
        self.assertNotIn("consume_result", for_project(actions, "P-0001"))


class TestCollectContinuation(unittest.TestCase):
    """観測側。実 git リポジトリ (local の bare origin) で見る。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p0182-collect-"))
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        gitutil.run(["init", "--bare", "-q", str(self.origin)])
        gitutil.run(["clone", "-q", str(self.origin), str(self.work)])
        gitutil.run(["config", "user.email", "t@example.com"], cwd=self.work)
        gitutil.run(["config", "user.name", "t"], cwd=self.work)
        (self.work / "README.md").write_text("x\n")
        gitutil.run(["add", "README.md"], cwd=self.work)
        gitutil.run(["commit", "-q", "-m", "init"], cwd=self.work)
        gitutil.run(["push", "-q", "origin", "HEAD"], cwd=self.work)
        # 既定ブランチの名前は git の設定次第で変わるので名前で決め打ちしない
        self.base = gitutil.run(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.work
        )

    def _push_branch(self, branch, with_progress):
        gitutil.run(["checkout", "-q", "-b", branch], cwd=self.work)
        if with_progress:
            path = self.work / "ops" / "projects" / "logs" / "P-0002" / "PROGRESS.md"
            path.parent.mkdir(parents=True)
            path.write_text("# P-0002 PROGRESS\n\n## checkpoint (予算上限)\n")
            gitutil.run(["add", "-A"], cwd=self.work)
            gitutil.run(["commit", "-q", "-m", "checkpoint"], cwd=self.work)
        gitutil.run(["push", "-q", "origin", branch], cwd=self.work)
        gitutil.run(["checkout", "-q", self.base], cwd=self.work)

    def _collect(self, **proj_kw):
        p = project(id="P-0002", branch="project/p-0002", **proj_kw)
        results = {"P-0002": BUDGET_RESULT}
        return observation.collect_continuation(self.work, doc(p), results)

    def test_progress_md_on_branch_is_evidence(self):
        self._push_branch("project/p-0002", with_progress=True)
        self.assertEqual(self._collect(), {"P-0002": True})

    def test_branch_without_progress_md_is_not_evidence(self):
        self._push_branch("project/p-0002", with_progress=False)
        self.assertEqual(self._collect(), {"P-0002": False})

    def test_branch_never_pushed_is_not_evidence(self):
        """initializer 中の予算死。何も push されていないので継続の候補にならない。"""
        self.assertEqual(self._collect(), {"P-0002": False})

    def test_only_active_budget_deaths_are_observed(self):
        """観測対象は「active かつ budget_exhausted の結果持ち」だけ。
        proposed や結果の無い active は観測せず、出力に載らない。"""
        self._push_branch("project/p-0002", with_progress=True)
        other = [
            project(id="P-0002", branch="project/p-0002", state="proposed"),
            project(id="P-0003", branch="project/p-0003"),
        ]
        results = {"P-0002": BUDGET_RESULT}
        self.assertEqual(
            observation.collect_continuation(self.work, doc(*other), results), {}
        )

    def test_broken_repo_observation_is_none_not_false(self):
        """観測に失敗したら None (「無い」と区別する)。False だと誤 stalled になる。"""
        p = project(id="P-0002", branch="project/p-0002")
        results = {"P-0002": BUDGET_RESULT}
        out = observation.collect_continuation(
            str(self.tmp / "no-such-repo"), doc(p), results
        )
        self.assertEqual(out, {"P-0002": None})


if __name__ == "__main__":
    unittest.main()
