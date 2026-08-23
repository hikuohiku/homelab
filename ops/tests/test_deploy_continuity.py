"""ops/tools/deploy_continuity.py の集計・安全弁・main 契約を固定する (P-0164)。

リポジトリルートから `python3 -m unittest ops.tests.test_deploy_continuity`。
CI (`unittest discover -s ops/tests`) でも走るため、**git も kubectl も使わない**。
外部コマンド層は FakeRunner (コマンド列を記録する CompletedProcess 工場) に差し替え、
fixture に無いコマンドは即座に失敗する。特に kubectl は既定で全拒否 — dry-run /
安全弁拒否の経路にクラスタ操作が混入したら、このテストが落として気づける。
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from ops.tools import deploy_continuity as dc


class FakeRunner:
    """部分文字列キーでレスポンスを返す runner。記録した呼び出しは calls に残る。

    responses の値は {"rc": int, "stdout": str}。どのキーにも一致しないコマンド、
    および fail_kubectl=True 時の kubectl コマンドは rc=1 (kubectl は例外) で失敗し、
    実装が想定外のコマンドを叩いても黙って通らない。
    """

    def __init__(self, responses=None, fail_kubectl=True):
        self.responses = responses or {}
        self.calls = []
        self.fail_kubectl = fail_kubectl

    def __call__(self, cmd, cwd=None, timeout=None):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if cmd[0] == "kubectl":
            if self.fail_kubectl:
                raise AssertionError("テストが kubectl を呼んだ: " + joined)
            key = next((k for k in self.responses if k.startswith("kubectl ") and k in joined), None)
            if key is None:
                return CompletedProcess(cmd, 1, stdout="", stderr="no fixture: " + joined)
            resp = self.responses[key]
            return CompletedProcess(cmd, resp.get("rc", 0), stdout=resp.get("stdout", ""), stderr="")
        for key, resp in self.responses.items():
            if key.startswith("kubectl "):
                continue
            if key in joined:
                return CompletedProcess(cmd, resp.get("rc", 0), stdout=resp.get("stdout", ""), stderr="")
        return CompletedProcess(cmd, 1, stdout="", stderr="no fixture: " + joined)


def projects_json(*projects):
    return json.dumps({"version": 1, "projects": list(projects)})


def p(pid, state):
    return {"id": pid, "state": state}


def git_fixtures(body, main_shas=("1111111111111111111111111111111111111111",)):
    ls = "".join("{}\trefs/heads/main\n".format(s) for s in main_shas)
    return {
        "git fetch": {"rc": 0},
        "git show": {"rc": 0, "stdout": body},
        "git ls-remote": {"rc": 0, "stdout": ls},
    }


class TestValve(unittest.TestCase):
    def test_self_is_excluded_others_block(self):
        """P-0164 自身は active でも弁は開く。他の active/announced は閉じる。"""
        projects = [
            p("P-0164", "active"),
            p("P-0092", "announced"),
            p("P-0100", "delivered"),
            p("P-0101", "stalled"),
            p("P-0102", "vetoed"),
            p("P-0116", "active"),
        ]
        blocked = dc.blocking_projects(projects)
        self.assertEqual(blocked, ["P-0092", "P-0116"])
        verdict = dc.valve_verdict(projects)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["blocking"], ["P-0092", "P-0116"])
        self.assertEqual(verdict["excluded_self"], ["P-0164"])

    def test_open_when_only_self_is_active(self):
        projects = [p("P-0164", "active"), p("P-0004", "delivered")]
        verdict = dc.valve_verdict(projects)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["blocking"], [])

    def test_literal_reading_available_by_dropping_exclusion(self):
        """dry-run --exclude-all 用。自己も数える文字通りの判定も取れる。"""
        projects = [p("P-0164", "active"), p("P-0004", "delivered")]
        verdict = dc.valve_verdict(projects, exclude=())
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["blocking"], ["P-0164"])

    def test_empty_projects_open(self):
        self.assertTrue(dc.valve_verdict([])["ok"])


class TestParseProjects(unittest.TestCase):
    def test_ok(self):
        projects = dc.parse_projects(projects_json(p("P-0164", "active")))
        self.assertEqual(projects[0]["id"], "P-0164")

    def test_non_dict_root_raises(self):
        with self.assertRaises(ValueError):
            dc.parse_projects(json.dumps([1, 2]))

    def test_missing_projects_key_raises(self):
        with self.assertRaises(ValueError):
            dc.parse_projects(json.dumps({"version": 1}))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            dc.parse_projects("not json")


class TestTimeUtils(unittest.TestCase):
    def test_seconds_between(self):
        self.assertEqual(dc.seconds_between(
            "2026-08-23T06:00:00+00:00", "2026-08-23T06:01:30.500+00:00"), 90.5)

    def test_z_suffix_and_offset_equivalence(self):
        self.assertEqual(dc.seconds_between(
            "2026-08-23T06:00:00Z", "2026-08-23T06:00:10+00:00"), 10.0)

    def test_negative_is_detectable(self):
        self.assertLess(dc.seconds_between(
            "2026-08-23T06:00:10Z", "2026-08-23T06:00:00Z"), 0)


T_DOWN = "2026-08-23T06:00:00+00:00"
T_UP = "2026-08-23T06:05:00+00:00"
SHA_BASE = "a" * 40


def measurement(apps=None, **over):
    m = {
        "ran_at": T_DOWN,
        "base_main_sha": SHA_BASE,
        "new_main_shas": ["b" * 40, "c" * 40],
        "commits_landed_at": "2026-08-23T06:03:00+00:00",
        "down_confirmed_at": T_DOWN,
        "up_issued_at": T_UP,
        "apps": apps or [
            {"name": "vaultwarden",
             "refreshed_at": "2026-08-23T06:05:30+00:00",
             "synced_at": "2026-08-23T06:06:10+00:00"},
            {"name": "coder",
             "refreshed_at": "2026-08-23T06:05:20+00:00",
             "synced_at": "2026-08-23T06:05:50+00:00"},
        ],
        "self_heal_restored": False,
    }
    m.update(over)
    return m


class TestBuildReport(unittest.TestCase):
    def test_happy_path_numbers(self):
        r = dc.build_report(measurement())
        self.assertEqual(r["downtime_seconds"], 300.0)
        # scale 1 発行から最後の label 到達 (vaultwarden 06:06:10) まで
        self.assertEqual(r["catchup_seconds"], 70.0)
        # 全アプリの refresh 完了は coder 06:05:20 → vaultwarden 06:05:30
        self.assertEqual(r["refresh_seconds"], 30.0)
        self.assertFalse(r["missed_changes"])
        self.assertEqual(r["caught_up_at"], "2026-08-23T06:06:10+00:00")
        self.assertEqual(r["sync_order"], ["coder", "vaultwarden"])
        self.assertEqual([a["name"] for a in r["apps"]], ["vaultwarden", "coder"])
        self.assertEqual(r["targets"][2],
                         {"kind": "statefulset", "name": "argocd-application-controller"})

    def test_missed_sync_makes_catchup_null(self):
        apps = [
            {"name": "vaultwarden", "refreshed_at": "2026-08-23T06:05:30+00:00",
             "synced_at": "2026-08-23T06:06:10+00:00"},
            {"name": "coder", "refreshed_at": None, "synced_at": None},
        ]
        r = dc.build_report(measurement(apps=apps))
        self.assertTrue(r["missed_changes"])
        self.assertIsNone(r["catchup_seconds"])
        self.assertIsNone(r["caught_up_at"])
        self.assertEqual(r["refresh_seconds"], 30.0)
        self.assertEqual(r["sync_order"], ["vaultwarden"])

    def test_validate_passes_on_happy_path(self):
        self.assertEqual(dc.validate_report(dc.build_report(measurement())), [])

    def test_validate_passes_with_missed_changes(self):
        apps = [{"name": "coder", "refreshed_at": "2026-08-23T06:05:20+00:00",
                 "synced_at": None}]
        report = dc.build_report(measurement(apps=apps))
        self.assertEqual(dc.validate_report(report), [])


class TestValidateReport(unittest.TestCase):
    def good(self, **over):
        r = dc.build_report(measurement())
        r.update(over)
        return r

    def test_missing_catchup_key(self):
        report = self.good()
        del report["catchup_seconds"]
        errors = dc.validate_report(report)
        self.assertTrue(any("catchup_seconds" in e for e in errors), errors)

    def test_negative_seconds(self):
        errors = dc.validate_report(self.good(downtime_seconds=-5))
        self.assertTrue(any("downtime_seconds" in e for e in errors), errors)

    def test_unsynced_app_without_missed_flag(self):
        report = self.good(missed_changes=False)
        report["apps"][1]["synced_at"] = None
        errors = dc.validate_report(report)
        self.assertTrue(any("missed_changes" in e for e in errors), errors)

    def test_time_disorder_down_after_up(self):
        errors = dc.validate_report(self.good(down_confirmed_at=T_UP, up_issued_at=T_DOWN))
        self.assertTrue(any("up_issued_at" in e for e in errors), errors)

    def test_sync_order_mismatch(self):
        report = self.good()
        report["sync_order"] = ["vaultwarden", "coder", "immich"]
        errors = dc.validate_report(report)
        self.assertTrue(any("sync_order" in e for e in errors), errors)

    def test_catchup_null_requires_missed_flag(self):
        report = self.good(catchup_seconds=None)
        errors = dc.validate_report(report)
        self.assertTrue(errors, "null 許容は missed_changes=True のときだけ")

    def test_required_keys_enforced(self):
        report = self.good()
        del report["base_main_sha"]
        errors = dc.validate_report(report)
        self.assertTrue(any("base_main_sha" in e for e in errors), errors)


class TestScaleGuard(unittest.TestCase):
    """spec 制約: kubectl write は TARGETS への 0/1 scale のみ。"""

    def call(self, kind, name, replicas, runner=None):
        runner = runner or FakeRunner(fail_kubectl=False)
        dc.k_scale(runner, kind, name, replicas)
        return runner

    def test_allowed_targets_scale_zero_and_one(self):
        # scale コマンド自体は fixture で rc=0 を返す (許可対象の実行まで見る)
        runner = FakeRunner({"kubectl scale": {"rc": 0}}, fail_kubectl=False)
        for t in dc.TARGETS:
            self.call(t["kind"], t["name"], 0, runner)
            self.call(t["kind"], t["name"], 1, runner)
        scaled = [c for c in runner.calls if c[:2] == ["kubectl", "scale"]]
        self.assertEqual(len(scaled), 6)
        self.assertTrue(all(any(a.startswith("--replicas=") for a in c) for c in scaled))
        self.assertTrue(all("-n" in c and "argocd" in c for c in scaled))

    def test_forbidden_component_rejected(self):
        for bad in (("deployment", "argocd-dex-server"), ("deployment", "argocd-redis"),
                    ("statefulset", "anything")):
            with self.assertRaises(ValueError):
                self.call(bad[0], bad[1], 0)

    def test_replicas_beyond_0_1_rejected(self):
        with self.assertRaises(ValueError):
            self.call("deployment", "argocd-server", 3)

    def test_statefulset_kind_is_real(self):
        """実測 (2026-08-23): controller は Deployment ではなく StatefulSet。"""
        kinds = {(t["kind"], t["name"]) for t in dc.TARGETS}
        self.assertIn(("statefulset", "argocd-application-controller"), kinds)
        self.assertIn(("deployment", "argocd-server"), kinds)
        self.assertIn(("deployment", "argocd-repo-server"), kinds)
        self.assertEqual(len(kinds), 3)


def run_main(argv, runner):
    buf = io.StringIO()
    err = io.StringIO()
    with mock.patch.object(dc, "default_runner", runner), \
         redirect_stdout(buf), redirect_stderr(err):
        rc = dc.main(argv)
    return rc, buf.getvalue(), err.getvalue()


class TestDryRunContract(unittest.TestCase):
    """verify 第 3 項: クラスタ書き込み無しで完走し、弁の判定が見えること。"""

    def test_completes_without_cluster_and_valve_visible(self):
        """クラスタ到達不能でも完走 (rc=0)。kubectl は読み取り (get) 以外禁止。"""
        body = projects_json(p("P-0164", "active"), p("P-0004", "delivered"))
        # kubectl は許可するが fixture 無し (rc=1) — 読み取り失敗を dry-run が
        # skipped として吸収して完走することも一緒に確認する
        runner = FakeRunner(git_fixtures(body), fail_kubectl=False)
        rc, out, _ = run_main(["--dry-run"], runner)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertFalse(data["cluster_writes"])
        self.assertTrue(data["valve"]["ok"])
        self.assertEqual(data["valve"]["excluded_self"], ["P-0164"])
        kubectl_cmds = [c for c in runner.calls if c[0] == "kubectl"]
        self.assertTrue(kubectl_cmds, "対象の実在確認 (get) を試みているはず")
        self.assertTrue(all(c[1] == "get" for c in kubectl_cmds),
                        "dry-run が get 以外の kubectl を呼んだ: {}".format(kubectl_cmds))

    def test_blocked_valve_still_reports_clearly(self):
        """弁が閉じていても dry-run は完走する (判定を見せるのが仕事)。"""
        body = projects_json(
            p("P-0164", "active"),
            p("P-0116", "active"),
            p("P-0157", "active"),
            p("P-0158", "announced"),
        )
        runner = FakeRunner(git_fixtures(body))
        rc, out, _ = run_main(["--dry-run"], runner)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertFalse(data["valve"]["ok"])
        self.assertEqual(data["valve"]["blocking"],
                         ["P-0116", "P-0157", "P-0158"])

    def test_git_failure_is_not_silent(self):
        runner = FakeRunner({"git fetch": {"rc": 128, "stdout": "", "stderr": "boom"}})
        rc, out, _ = run_main(["--dry-run"], runner)
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertIsNone(data["valve"]["ok"])
        self.assertIn("error", data["valve"])

    def test_exclude_all_shows_literal_verdict(self):
        body = projects_json(p("P-0164", "active"))
        runner = FakeRunner(git_fixtures(body))
        _, out, _ = run_main(["--dry-run", "--exclude-all"], runner)
        data = json.loads(out)
        # exclude_all では「文字通りの判定」を上書きして見せる
        self.assertFalse(data["valve"]["ok"])


class TestRunRefusal(unittest.TestCase):
    """安全弁が閉いているとき --run は何も触らず rc=2。"""

    def test_refuses_before_touching_cluster(self):
        body = projects_json(p("P-0164", "active"), p("P-0163", "active"))
        runner = FakeRunner(git_fixtures(body))  # kubectl は即例外
        with tempfile.TemporaryDirectory() as d:
            out_path = str(Path(d) / "report.json")
            rc, out, _ = run_main(["--run", "--out", out_path], runner)
            self.assertEqual(rc, 2)
            data = json.loads(out)
            self.assertFalse(data["started"])
            self.assertFalse(Path(out_path).exists(), "拒否されたのにレポートを書いた")
        self.assertTrue(all(c[0] != "kubectl" for c in runner.calls),
                        "弁の拒否経路で kubectl を呼んだ: {}".format(runner.calls))


class TickClock:
    """呼ぶたびに step 秒進む clock。sleep は時間だけ進めて何もしない。"""

    def __init__(self, step=10.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t

    @staticmethod
    def sleep(_seconds):
        pass


class ShaRunner:
    """ls-remote のたびに列の次を返し、尽きたら最後の SHA を維持する runner。"""

    def __init__(self, base_sha, shas=()):
        self.seq = [base_sha] + list(shas)
        self.i = 0
        self.ls_calls = 0

    def __call__(self, cmd, cwd=None, timeout=None):
        if cmd[:2] == ["git", "ls-remote"]:
            sha = self.seq[min(self.i, len(self.seq) - 1)]
            self.i += 1
            self.ls_calls += 1
            return CompletedProcess(cmd, 0,
                                    stdout="{}\trefs/heads/main\n".format(sha), stderr="")
        return CompletedProcess(cmd, 0, stdout="", stderr="")


class TestWatchMainAdvance(unittest.TestCase):
    BASE = "a" * 40

    def test_two_commits_then_settle(self):
        s1, s2 = "b" * 40, "c" * 40
        runner = ShaRunner(self.BASE, [s1, s2])
        result = dc.watch_main_advance(
            runner, self.BASE, commit_count=2, max_wait=10000, settle=20,
            clock=TickClock(), sleep=TickClock.sleep, now=lambda: "T-SETTLED",
        )
        self.assertEqual(result, ([s1, s2], "T-SETTLED"))

    def test_no_movement_times_out(self):
        runner = ShaRunner(self.BASE)
        result = dc.watch_main_advance(
            runner, self.BASE, commit_count=2, max_wait=100, settle=20,
            clock=TickClock(step=25), sleep=TickClock.sleep, now=lambda: "T",
        )
        self.assertIsNone(result)

    def test_single_commit_is_returned_for_caller_to_judge(self):
        """足りない判定は呼び出し側。観測できた分は潰さず返す (エラーに残すため)。"""
        s1 = "b" * 40
        runner = ShaRunner(self.BASE, [s1])
        result = dc.watch_main_advance(
            runner, self.BASE, commit_count=2, max_wait=10000, settle=20,
            clock=TickClock(), sleep=TickClock.sleep, now=lambda: "T",
        )
        self.assertEqual(result[0], [s1])


def status_json(replicas, ready):
    return json.dumps({"spec": {"replicas": replicas},
                       "status": {"readyReplicas": ready}})


class TestRestoreScaleOne(unittest.TestCase):
    def responses(self, per_target):
        out = {"git fetch": {"rc": 0}}
        for kind, name in (("deployment", "argocd-server"),
                           ("deployment", "argocd-repo-server"),
                           ("statefulset", "argocd-application-controller")):
            key = "kubectl get {} {}".format(kind, name)
            spec = per_target.get(name)
            if spec is not None:
                out[key] = {"rc": 0, "stdout": status_json(*spec)}
        return out

    def names(self, runner):
        return [c[2] for c in runner.calls if c[:2] == ["kubectl", "scale"]]

    def test_all_healthy_touches_nothing(self):
        body = projects_json(p("P-0004", "delivered"))
        resp = git_fixtures(body)
        resp.update(self.responses({n: (1, 1) for n in (
            "argocd-server", "argocd-repo-server", "argocd-application-controller")}))
        runner = FakeRunner(resp, fail_kubectl=False)
        state = dc.restore_scale_one(runner, timeout=1, clock=TickClock(),
                                     sleep=TickClock.sleep)
        self.assertEqual(state["restored"], [])
        self.assertTrue(state["ready"])
        self.assertEqual(self.names(runner), [])

    def test_scales_only_whatever_is_still_zero(self):
        body = projects_json(p("P-0004", "delivered"))
        resp = git_fixtures(body)
        resp.update(self.responses({
            "argocd-server": (1, 1),
            # scale 発行後も fixture は静的なので ready にはならない (timeout 経路)
            "argocd-repo-server": (0, 0),
            "argocd-application-controller": (1, 1),
        }))
        resp["kubectl scale"] = {"rc": 0}
        runner = FakeRunner(resp, fail_kubectl=False)
        state = dc.restore_scale_one(runner, timeout=1, poll_interval=0,
                                     clock=TickClock(step=100), sleep=TickClock.sleep)
        self.assertEqual(state["restored"], ["argocd-repo-server"])
        self.assertFalse(state["ready"])
        self.assertEqual(self.names(runner),
                         ["deployment/argocd-repo-server"])


if __name__ == "__main__":
    unittest.main()
