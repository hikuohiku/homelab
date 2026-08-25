"""root_disk_usage.py の純関数と fixture を固定する (P-9062)。

P-9062 の受入検証 `python3 ops/tools/root_disk_usage.py --check` と同じロジックを
unittest でも固定する (node_saturation.py の --check と ops/tests の二重固定と同じ
流儀)。fixture は kubelet stats/summary の実スキーマに即したものを同梱する。
"""

import unittest
import urllib.error

from ops.tools import root_disk_usage as ru

GIB = 1024 * 1024 * 1024

SUMMARY = {
    "node": {
        "nodeName": "node01",
        "fs": {
            "availableBytes": 179000000000,
            "capacityBytes": 270000000000,
            "usedBytes": 74000000000,
        },
        "runtime": {
            "imageFs": {
                "availableBytes": 100000000000,
                "capacityBytes": 270000000000,
                "usedBytes": 45000000000,
            }
        },
        "pods": [
            {
                "podRef": {"name": "p1"},
                "volume": [{"name": "data", "fs": {"usedBytes": 1000000000}}],
            },
            {
                "podRef": {"name": "p2"},
                "volume": [{"name": "home", "fs": {"usedBytes": 250000000}}],
            },
        ],
    }
}


class SampleFromSummaryTest(unittest.TestCase):
    def test_total_and_breakdown_are_parsed(self):
        s = ru.sample_from_summary(SUMMARY)
        self.assertEqual(s["source"], "kubelet_summary")
        self.assertEqual(s["used_bytes"], 74000000000)
        self.assertEqual(s["capacity_bytes"], 270000000000)
        self.assertEqual(s["free_bytes"], 179000000000)
        self.assertEqual(s["images_bytes"], 45000000000)
        self.assertEqual(s["local_path_pvc_bytes"], 1250000000)

    def test_unreadable_breakdown_is_none(self):
        # 非特権 pod から hostPath 無しでは読めない内訳 (2026-08-25 実測)
        s = ru.sample_from_summary(SUMMARY)
        self.assertIsNone(s["k3s_bytes"])
        self.assertIsNone(s["containerd_bytes"])
        self.assertIsNone(s["logs_bytes"])

    def test_broken_summary_is_none(self):
        for bad in (None, {}, {"node": {}}, {"node": {"fs": {}}}):
            self.assertIsNone(ru.sample_from_summary(bad))

    def test_missing_available_bytes_is_derived(self):
        s = ru.sample_from_summary({"node": {"fs": {"usedBytes": 100, "capacityBytes": 200}}})
        self.assertEqual(s["free_bytes"], 100)

    def test_bool_and_string_bytes_are_rejected(self):
        # bool は int の派生なので弾く (download_budget.coerce_bytes と同じ判断)
        self.assertIsNone(ru._num(True))
        self.assertIsNone(ru._num("abc"))


class SampleFromStatvfsTest(unittest.TestCase):
    def test_total_only_with_none_breakdown(self):
        s = ru.sample_from_statvfs(1000, 300, 700)
        self.assertEqual(s["source"], "statvfs")
        self.assertEqual(s["used_bytes"], 300)
        self.assertEqual(s["free_bytes"], 700)
        self.assertIsNone(s["images_bytes"])
        self.assertIsNone(s["local_path_pvc_bytes"])


