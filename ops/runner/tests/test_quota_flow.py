"""死因が制御を変える経路の試験 (P-0026 のレビュー指摘 [1][2][3])。

test_exit_reason.py が「文字列 → 死因」の契約なら、こちらは「死因 → 何をするか」。
上限 (usage_limit) を実装詰まりと読み違える経路が **initializer / worker ループ /
reviewer の 3 箇所**にあり、どれもループを止める:

  - initializer: 新規プロジェクトの最初のセッションこそ上限に当たりやすいのに、
    死ねば stalled + incident になっていた
  - worker ループ: 3 回連続 error に数えると上限だけで stalled になる
  - reviewer: verdict=fail に読み替えると review_cycles を消費し、
    max_cycles で review_rejected = ここでもループが止まる

Runner.__init__ は git/rules/gh を触るので、__init__ を通さない殻 (FakeRunner) に
セッションの結果列だけを与えて分岐を動かす。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.runner import runner as R

FAIL = [{"cmd": "c", "ok": False, "output": ""}]
PASS = [{"cmd": "c", "ok": True, "output": ""}]


class FakeRunner(R.Runner):
    """__init__ を通さずに mode_worker / mode_review の分岐だけを動かす殻。

    outcomes は `(outcome, failure_kind)` の列で、run_session が 1 回ずつ取り出す。
    verify_seq は run_verify が 1 回ずつ返す列 (尽きたら最後の値を返し続ける)。
    """

    def __init__(self, tmp, outcomes, verify_seq, session_max_seconds=7200,
                 initialized=False):
        self.mode = "worker"
        self.project_id = "P-TEST"
        self.branch = "project/p-test"
        self.model = "test-model"
        self.repo = "o/r"
        # git log / git diff は実際に走る (読むだけ)。tmp を渡すと非 git で落ちる
        self.repo_dir = R.REPO_ROOT
        self.data = tmp
        self.project_dir = tmp / "proj"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.doc_dir = tmp / "docs"
        self.doc_dir.mkdir(parents=True, exist_ok=True)
        self.project_md = self.doc_dir / "PROJECT.md"
        self.progress_md = self.doc_dir / "PROGRESS.md"
        if initialized:
            self.project_md.write_text("既に初期化済み\n")
        self.rules = {
            "runner": {
                "session_max_seconds": session_max_seconds,
                "max_sessions_per_project": 50,
                "inactivity_nudge_seconds": 60,
                "inactivity_kill_seconds": 120,
                # P-0141: rules.json runner.unknown_error_max_rounds と同じ値
                "unknown_error_max_rounds": 3,
            },
            "review": {"max_cycles": 3},
        }
        self.spec = {"verify": ["true"], "title": "テスト"}
        self.budget = R.Budget(50)
        self.last_session = {}
        self.outcomes = list(outcomes)
        self.verify_seq = list(verify_seq)
        self.tags = []

    # --- I/O の差し替え ---
    def checkout_branch(self):
        pass

    def push_if_committed(self):
        pass

    def ensure_pr(self):
        return 99

    def prompt_text(self, name, extra=None, from_main=False):
        return name

    def run_verify(self):
        return self.verify_seq.pop(0) if len(self.verify_seq) > 1 else self.verify_seq[0]

    def run_session(self, prompt, tag, cwd=None):
        self.budget.sessions += 1
        self.tags.append(tag)
        outcome, kind = self.outcomes.pop(0)
        self.last_session = {
            "failure_kind": kind,
            "stderr_tail": "Claude AI usage limit reached" if kind else "",
            "reset_at": None,
        }
        return outcome

    def result_doc(self):
        return json.loads((self.project_dir / "result.json").read_text())


class QuotaFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.addCleanup(self.tmpdir.cleanup)
        self.slept = []
        # R.time は共有の time モジュールなので、この patch はプロセス全体に効く。
        # subprocess.run(timeout=) が内部で行う指数ポーリング (0.001s×2^n) まで
        # 混入し、git が遅い環境で偽陽性を出した (2026-08-23 実測。CI でも再現)。
        # quota 待ちは常に 60 秒以上なので、1 秒未満は捨てて意図だけを記録する
        patcher = mock.patch.object(
            R.time, "sleep",
            lambda seconds: self.slept.append(seconds) if seconds >= 1 else None,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # mode_worker は REVIEW_FINDINGS を環境から読む。runner Job の中でこの
        # テストを走らせると差し戻し中の findings を拾って 1 セッション余分に回る
        # (実際にこのプロジェクトの worker セッションで踏んだ)。環境から切り離す
        env = mock.patch.dict(R.os.environ, {"REVIEW_FINDINGS": ""})
        env.start()
        self.addCleanup(env.stop)


class TestInitializerQuota(QuotaFlowTest):
    def test_usage_limit_waits_and_retries_instead_of_stalling(self):
        r = FakeRunner(
            self.tmp,
            outcomes=[("error", "usage_limit"), ("completed", None)],
            verify_seq=[FAIL, PASS],
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 0)
        self.assertEqual(r.result_doc()["state"], "ready_for_review")
        # 同じ initializer をもう一度出している (stalled にしていない)
        self.assertEqual(r.tags, ["s0-init", "s0-init"])
        self.assertEqual(self.slept, [R.DEFAULT_QUOTA_WAIT_SECONDS])

    def test_usage_limit_beyond_the_budget_yields_waiting_quota(self):
        # 待機予算 (session_max_seconds) に収まらないなら待たずに heart へ返す。
        # rc は 0 — Job を失敗させると heart 側が異常終了として数えてしまう
        r = FakeRunner(
            self.tmp,
            outcomes=[("error", "usage_limit")],
            verify_seq=[FAIL],
            session_max_seconds=100,
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 0)
        doc = r.result_doc()
        self.assertEqual(doc["state"], "waiting_quota")
        self.assertEqual(doc["failure_kind"], "usage_limit")
        self.assertTrue(doc["resume_after"].endswith("Z"))
        self.assertEqual(self.slept, [])

    def test_retries_still_respect_max_sessions(self):
        # 上限リトライは待機予算 (7200s) で有界だが、max_sessions_per_project も
        # 最後の歯止めとして残す (別軸の上限を上限リトライで外さない)
        r = FakeRunner(
            self.tmp, outcomes=[("error", "usage_limit")], verify_seq=[FAIL]
        )
        r.budget = R.Budget(1)
        rc = r.mode_worker()
        self.assertEqual(rc, 0)
        self.assertEqual(r.result_doc()["state"], "session_limit")
        self.assertEqual(r.tags, ["s0-init"])

    def test_other_failures_still_error_and_name_the_kind(self):
        # incident 通知の本文に出るのは error フィールドだけ。死因を本文に入れる。
        # P-0278 以降、error を書くのは 3 回連続で死んだ後 (下の TestInitializerRetry)
        r = FakeRunner(
            self.tmp, outcomes=[("error", "auth")] * 3, verify_seq=[FAIL]
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 1)
        doc = r.result_doc()
        self.assertEqual(doc["state"], "error")
        self.assertIn("failure_kind=auth", doc["error"])
        self.assertEqual(doc["failure_kind"], "auth")


class TestInitializerRetry(QuotaFlowTest):
    """initializer の有界リトライ (P-0278)。

    2026-08-24、runner-p-0278-a1 は s0-init が 1 回 error で死んだだけで
    プロジェクトを作業 1 行も無いまま stalled にした。同じ鍵・同じモデルの
    隣接実行 (07:56 の runner-p-0284-a1) は s1/s2 で同じ死因を食らいながら
    s3 で completed している — 一時的な失敗を 1 回で致命扱いしていた。
    本ループ (3 回連続 error まで耐える) との非対称を解消する。
    """

    def test_transient_death_is_retried_not_fatal(self):
        for kind in ("auth", "network", "unknown"):
            with self.subTest(kind=kind):
                r = FakeRunner(
                    self.tmp,
                    outcomes=[("error", kind), ("error", kind),
                              ("completed", None)],
                    verify_seq=[FAIL, PASS],
                )
                rc = r.mode_worker()
                self.assertEqual(rc, 0)
                self.assertEqual(r.result_doc()["state"], "ready_for_review")
                # 3 回とも同じ initializer を出し直している
                self.assertEqual(r.tags, ["s0-init"] * 3)
                # リトライは待たない (待つのは上限のときだけ)
                self.assertEqual(self.slept, [])

    def test_bound_is_three_and_then_error(self):
        for kind in ("auth", "network", "unknown"):
            with self.subTest(kind=kind):
                r = FakeRunner(
                    self.tmp, outcomes=[("error", kind)] * 3, verify_seq=[FAIL]
                )
                rc = r.mode_worker()
                self.assertEqual(rc, 1)
                doc = r.result_doc()
                self.assertEqual(doc["state"], "error")
                self.assertEqual(doc["failure_kind"], kind)
                self.assertIn("3 回連続", doc["error"])
                self.assertEqual(r.tags, ["s0-init"] * 3)

    def test_runner_killed_outcomes_are_also_retried(self):
        # session_timeout / inactive_killed も有界リトライの対象
        for outcome in ("session_timeout", "inactive_killed"):
            with self.subTest(outcome=outcome):
                r = FakeRunner(
                    self.tmp,
                    outcomes=[(outcome, None), ("completed", None)],
                    verify_seq=[FAIL, PASS],
                )
                self.assertEqual(r.mode_worker(), 0)
                self.assertEqual(r.result_doc()["state"], "ready_for_review")
                self.assertEqual(r.tags, ["s0-init"] * 2)

    def test_usage_limit_is_not_counted_toward_the_bound(self):
        # 上限は器の外側の事実。何回来ても error 連続には数えない (回帰防止)
        r = FakeRunner(
            self.tmp,
            outcomes=[("error", "usage_limit")] * 5 + [("completed", None)],
            verify_seq=[FAIL, PASS],
        )
        self.assertEqual(r.mode_worker(), 0)
        self.assertEqual(r.result_doc()["state"], "ready_for_review")
        self.assertEqual(self.slept, [R.DEFAULT_QUOTA_WAIT_SECONDS] * 5)

    def test_retries_still_respect_max_sessions(self):
        # 有界リトライで max_sessions_per_project (別軸の歯止め) を外さない
        r = FakeRunner(
            self.tmp, outcomes=[("error", "auth")] * 3, verify_seq=[FAIL]
        )
        r.budget = R.Budget(1)
        rc = r.mode_worker()
        self.assertEqual(rc, 0)
        self.assertEqual(r.result_doc()["state"], "session_limit")
        self.assertEqual(r.tags, ["s0-init"])


class TestSessionRetryAction(unittest.TestCase):
    """共有された純関数の 4 値マッピング (curriculum と initializer の共通部)。"""

    def test_completed_is_done(self):
        self.assertEqual(R.session_retry_action("completed", None, 0), "done")

    def test_usage_limit_always_waits(self):
        for n in range(R.SESSION_MAX_CONSECUTIVE_ERRORS + 3):
            self.assertEqual(
                R.session_retry_action("error", "usage_limit", n), "quota_wait"
            )

    def test_transient_kinds_share_one_bound(self):
        for kind in ("auth", "network", "unknown", None):
            with self.subTest(kind=kind):
                seen = [
                    R.session_retry_action("error", kind, n)
                    for n in range(R.SESSION_MAX_CONSECUTIVE_ERRORS)
                ]
                self.assertEqual(seen, ["retry", "retry", "give_up"])


class TestWorkerLoopQuota(QuotaFlowTest):
    def test_three_usage_limits_are_not_three_consecutive_errors(self):
        r = FakeRunner(
            self.tmp,
            outcomes=[("error", "usage_limit")] * 3 + [("completed", None)],
            verify_seq=[FAIL, FAIL, FAIL, FAIL, PASS],
            initialized=True,
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 0)
        self.assertEqual(r.result_doc()["state"], "ready_for_review")
        self.assertEqual(self.slept, [R.DEFAULT_QUOTA_WAIT_SECONDS] * 3)

    def test_real_errors_still_stall_after_three(self):
        # 上限の枝が本物の詰まりまで飲み込まないこと (ここが緩むと空回りが無限になる)。
        # P-0141 から unknown は既知死因とは別カウンタに移されたので、ここでは
        # 既知死因 (auth) で固定する。unknown の挙動は
        # ops/tests/test_unknown_death_probe.py が見る
        r = FakeRunner(
            self.tmp,
            outcomes=[("error", "auth")] * 3,
            verify_seq=[FAIL],
            initialized=True,
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 1)
        doc = r.result_doc()
        self.assertEqual(doc["state"], "error")
        self.assertIn("3 回連続", doc["error"])


class TestReviewerQuota(QuotaFlowTest):
    def test_usage_limit_does_not_become_a_failed_review(self):
        r = FakeRunner(
            self.tmp, outcomes=[("error", "usage_limit")], verify_seq=[PASS]
        )
        rc = r.mode_review()
        self.assertEqual(rc, 1)
        # review.json を書かない = heart の review タイムアウト再試行に任せる。
        # 書くと review_cycles が 1 減り、worker に偽の findings が渡る
        self.assertFalse((r.project_dir / "review.json").exists())

    def test_a_silent_reviewer_still_fails_the_review(self):
        r = FakeRunner(self.tmp, outcomes=[("completed", None)], verify_seq=[PASS])
        rc = r.mode_review()
        self.assertEqual(rc, 0)
        doc = json.loads((r.project_dir / "review.json").read_text())
        self.assertEqual(doc["verdict"], "fail")

    def test_verdict_written_before_the_limit_is_kept(self):
        r = FakeRunner(
            self.tmp, outcomes=[("error", "usage_limit")], verify_seq=[PASS]
        )
        (r.project_dir / "review.json").write_text(
            json.dumps({"verdict": "pass", "findings": []})
        )
        rc = r.mode_review()
        self.assertEqual(rc, 0)
        doc = json.loads((r.project_dir / "review.json").read_text())
        self.assertEqual(doc["verdict"], "pass")


class TestSpecWithoutVerify(QuotaFlowTest):
    """受入検証を持たない spec (dispatch 由来。2026-08-24 の所有者判断)。

    verify を書くのも LLM なので迂回でき、機械の判定として意味を成さない、
    という理由で dispatch 経路から外した。runner はこの形の spec でも
    止まらずに走り切り、作業を終えた時点で PR を出す。
    """

    def runner(self, outcomes, **kw):
        r = FakeRunner(self.tmp, outcomes=outcomes, verify_seq=[[]], **kw)
        r.spec = {"verify": [], "title": "所有者の依頼"}
        return r

    def test_empty_verify_does_not_trip_the_start_gate(self):
        """開始前の all-fail ゲートは素通り。測る基準が無いのは spec の不良ではない。"""
        r = self.runner([("completed", None), ("completed", None)])
        rc = r.mode_worker()
        self.assertEqual(rc, 0)
        self.assertNotEqual(r.result_doc()["state"], "spec_error")

    def test_one_finished_session_ships_a_pr(self):
        """完成の合図はセッションの正常終了。PR まで出してレビューへ渡す。"""
        r = self.runner([("completed", None), ("completed", None)])
        self.assertEqual(r.mode_worker(), 0)
        doc = r.result_doc()
        self.assertEqual(doc["state"], "ready_for_review")
        self.assertEqual(doc["pr"], 99)
        # initializer + worker を 1 回ずつ。何もせずに完成を宣言していない
        self.assertEqual(r.tags, ["s0-init", "s1"])

    def test_a_dead_session_does_not_ship(self):
        """セッションが死んだ回を「作業を終えた」と読み替えない。"""
        r = self.runner([("completed", None)] + [("error", "auth")] * 3)
        self.assertEqual(r.mode_worker(), 1)
        self.assertEqual(r.result_doc()["state"], "error")

    def test_review_is_not_force_failed_without_verify(self):
        """verify が無い spec に「wrapper 実測が green でない」を課さない
        (課すと reviewer が何と言おうと必ず fail になる)。"""
        r = self.runner([("completed", None)])
        r.mode = "review"
        (r.project_dir / "review.json").write_text(
            json.dumps({"verdict": "pass", "findings": []})
        )
        self.assertEqual(r.mode_review(), 0)
        doc = json.loads((r.project_dir / "review.json").read_text())
        self.assertEqual(doc["verdict"], "pass")


class TestShouldWithholdReview(unittest.TestCase):
    def test_table(self):
        cases = [
            (("usage_limit", False), True),
            (("usage_limit", True), False),   # verdict が在るなら有効な結果
            (("auth", False), False),         # 上限以外は従来通り fail を書く
            (("network", False), False),
            (("unknown", False), False),
            ((None, False), False),
        ]
        for (kind, exists), want in cases:
            with self.subTest(kind=kind, exists=exists):
                self.assertIs(R.should_withhold_review(kind, exists), want)


if __name__ == "__main__":
    unittest.main()
