"""apps/ops-health-reporter/download_budget.py (P-0128, B2 download cap の帳簿) の純関数を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_download_budget`。
download_budget.py は report.py と同じく ConfigMap から直接起動される単一ファイルの
ためパッケージではなく、テストからは importlib で実ファイルをロードする
(test_openclaw_bridge.py と同じ形)。report.py 自身は import 時に ServiceAccount token
を読むため cluster 外からロードできず、純関数をここに分離した経緯もある。

固定する契約:
- 直近 N 日 (UTC) の集計: 窓の外と未来日は捨て、壊れた記録は例外ではなく skipped
- 月次見積もり: 窓合計の比例外挿。データゼロでは None を返し 0 除算しない
- 閾値判定: cap 実値は repo の docs に無いため未設定 (None) なら決め打ちせず
  unconfigured を正直に返す。境界は鳴る側 (warn/exceed) に倒す
"""

import datetime
import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "download_budget.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("download_budget_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


db = _load_module()

TODAY = datetime.date(2026, 8, 23)


def run(date, job, bytes_):
    return {"date": date, "job": job, "bytes": bytes_}


class SumWindowTest(unittest.TestCase):
    def test_empty_runs(self):
        s = db.sum_window([], TODAY)
        self.assertEqual(s["window_total_bytes"], 0)
        self.assertEqual(s["days_covered"], 0)
        self.assertEqual(s["daily_bytes"], {})
        self.assertEqual(s["by_job"], {})
        self.assertEqual(s["skipped_records"], 0)

    def test_sums_per_date_and_job(self):
        runs = [
            run("2026-08-23", "immich-restic-backup", 100),
            run("2026-08-23", "coder-restic-backup", 50),
            run("2026-08-22", "immich-restic-backup", 10),
        ]
        s = db.sum_window(runs, TODAY)
        self.assertEqual(s["daily_bytes"], {"2026-08-22": 10, "2026-08-23": 150})
        self.assertEqual(s["by_job"],
                         {"coder-restic-backup": 50, "immich-restic-backup": 110})
        self.assertEqual(s["window_total_bytes"], 160)
        self.assertEqual(s["days_covered"], 2)

    def test_window_is_last_n_days_including_today(self):
        # 窓 = [today - (N-1), today]。7 日窓なら 08-16 は外、08-17 は入る (境界)
        runs = [
            run("2026-08-16", "old", 1),
            run("2026-08-17", "boundary-in", 10),
            run("2026-08-18", "middle", 100),
            run("2026-08-23", "today", 1000),
        ]
        s = db.sum_window(runs, TODAY, window_days=7)
        self.assertEqual(s["daily_bytes"],
                         {"2026-08-17": 10, "2026-08-18": 100, "2026-08-23": 1000})
        self.assertNotIn("old", s["by_job"])

    def test_future_date_is_skipped_not_counted(self):
        # clock skew 記録を足し算に乗せない (heartbeat judge() の skew 扱いと同じ倒し方)
        s = db.sum_window([run("2026-08-24", "skewed", 999)], TODAY)
        self.assertEqual(s["window_total_bytes"], 0)
        self.assertEqual(s["skipped_records"], 1)

    def test_malformed_records_are_skipped_without_raising(self):
        runs = [
            None,
            "not-a-dict",
            {},
            run("not-a-date", "j", 1),
            run("2026-08-23", "j", "100"),   # 文字列は不可
            run("2026-08-23", "j", -1),      # 負値は不可
            run("2026-08-23", "j", True),    # bool は int の派生なので明示的に弾く
            run("2026-08-23", "j", None),    # 欠損
            run("2026-08-23", "j", 0),       # 0 は正当な記録
            run("2026-08-23", "", 5),        # job 名が空でも記録自体は数える (unknown 扱い)
        ]
        s = db.sum_window(runs, TODAY)
        self.assertEqual(s["window_total_bytes"], 5)
        self.assertEqual(s["skipped_records"], 8)
        self.assertEqual(s["days_covered"], 1)

    def test_missing_job_name_becomes_unknown(self):
        s = db.sum_window([{"date": "2026-08-23", "bytes": 7}], TODAY)
        self.assertEqual(s["by_job"], {"unknown": 7})

    def test_extra_fields_are_tolerated(self):
        # 産出側が後からフィールドを増やしても (例: kind, source) 集計は壊れない
        rec = {"date": "2026-08-23", "job": "j", "bytes": 3,
               "kind": "backup", "source": "restic-summary"}
        self.assertEqual(db.sum_window([rec], TODAY)["window_total_bytes"], 3)

    def test_today_as_string(self):
        s = db.sum_window([run("2026-08-23", "j", 4)], "2026-08-23")
        self.assertEqual(s["window_total_bytes"], 4)
        with self.assertRaises(ValueError):
            db.sum_window([], "garbage")


