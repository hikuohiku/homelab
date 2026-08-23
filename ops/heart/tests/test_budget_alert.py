"""budget_alert / budget_alert_due の分岐テスト (P-0128)。

latest.json (raw doc) から warn/exceed だけを拾うこと、そして同じ status の
同一日内の再通知を落とすことを固定する。heart は 120s ビートで回るため
抑制が壊れると briefing-queue.jsonl と Discord 予算を 1 日で使い潰す。
"""

import unittest

from ops.heart import facts


def report_doc(status, reason="r", daily_avg=1234, monthly=45678):
    return {
        "download_budget": {
            "total": {"daily_avg_bytes": daily_avg},
            "monthly_estimate_bytes": monthly,
            "budget": {"status": status, "reason": reason},
        }
    }


class BudgetAlertTest(unittest.TestCase):
    def test_warn_and_exceed_are_extracted(self):
        for status in ("warn", "exceed"):
            a = facts.budget_alert(
                report_doc(status, reason="理由", daily_avg=12.5, monthly=45678)
            )
            self.assertEqual(
                a,
                {
                    "status": status,
                    "reason": "理由",
                    # 日次平均は割り算の結果なので float が普通
                    "daily_avg_bytes": 12.5,
                    "monthly_estimate_bytes": 45678,
                },
            )

    def test_non_numeric_estimates_are_dropped(self):
        doc = report_doc("exceed", daily_avg="1GiB", monthly=True)
        self.assertEqual(facts.budget_alert(doc)["daily_avg_bytes"], None)
        self.assertEqual(facts.budget_alert(doc)["monthly_estimate_bytes"], None)

    def test_quiet_statuses_return_none(self):
        for status in ("ok", "unconfigured", "no_data"):
            self.assertIsNone(facts.budget_alert(report_doc(status)))

    def test_broken_or_missing_shapes_return_none(self):
        for doc in (
            None,
            {},
            {"download_budget": None},
            {"download_budget": {}},
            {"download_budget": {"budget": "exceed"}},
            {"download_budget": {"budget": {"status": 1}}},
            ["not", "a", "dict"],
        ):
            self.assertIsNone(facts.budget_alert(doc))


class BudgetAlertDueTest(unittest.TestCase):
    ALERT = {"status": "warn", "reason": "r"}

    def test_first_alert_fires(self):
        self.assertTrue(facts.budget_alert_due(self.ALERT, None, "2026-08-23"))

    def test_same_status_same_day_is_suppressed(self):
        prev = {"status": "warn", "date": "2026-08-23"}
        self.assertFalse(facts.budget_alert_due(self.ALERT, prev, "2026-08-23"))

    def test_next_day_fires_again(self):
        prev = {"status": "warn", "date": "2026-08-22"}
        self.assertTrue(facts.budget_alert_due(self.ALERT, prev, "2026-08-23"))

    def test_status_change_fires_even_same_day(self):
        """warn → exceed への悪化は同日でも再通知する。"""
        prev = {"status": "warn", "date": "2026-08-23"}
        exceed = {"status": "exceed", "reason": "r"}
        self.assertTrue(facts.budget_alert_due(exceed, prev, "2026-08-23"))

    def test_none_alert_never_fires(self):
        for prev in (None, {"status": "warn", "date": "2026-08-23"}, "garbage"):
            self.assertIs(facts.budget_alert_due(None, prev, "2026-08-23"), False)

    def test_garbage_prev_fires(self):
        """前回記録が壊れている場合は鳴る側に倒す (沈黙より過剰通知)。"""
        self.assertTrue(facts.budget_alert_due(self.ALERT, "garbage", "2026-08-23"))


if __name__ == "__main__":
    unittest.main()
