"""node_saturation_alert の分岐テスト (P-9037)。

facts.node_saturation_alert() が latest.json の node_saturation キーから
「鳴らすべき状態」だけを抽出することを固定する。鳴らすかどうかの繰り返し抑制は
budget_alert_due() (status/date の一般判定。test_budget_alert.py で両方向固定済み)
を流用するので、ここでは抽出の契約のみを見る。
"""

import unittest

from ops.heart import facts


def reporter_node_saturation(status, reasons=None, **values):
    """reporter (collect_node_saturation) が書く node_saturation キーの形。"""
    sat = {
        "status": status,
        "reasons": reasons,
        "requests_m": values.get("requests_m", 3761),
        "allocatable_m": values.get("allocatable_m", 4000),
        "requests_ratio": values.get("requests_ratio", 0.9403),
        "load_1m": values.get("load_1m", 25.0),
        "vcpus": values.get("vcpus", 4),
        "node": "node01",
        "load_source": "proc_loadavg",
        "checked_at": "2026-08-24T23:00:00Z",
    }
    return {"node_saturation": sat}


class NodeSaturationAlertTest(unittest.TestCase):
    def test_20260824_fixture_warn_is_extracted_with_numbers(self):
        a = facts.node_saturation_alert(
            reporter_node_saturation("warn", ["requests_ratio", "load"])
        )
        self.assertEqual(a["status"], "warn")
        self.assertIn("90% 超 (3761m/4000m)", a["reason"])
        self.assertIn("25.0 > 4", a["reason"])
        self.assertEqual(a["requests_m"], 3761)
        self.assertEqual(a["load_1m"], 25.0)

    def test_ok_is_silent(self):
        for status in ("ok", None):
            self.assertIsNone(
                facts.node_saturation_alert(reporter_node_saturation(status, []))
            )

    def test_missing_key_is_silent(self):
        self.assertIsNone(facts.node_saturation_alert({}))
        self.assertIsNone(facts.node_saturation_alert({"node_saturation": None}))
        self.assertIsNone(facts.node_saturation_alert({"node_saturation": {}}))

    def test_observation_failure_error_entry_is_silent(self):
        # reporter の観測失敗 (collect が error を返す) は鳴らさない
        self.assertIsNone(
            facts.node_saturation_alert({"node_saturation": {"error": "FileNotFoundError: ..."}})
        )

    def test_broken_reasons_does_not_swallow_the_alert(self):
        # reasons が壊れていても status=warn は成立させる (文面だけ落とす)
        a = facts.node_saturation_alert(reporter_node_saturation("warn", None))
        self.assertEqual(a["status"], "warn")
        self.assertEqual(a["reason"], "CPU 飽和前兆")


if __name__ == "__main__":
    unittest.main()