"""checksum_alert の分岐テスト (P-0361)。

latest.json (raw doc) から fail / error だけを拾うことを固定する。heart は
120s ビートで回るため、繰り返し抑制は budget_alert_due() (status/date の一般判定。
test_budget_alert.py で両方向固定済み) を流用する — ここでは抽出の契約のみを見る。
"""

import unittest

from ops.heart import facts


def report_doc(status, reason="r"):
    """reporter (collect_checksum) が作る latest.json の checksum キーの形。"""
    return {
        "checksum": {
            "status": status,
            "reason": reason,
            "ok": status == "ok",
            "generated_at": "2026-08-24T12:00:00Z",
            "namespace": "immich",
        }
    }


class ChecksumAlertTest(unittest.TestCase):
    def test_fail_is_extracted_with_reason(self):
        a = facts.checksum_alert(
            report_doc("fail", reason="checksum_mismatch が 3 件 (閾値 1 以上) — 原本の腐りを検出")
        )
        self.assertEqual(
            a,
            {
                "status": "fail",
                "reason": "checksum_mismatch が 3 件 (閾値 1 以上) — 原本の腐りを検出",
            },
        )

    def test_error_stub_record_is_extracted_too(self):
        """代役レコード (産出側自身の失敗) も error として鳴らす。

        週次でしか回らない予防装置が「測れなかった」まま沈黙すると腐りの検出自体が
        失われるため、dashboard_smoke が tool_error を区別せず乗せるのと同じ判断。
        """
        a = facts.checksum_alert(
            report_doc("error", reason="RuntimeError: queue の取得に失敗: 500 {...}")
        )
        self.assertEqual(a["status"], "error")
        self.assertIn("queue の取得に失敗", a["reason"])

    def test_quiet_statuses_return_none(self):
        for status in ("ok", "unconfigured", "no_data"):
            self.assertIsNone(facts.checksum_alert(report_doc(status)))

    def test_broken_or_missing_shapes_return_none(self):
        for doc in (
            None,
            {},
            {"checksum": None},
            {"checksum": {}},
            {"checksum": {"status": 1}},
            ["not", "a", "dict"],
        ):
            self.assertIsNone(facts.checksum_alert(doc))

    def test_non_string_or_missing_reason_becomes_none_not_crash(self):
        doc = report_doc("fail")
        del doc["checksum"]["reason"]
        self.assertIsNone(facts.checksum_alert(doc)["reason"])
        doc = report_doc("error", reason=5)
        self.assertIsNone(facts.checksum_alert(doc)["reason"])


if __name__ == "__main__":
    unittest.main()