class AppendSampleTest(unittest.TestCase):
    def test_append_and_replace_same_timestamp(self):
        samples = ru.append_sample([], 100, "2026-08-25T00:00:00Z")
        self.assertEqual(samples, [{"ts": "2026-08-25T00:00:00Z", "used_bytes": 100}])
        samples = ru.append_sample(samples, 200, "2026-08-25T00:30:00Z")
        samples = ru.append_sample(samples, 210, "2026-08-25T00:30:00Z")
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[-1]["used_bytes"], 210)

    def test_trim_to_max_samples(self):
        many = []
        for i in range(ru.MAX_SAMPLES + 5):
            many = ru.append_sample(
                many, i, "2026-08-23T00:{:02d}:{:02d}Z".format(i // 60, i % 60)
            )
        self.assertEqual(len(many), ru.MAX_SAMPLES)


class ForecastTest(unittest.TestCase):
    def test_one_gib_per_day_fixture(self):
        hist = [
            {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100000000000},
            {"ts": "2026-08-24T00:00:00Z", "used_bytes": 100000000000 + GIB},
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": 100000000000 + 2 * GIB},
        ]
        rate = ru.daily_increase_bytes(hist)
        self.assertIsNotNone(rate)
        self.assertAlmostEqual(rate, GIB, delta=1e3)
        fc = ru.forecast(hist, 167000000000)
        self.assertEqual(fc["daily_increase_bytes"], GIB)
        self.assertIsNotNone(fc["fill_days"])
        self.assertGreater(fc["fill_days"], 100)
        self.assertIsNone(fc["note"])

    def test_forecast_requires_window(self):
        short = [
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": 100},
            {"ts": "2026-08-25T00:30:00Z", "used_bytes": 200},
        ]
        fc = ru.forecast(short, 100000)
        self.assertIsNone(fc["fill_days"])
        self.assertIsNone(fc["daily_increase_bytes"])
        self.assertIn("観測窓", fc["note"])

    def test_forecast_needs_two_samples(self):
        fc = ru.forecast([], 1000)
        self.assertIsNone(fc["fill_days"])
        self.assertIsNotNone(fc["note"])

    def test_forecast_shrinking_is_unforecastable(self):
        shrinking = [
            {"ts": "2026-08-23T00:00:00Z", "used_bytes": 200},
            {"ts": "2026-08-24T00:00:00Z", "used_bytes": 180},
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": 160},
        ]
        fc = ru.forecast(shrinking, 1000)
        self.assertIsNone(fc["fill_days"])
        self.assertIn("0 以下", fc["note"])

    def test_corrupt_timestamp_sample_is_dropped(self):
        hist = [
            {"ts": "garbage", "used_bytes": 1},
            {"ts": "2026-08-24T00:00:00Z", "used_bytes": 100000000000},
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": 100000000000 + GIB},
        ]
        self.assertIsNotNone(ru.daily_increase_bytes(hist))


class BuildReportTest(unittest.TestCase):
    def test_section_and_history_from_injected_summary(self):
        section, samples = ru.build_report(
            [], "2026-08-25T00:00:00Z", node_name="node01", summary_doc=SUMMARY
        )
        self.assertEqual(section["node"], "node01")
        self.assertEqual(section["source"], "kubelet_summary")
        self.assertIn("fill_days", section)
        self.assertEqual(section["samples"], 1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["used_bytes"], 74000000000)

    def test_statvfs_fallback_section(self):
        section, samples = ru.build_report(
            [], "2026-08-25T00:00:00Z", node_name="node01", summary_doc=None
        )
        self.assertEqual(section["source"], "statvfs")
        self.assertIsNone(section["breakdown"]["images_bytes"])


class FetchKubeletSummaryTest(unittest.TestCase):
    def setUp(self):
        self._orig = ru.k8s_get
        self.addCleanup(setattr, ru, "k8s_get", self._orig)

    def test_network_error_falls_back_to_none(self):
        # 403 (RBAC 不備) や接続不能は None → statvfs へ倒れる
        for exc in (OSError("no route"), urllib.error.HTTPError("u", 403, "Forbidden", {}, None)):
            ru.k8s_get = lambda path: (_ for _ in ()).throw(exc)
            self.assertIsNone(ru.fetch_kubelet_summary("node01"))

    def test_non_json_response_falls_back_to_none(self):
        # 200 だが応答が JSON でない (apiserver 前段のプロキシが HTML を返す等)。
        # json.load の ValueError は OSError/HTTPError と違い漏れて root_disk 節を
        # {"error": ...} にしていた (取りこぼすと fill_days の契約が壊れる — P-9062)
        ru.k8s_get = lambda path: (_ for _ in ()).throw(ValueError("Expecting value"))
        self.assertIsNone(ru.fetch_kubelet_summary("node01"))

    def test_build_report_propagates_summary_fetch_failure_to_statvfs(self):
        # 実測経路の結合: summary 取得が JSON パース失敗でも root_disk 節は必ず
        # でき、fill_days キーを持つ (受入検証の契約)
        ru.k8s_get = lambda path: (_ for _ in ()).throw(ValueError("Expecting value"))
        section, _ = ru.build_report(
            [], "2026-08-25T00:00:00Z", node_name="node01", summary_doc=None
        )
        self.assertEqual(section["source"], "statvfs")
        self.assertIn("fill_days", section)


if __name__ == "__main__":
    unittest.main()