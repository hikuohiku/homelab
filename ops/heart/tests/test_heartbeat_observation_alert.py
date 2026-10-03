"""heartbeat_observation_alert の分岐テスト。

latest.json (raw doc) の autopilot.heartbeat.error だけを拾うことを固定する。
繰り返し抑制は budget_alert_due() (test_budget_alert.py で両方向固定済み) を
流用するので、ここでは抽出の契約のみを見る。
"""

import unittest

from ops.heart import facts


def health_doc(heartbeat):
    return {"autopilot": {"deployment": {"replicas": 1}, "heartbeat": heartbeat}}


class HeartbeatObservationAlertTest(unittest.TestCase):
    def test_error_is_extracted(self):
        a = facts.heartbeat_observation_alert(
            health_doc({"error": "HTTPError: HTTP Error 400: Bad Request"})
        )
        self.assertEqual(
            a,
            {"status": "error", "reason": "HTTPError: HTTP Error 400: Bad Request"},
        )

    def test_pod_not_found_is_also_an_error(self):
        # 観測対象の Pod が居ないのも「計器が見えていない」— 同じ経路で鳴らす
        a = facts.heartbeat_observation_alert(
            health_doc({"error": "app=autopilot-heart の pod が見つからない"})
        )
        self.assertEqual(a["status"], "error")

    def test_observed_heartbeat_is_quiet(self):
        # ビートが遅い/欠けていること自体はコアの Lease 監視の担当。ここでは鳴らさない
        for hb in (
            {"last_start": {"iteration": 3483}, "last_end": None},
            {"last_start": None, "last_end": None},
        ):
            self.assertIsNone(facts.heartbeat_observation_alert(health_doc(hb)))

    def test_broken_or_missing_shapes_return_none(self):
        for doc in (
            None,
            {},
            {"autopilot": None},
            {"autopilot": {}},
            {"autopilot": {"heartbeat": None}},
            {"autopilot": {"heartbeat": {"error": ""}}},
            {"autopilot": {"heartbeat": {"error": 400}}},
            ["not", "a", "dict"],
        ):
            self.assertIsNone(facts.heartbeat_observation_alert(doc))

    def test_long_error_is_truncated(self):
        a = facts.heartbeat_observation_alert(health_doc({"error": "x" * 500}))
        self.assertEqual(len(a["reason"]), 200)


if __name__ == "__main__":
    unittest.main()
