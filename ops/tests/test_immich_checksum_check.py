"""ops/tools/immich_checksum_check.py の判定ロジックを固定する (P-0361)。

リポジトリルートから `python3 -m unittest ops.tests.test_immich_checksum_check`。
純関数のみを対象とし、ネットワークにもクラスタにも出ない。API レスポンス形が
実機で確定したら、このテストの fixture を実測値で更新する (PROJECT.md の手順)。
"""

import io
import json
import unittest
from contextlib import redirect_stdout

from ops.tools import immich_checksum_check as icc

OK_SUMMARY = {"checksum_mismatch": 0, "missing_file": 1, "untracked_file": 2}
MISMATCH_SUMMARY = {"checksum_mismatch": 3, "missing_file": 0, "untracked_file": 0}


class TestParseSummary(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(icc.parse_summary(OK_SUMMARY), OK_SUMMARY)

    def test_non_dict(self):
        with self.assertRaises(ValueError):
            icc.parse_summary(["checksum_mismatch", 0])

    def test_missing_key(self):
        with self.assertRaises(ValueError):
            icc.parse_summary({"checksum_mismatch": 0, "missing_file": 0})

    def test_negative_count(self):
        with self.assertRaises(ValueError):
            icc.parse_summary(
                {"checksum_mismatch": -1, "missing_file": 0, "untracked_file": 0}
            )

    def test_bool_count(self):
        with self.assertRaises(ValueError):
            icc.parse_summary(
                {"checksum_mismatch": True, "missing_file": 0, "untracked_file": 0}
            )

    def test_string_count(self):
        with self.assertRaises(ValueError):
            icc.parse_summary(
                {"checksum_mismatch": "0", "missing_file": 0, "untracked_file": 0}
            )


class TestNormalizeThreshold(unittest.TestCase):
    def test_none_passes(self):
        self.assertIsNone(icc.normalize_threshold(None))

    def test_positive_int(self):
        self.assertEqual(icc.normalize_threshold(1), 1)

    def test_zero_int(self):
        self.assertEqual(icc.normalize_threshold(0), 0)

    def test_negative_rejected(self):
        self.assertIsNone(icc.normalize_threshold(-1))

    def test_bool_rejected(self):
        self.assertIsNone(icc.normalize_threshold(True))

    def test_string_rejected(self):
        self.assertIsNone(icc.normalize_threshold("1"))


class TestJudgeMismatch(unittest.TestCase):
    def test_ok_below_threshold(self):
        result = icc.judge_mismatch(0, 1)
        self.assertEqual(result["status"], "ok")

    def test_fail_at_threshold(self):
        result = icc.judge_mismatch(1, 1)
        self.assertEqual(result["status"], "fail")

    def test_fail_above_threshold(self):
        result = icc.judge_mismatch(3, 1)
        self.assertEqual(result["status"], "fail")

    def test_fail_when_threshold_zero(self):
        result = icc.judge_mismatch(1, 0)
        self.assertEqual(result["status"], "fail")

    def test_unconfigured(self):
        result = icc.judge_mismatch(0, None)
        self.assertEqual(result["status"], "unconfigured")


class TestBuildReport(unittest.TestCase):
    def test_report_ok(self):
        report = icc.build_report(
            OK_SUMMARY, "2026-08-24T00:00:00Z", threshold=1
        )
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["ok"])
        self.assertEqual(report["checksum_mismatch"], 0)
        self.assertEqual(report["missing_file"], 1)
        self.assertEqual(report["untracked_file"], 2)
        self.assertEqual(report["generated_at"], "2026-08-24T00:00:00Z")
        self.assertNotIn("job", report)

    def test_report_fail(self):
        report = icc.build_report(MISMATCH_SUMMARY, "2026-08-24T00:00:00Z", threshold=1)
        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["ok"])
        self.assertEqual(report["checksum_mismatch"], 3)

    def test_report_job_meta_included(self):
        job = {"name": icc.JOB_NAME, "triggered_at": "2026-08-24T00:00:00Z"}
        report = icc.build_report(OK_SUMMARY, "2026-08-24T01:00:00Z", threshold=1, job=job)
        self.assertEqual(report["job"], job)


class TestSelftest(unittest.TestCase):
    def test_selftest_passes(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = icc.run_selftest()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("selftest: 全", out.getvalue())

    def test_main_selftest_flag(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = icc.main(["--selftest"])
        self.assertEqual(rc, 0, out.getvalue())

    def test_main_no_args_prints_help(self):
        rc = icc.main([])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()