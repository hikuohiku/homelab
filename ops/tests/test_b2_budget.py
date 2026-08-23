"""ops/b2/budget.py (P-0216, B2 download cap の事前歯止め) の純関数と実リポジトリを固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_b2_budget`。
budget.py は単一ファイルスクリプトのため importlib で実ファイルをロードする
(test_download_budget.py と同じ形)。

固定する契約:
- cron 式の JST→UTC 換算: 日跨ぎ (日曜 JST 深夜 = 土曜 UTC 夜) を落とさない
- 密集・境界・未登録・stale の各検査が両方向に鳴る/鳴らない
- cap 未設定なら合計検査は沈黙する (決め打ちしない)
- 実リポジトリ: 抽出台帳は REGISTRY と一致し、--check が rc=0
  (schedule 分散済みの現状が「cap を食い潰すスケジュール」でないことの固定)
"""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "ops" / "b2" / "budget.py"
APPS_DIR = REPO / "apps"


def _load_module():
    spec = importlib.util.spec_from_file_location("b2_budget_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


b = _load_module()

MIB = 1024 * 1024


def consumer(name, fires):
    return {"name": name, "namespace": "ns", "file": "f.yaml",
            "cron": "?", "fires": fires}


def daily(minute_of_day):
    return [(d, minute_of_day) for d in range(7)]


class ParseScheduleTest(unittest.TestCase):
    def test_daily_jst_evening_becomes_utc_afternoon(self):
        # immich-restic-backup の実値: 毎日 02:45 JST = 前日 17:45 UTC
        fires = b.parse_schedule("45 2 * * *")
        self.assertEqual(sorted(fires), [(d, 17 * 60 + 45) for d in range(7)])

    def test_sunday_jst_night_is_saturday_utc(self):
        # immich-restic-retention の旧値: 日曜 03:45 JST = 土曜 18:45 UTC
        self.assertEqual(b.parse_schedule("45 3 * * 0"), [(6, 18 * 60 + 45)])

    def test_dow_7_means_sunday(self):
        self.assertEqual(b.parse_schedule("45 3 * * 7"), b.parse_schedule("45 3 * * 0"))

    def test_lists_and_steps_expand(self):
        fires = b.parse_schedule("0 1,13 * * *")
        self.assertEqual(sorted(fires), sorted([(d, 16 * 60) for d in range(7)]
                                               + [(d, 4 * 60) for d in range(7)]))
        self.assertEqual(len(b.parse_schedule("*/30 12 * * *")), 14)

    def test_dom_month_unsupported_fails_closed(self):
        with self.assertRaises(b.ManifestError):
            b.parse_schedule("0 4 1 * *")

    def test_dow_names_fail_closed(self):
        with self.assertRaises(b.ManifestError):
            b.parse_schedule("0 4 * * SUN")


class EvaluateTest(unittest.TestCase):
    """合成した台帳 (registry) を差し込んで両方向を固定する。"""

    HEAVY = {"bytes": 512 * MIB}
    LIGHT = {"bytes": 32 * MIB}

    def test_cluster_same_utc_day_flags_both_names(self):
        consumers = [
            consumer("a-retention", [(6, 1125)]),
            consumer("b-retention", [(6, 1140)]),
        ]
        registry = {"a-retention": self.HEAVY, "b-retention": self.HEAVY}
        problems = b.evaluate(consumers, None, registry)
        joined = "\n".join(problems)
        self.assertIn("a-retention", joined)
        self.assertIn("b-retention", joined)

    def test_heavy_consumers_hours_apart_pass(self):
        consumers = [
            consumer("a-retention", [(6, 8 * 60)]),
            consumer("b-retention", [(6, 19 * 60)]),
        ]
        registry = {"a-retention": self.HEAVY, "b-retention": self.HEAVY}
        self.assertEqual(b.evaluate(consumers, None, registry), [])

    def test_same_time_different_days_is_not_a_cluster(self):
        # P-0216 の分散先: 1 曜日 1 本なら同時刻でも密集しない
        consumers = [consumer(f"r{i}-retention", [(i + 1, 19 * 60)]) for i in range(5)]
        registry = {f"r{i}-retention": self.HEAVY for i in range(5)}
        self.assertEqual(b.evaluate(consumers, None, registry), [])

    def test_light_backups_never_trigger_cluster(self):
        # 日次 backup 5 本は実際の帯 (17:45–18:55Z) にいても重くないので無視される
        minutes = [1065, 1090, 1110, 1120, 1135]
        consumers = [consumer(f"x-backup-{m}", daily(m)) for m in minutes]
        registry = {f"x-backup-{m}": self.LIGHT for m in minutes}
        self.assertEqual(b.evaluate(consumers, None, registry), [])

    def test_boundary_start_is_flagged(self):
        consumers = [consumer("edge-retention", [(2, 23 * 60 + 40)])]
        problems = b.evaluate(consumers, None, {"edge-retention": self.HEAVY})
        joined = "\n".join(problems)
        self.assertIn("edge-retention", joined)
        self.assertIn("リセット境界", joined)

    def test_boundary_distance_over_margin_passes(self):
        consumers = [consumer("far-retention", [(2, 20 * 60)])]  # 00:00Z から 4h
        self.assertEqual(
            b.evaluate(consumers, None, {"far-retention": self.HEAVY}), [])

    def test_unregistered_consumer_named(self):
        consumers = [consumer("mystery-b2-job", daily(10 * 60))]
        joined = "\n".join(b.evaluate(consumers))
        self.assertIn("mystery-b2-job", joined)
        self.assertIn("未登録", joined)

    def test_stale_registry_entry_named(self):
        consumers = [consumer(name, daily(10 * 60)) for name in b.REGISTRY]
        # 台帳にだけあって manifest に無い名前が stale。軽い推定値で密集ノイズも消す
        registry = {name: self.LIGHT for name in b.REGISTRY}
        registry["phantom-retention"] = self.HEAVY
        joined = "\n".join(b.evaluate(consumers, None, registry))
        self.assertIn("phantom-retention", joined)
        self.assertIn("消えている", joined)

    def test_cap_exceeded_flagged_and_under_cap_passes(self):
        heavy = [consumer("a-retention", [(6, 1125)])]
        registry = {"a-retention": self.HEAVY}
        # cap 500 MiB × 0.8 = 400 MiB 予算に対し retention 512 MiB
        self.assertGreaterEqual(
            len([p for p in b.evaluate(heavy, 500 * MIB, registry)
                 if p.startswith("(a)")]), 1)
        small = [consumer("tiny-backup", daily(10 * 60))]
        tiny_registry = {"tiny-backup": {"bytes": MIB}}
        self.assertEqual(
            [p for p in b.evaluate(small, 100 * MIB, tiny_registry)
             if p.startswith("(a)")], [])

    def test_cap_unset_skips_total_check_but_keeps_rest(self):
        consumers = [consumer("a-retention", [(6, 1125)]),
                     consumer("b-retention", [(6, 1140)])]
        registry = {"a-retention": self.HEAVY, "b-retention": self.HEAVY}
        problems_no_cap = b.evaluate(consumers, None, registry)
        self.assertFalse(any(p.startswith("(a)") for p in problems_no_cap))
        self.assertTrue(any(p.startswith("(b)") for p in problems_no_cap))

    def test_evaluate_is_pure(self):
        """表示は main 側の担当。evaluate が印字するとテスト出力も汚れる。"""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            b.evaluate([consumer("a-retention", [(6, 1125)])],
                       None, {"a-retention": self.HEAVY})
        self.assertEqual(buf.getvalue(), "")


class RealRepoTest(unittest.TestCase):
    """実リポジトリに対する固定。schedule 分散後の現状が合格であることを守る。"""

    def setUp(self):
        self.consumers = b.collect_consumers(APPS_DIR)

    def test_ledger_matches_registry_exactly(self):
        names = {c["name"] for c in self.consumers}
        self.assertEqual(names, set(b.REGISTRY))

    def test_check_is_green_on_current_repo(self):
        self.assertEqual(b.evaluate(self.consumers, None), [])

    def test_retentions_are_spread_across_weekdays(self):
        heavy_days = []
        for c in self.consumers:
            est = b.REGISTRY.get(c["name"]) or {}
            if est.get("bytes", 0) >= b.HEAVY_THRESHOLD_BYTES:
                days = {d for d, _ in c["fires"]}
                self.assertEqual(len(days), 1, f"{c['name']} が複数曜日に発火する")
                heavy_days.extend(days)
        # 月〜金曜 04:00 JST (= 前日日〜木曜 19:00 UTC) に 1 本ずつ。5 曜日すべて別々
        self.assertEqual(sorted(heavy_days), [0, 1, 2, 3, 4])

    def test_main_rc0(self):
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--check"],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class MainCliTest(unittest.TestCase):
    def test_tiny_cap_forces_violation_rc1(self):
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--check", "--cap-bytes", str(200 * MIB)],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("(a)", proc.stdout)

    def test_bad_env_returns_2(self):
        env = dict(**{"B2_DAILY_CAP_BYTES": "not-a-number"})
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH)], capture_output=True,
            text=True, cwd=REPO, env=env,
        )
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