class MonthlyEstimateTest(unittest.TestCase):
    def test_projection_math(self):
        # 700 bytes / 7 日 covered → 30 日で 3000
        self.assertEqual(db.monthly_estimate(700, 7), 3000.0)

    def test_horizon_days_parameter(self):
        self.assertEqual(db.monthly_estimate(700, 7, horizon_days=14), 1400.0)

    def test_no_data_returns_none_instead_of_zero_division(self):
        self.assertIsNone(db.monthly_estimate(0, 0))
        self.assertIsNone(db.monthly_estimate(12345, None))


class JudgeTest(unittest.TestCase):
    def test_no_data(self):
        v = db.judge(None, daily_cap_bytes=1000)
        self.assertEqual(v["status"], "no_data")

    def test_unconfigured_when_cap_value_is_none(self):
        # cap の実値は B2 コンソールにしか無い。決め打ちで ok/exceed を出さないのが契約
        v = db.judge(500.0, daily_cap_bytes=None)
        self.assertEqual(v["status"], "unconfigured")
        self.assertIn("未設定", v["reason"])

    def test_default_cap_is_unconfigured(self):
        # モジュール既定値が「設定なし」であることを固定 (docs 外の実値に依存しない)
        self.assertIsNone(db.DEFAULT_DAILY_CAP_BYTES)
        self.assertEqual(db.judge(500.0)["status"], "unconfigured")

    def test_ok_below_warn_ratio(self):
        v = db.judge(700.0, daily_cap_bytes=1000)  # 0.8 境界の下
        self.assertEqual(v["status"], "ok")
        # 月次換算 (700×30 = 21000 B = 20.5 KiB) が文面に載る
        self.assertIn("20.5 KiB", v["reason"])

    def test_warn_at_exact_ratio_boundary(self):
        # 境界は鳴る側 (>=)。上限近傍の計器が沈黙するのは手遅れ型
        v = db.judge(800.0, daily_cap_bytes=1000)
        self.assertEqual(v["status"], "warn")

    def test_ok_just_under_warn_ratio(self):
        v = db.judge(799.9, daily_cap_bytes=1000)
        self.assertEqual(v["status"], "ok")

    def test_exceed_at_exact_cap(self):
        v = db.judge(1000.0, daily_cap_bytes=1000)
        self.assertEqual(v["status"], "exceed")
        self.assertIn("超過", v["reason"])

    def test_exceed_above_cap(self):
        v = db.judge(2500.0, daily_cap_bytes=1000)
        self.assertEqual(v["status"], "exceed")


class BuildReportTest(unittest.TestCase):
    def test_composes_namespaces_totals_and_budget(self):
        reports = [
            {"namespace": "immich",
             "runs": [run("2026-08-23", "immich-restic-backup", 300),
                      run("2026-08-22", "immich-restic-backup", 400)]},
            {"namespace": "coder",
             "runs": [run("2026-08-23", "coder-restic-backup", 100)]},
        ]
        r = db.build_report(reports, today=TODAY)
        self.assertEqual(r["window_days"], 7)
        self.assertEqual(r["total"]["window_total_bytes"], 800)
        self.assertEqual(r["total"]["days_covered"], 2)
        self.assertEqual(r["total"]["daily_avg_bytes"], 400.0)
        # by_job は namespace をまたぐ衝突を避けるため ns/job で連結する
        self.assertEqual(r["total"]["by_job"],
                         {"coder/coder-restic-backup": 100,
                          "immich/immich-restic-backup": 700})
        self.assertEqual(r["monthly_estimate_bytes"], 12000.0)
        self.assertEqual(r["budget"]["status"], "unconfigured")

    def test_error_entry_does_not_stop_other_namespaces(self):
        # collect_pvc_usage() と同じ思想: 1 namespace の失敗で全体を止めない
        reports = [
            {"namespace": "vaultwarden", "error": "HTTPError: 404"},
            {"namespace": "immich",
             "runs": [run("2026-08-23", "immich-restic-backup", 42)]},
        ]
        r = db.build_report(reports, today=TODAY)
        self.assertEqual(r["namespaces"]["vaultwarden"],
                         {"error": "HTTPError: 404"})
        self.assertEqual(r["total"]["window_total_bytes"], 42)

    def test_no_data_anywhere_reports_no_data(self):
        r = db.build_report([], today=TODAY)
        self.assertEqual(r["budget"]["status"], "no_data")
        self.assertIsNone(r["monthly_estimate_bytes"])
        self.assertEqual(r["total"]["days_covered"], 0)

    def test_budget_wired_to_configured_cap(self):
        reports = [{"namespace": "immich",
                    "runs": [run(f"2026-08-{d:02d}", "j", 900) for d in range(17, 24)]}]
        # 平均 900 /日。cap 1000 未満だが warn 境界 (0.8×1000) 以上 → warn
        r = db.build_report(reports, today=TODAY, daily_cap_bytes=1000)
        self.assertEqual(r["budget"]["status"], "warn")
        self.assertEqual(r["total"]["daily_avg_bytes"], 900.0)
