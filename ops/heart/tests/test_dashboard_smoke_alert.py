"""dashboard_smoke_alert の分岐テスト (P-0193)。

latest.json (raw doc) から fail/stale だけを拾うことを固定する。heart は
120s ビートで回るため、繰り返し抑制は budget_alert_due() (status/date の一般判定。
test_budget_alert.py で両方向固定済み) を流用する — ここでは抽出の契約のみを見る。
"""

import unittest

from ops.heart import facts


def report_doc(status, reason="r", failed_checks=None):
    """reporter (_dashboard_smoke_summary) が作る dashboard_smoke キーの形。"""
    doc = {
        "status": status,
        "reason": reason,
        "ok": status == "ok",
        "generated_at": "2026-08-23T09:00:00Z",
        "age_seconds": 600,
        "checks_total": 12,
    }
    if failed_checks is not None:
        doc["failed_checks"] = failed_checks
    return {"dashboard_smoke": doc}


class DashboardSmokeAlertTest(unittest.TestCase):
    def test_fail_and_stale_are_extracted_with_check_names(self):
        checks = [
            {"name": "no-lie-coexistence", "detail": "正常チップと異常表示が共存"},
            {"name": "heartbeat-fresh", "detail": "LAST HEART が古い"},
        ]
        a = facts.dashboard_smoke_alert(
            report_doc("fail", reason="描画断言が不合格: no-lie-coexistence",
                       failed_checks=checks)
        )
        self.assertEqual(
            a,
            {
                "status": "fail",
                "reason": "描画断言が不合格: no-lie-coexistence",
                "failed_checks": ["no-lie-coexistence", "heartbeat-fresh"],
            },
        )
        a = facts.dashboard_smoke_alert(report_doc("stale", reason="装置が沈黙"))
        self.assertEqual(a["status"], "stale")
        self.assertEqual(a["failed_checks"], [])

    def test_quiet_statuses_return_none(self):
        for status in ("ok", "no_data"):
            self.assertIsNone(facts.dashboard_smoke_alert(report_doc(status)))

    def test_broken_or_missing_shapes_return_none(self):
        for doc in (
            None,
            {},
            {"dashboard_smoke": None},
            {"dashboard_smoke": {}},
            {"dashboard_smoke": {"status": 1}},
            ["not", "a", "dict"],
        ):
            self.assertIsNone(facts.dashboard_smoke_alert(doc))

    def test_malformed_failed_checks_are_tolerated(self):
        """内訳が壊れていても status/reason の警報は倒さない (過剰通知側に倒す)。"""
        a = facts.dashboard_smoke_alert(
            report_doc("fail", failed_checks=["garbage", None, {"detail": "名前無し"}])
        )
        self.assertEqual(a["failed_checks"], [])
        # failed_checks キー自体が無くても空リストで形は保たれる
        a = facts.dashboard_smoke_alert(report_doc("stale"))
        self.assertEqual(a["failed_checks"], [])

    def test_non_string_or_missing_reason_becomes_none_not_crash(self):
        doc = report_doc("fail")
        del doc["dashboard_smoke"]["reason"]
        self.assertIsNone(facts.dashboard_smoke_alert(doc)["reason"])
        doc = report_doc("stale", reason=5)
        self.assertIsNone(facts.dashboard_smoke_alert(doc)["reason"])

    def test_tool_error_record_is_also_an_alert(self):
        """rc=2 の代役レコード (装置故障) も fail なので乗る。区別は reason が担う。"""
        doc = report_doc(
            "fail",
            reason="スモーク本体が異常終了 (rc=2) — 装置が回らなかった: chromium を起動できない",
        )
        a = facts.dashboard_smoke_alert(doc)
        self.assertEqual(a["status"], "fail")
        self.assertIn("装置が回らなかった", a["reason"])


if __name__ == "__main__":
    unittest.main()
