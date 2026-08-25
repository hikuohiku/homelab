"""node_saturation.py の純関数と 08-24 実測値 fixture を固定する (P-9037)。

P-9037 の受入検証 `python3 -m unittest ops.tests.test_node_saturation` の本体。
08-24 18:18 JST の実測値 (ops/rules.json `_max_concurrent_comment`: runner×2 +
curriculum + heart で requests 合計 3761m/4000m、ホスト load 25) で「この値なら
警告が出る」を単体テストに固定する。閾値は rules.json の逆算を根拠に P-9029 の
dod 踏襲: allocatable の 90% 超 または load > vCPU 数。
"""

import tempfile
import unittest

from ops.tools import node_saturation as ns


class ParseCpuMillicoresTest(unittest.TestCase):
    def test_milli_and_core_notations(self):
        self.assertEqual(ns.parse_cpu_millicores("250m"), 250)
        self.assertEqual(ns.parse_cpu_millicores("1"), 1000)
        self.assertEqual(ns.parse_cpu_millicores("1.5"), 1500)
        self.assertEqual(ns.parse_cpu_millicores(2), 2000)
        self.assertEqual(ns.parse_cpu_millicores(0.5), 500)

    def test_undecidable_is_none_not_exception(self):
        for value in (None, "", "abc", "m", True, False):
            self.assertIsNone(ns.parse_cpu_millicores(value))


class SumCpuRequestsTest(unittest.TestCase):
    def test_sums_container_requests_across_pods(self):
        pods = {
            "items": [
                {
                    "spec": {
                        "containers": [
                            {"resources": {"requests": {"cpu": "1"}}},
                            {"resources": {"requests": {"cpu": "250m"}}},
                            {"name": "no-request"},
                        ]
                    }
                },
                {
                    "spec": {
                        "containers": [
                            {"resources": {"requests": {"cpu": "500m"}}},
                            {"resources": {"requests": {"cpu": "1.5"}}},
                        ]
                    }
                },
            ]
        }
        # 1000 + 250 + 500 + 1500
        self.assertEqual(ns.sum_cpu_requests(pods), 3250)

    def test_requests_without_cpu_or_no_containers_are_zero(self):
        self.assertEqual(ns.sum_cpu_requests({"items": []}), 0)
        self.assertEqual(
            ns.sum_cpu_requests({"items": [{"spec": {"containers": [{}]}}]}), 0
        )

    def test_terminal_pods_are_not_counted(self):
        # レビュー指摘 (P-9037): スケジューラは終端 pod (Succeeded/Failed) の
        # requests を容量に数えない。k3s は terminated-pod-gc まで終端 pod を
        # 残し続けるため、数えると水増しになる。実測: Running のみ 3924m/4000m
        # に対し終端 pod 込みだと 43594m (ratio 10.90)。
        pods = {
            "items": [
                {
                    "status": {"phase": "Running"},
                    "spec": {"containers": [{"resources": {"requests": {"cpu": "3924m"}}}]},
                },
                {
                    "status": {"phase": "Succeeded"},
                    "spec": {"containers": [{"resources": {"requests": {"cpu": "20000m"}}}]},
                },
                {
                    "status": {"phase": "Failed"},
                    "spec": {"containers": [{"resources": {"requests": {"cpu": "19670m"}}}]},
                },
            ]
        }
        # 終端 39670m を除いて 3924m だけが残る (43594 に水増ししない)
        self.assertEqual(ns.sum_cpu_requests(pods), 3924)

    def test_pod_without_phase_is_still_counted(self):
        # status.phase を持たない pod (手作り fixture 等) は従来どおり数える
        pods = {
            "items": [
                {"spec": {"containers": [{"resources": {"requests": {"cpu": "500m"}}}]}}
            ]
        }
        self.assertEqual(ns.sum_cpu_requests(pods), 500)


class AllocatableTest(unittest.TestCase):
    def test_allocatable_and_vcpus(self):
        node = {"status": {"allocatable": {"cpu": "4"}}}
        self.assertEqual(ns.allocatable_cpu_millicores(node), 4000)
        self.assertEqual(ns.vcpus(node), 4)

    def test_vcpus_from_milli_allocatable(self):
        node = {"status": {"allocatable": {"cpu": "3900m"}}}
        self.assertEqual(ns.vcpus(node), 4)

    def test_missing_allocatable_is_none(self):
        self.assertIsNone(ns.allocatable_cpu_millicores({}))
        self.assertIsNone(ns.vcpus({}))


class LoadavgTest(unittest.TestCase):
    def test_reads_first_field(self):
        with tempfile.NamedTemporaryFile("w", suffix=".loadavg", delete=False) as f:
            f.write("25.0 12.3 10.1 5/2015 696\n")
            path = f.name
        try:
            self.assertEqual(ns.read_loadavg(path), 25.0)
        finally:
            import os
            os.unlink(path)

    def test_missing_file_is_none(self):
        self.assertIsNone(ns.read_loadavg("/nonexistent/loadavg"))


class LoadFromSummaryTest(unittest.TestCase):
    def test_summary_has_no_load_yet(self):
        # 現行 kubelet の stats/summary に host load は無い (P-9029 の審査指摘)
        self.assertIsNone(ns.load_from_summary({}))
        self.assertIsNone(ns.load_from_summary({"node": {"cpu": {"usageNanoCores": 1}}}))

    def test_future_load_field_is_parsed(self):
        self.assertEqual(
            ns.load_from_summary({"node": {"load": "25.0"}}), 25.0
        )


class JudgeTest(unittest.TestCase):
    """08-24 実測値 fixture (3761m/4000m・load 25) で警告が出ることを固定する。"""

    def test_20260824_fixture_fires_warning(self):
        report = ns.judge(3761, 4000, 25.0, 4)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(set(report["reasons"]), {"requests_ratio", "load"})
        self.assertAlmostEqual(report["requests_ratio"], 0.9403, places=4)
        self.assertEqual(ns.exit_code(report), 1)

    def test_comfortable_normal_is_ok(self):
        report = ns.judge(1800, 4000, 2.0, 4)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(ns.exit_code(report), 0)

    def test_requests_ratio_alone_fires(self):
        # load が取れない観測失敗時でも requests 比率側で鳴る
        report = ns.judge(3700, 4000, None, 4)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reasons"], ["requests_ratio"])
        self.assertEqual(ns.exit_code(report), 1)

    def test_load_alone_fires(self):
        report = ns.judge(None, 4000, 9.0, 4)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reasons"], ["load"])

    def test_boundary_at_90_percent(self):
        # ちょうど 90% は ok、90% 超で warn (rules.json の逆算踏襲)
        self.assertEqual(ns.judge(3600, 4000, None, 4)["status"], "ok")
        self.assertEqual(ns.judge(3601, 4000, None, 4)["status"], "warn")

    def test_load_exactly_at_vcpus_is_ok(self):
        self.assertEqual(ns.judge(None, 4000, 4.0, 4)["status"], "ok")
        self.assertEqual(ns.judge(None, 4000, 4.1, 4)["status"], "warn")

    def test_unknown_values_do_not_crash(self):
        report = ns.judge(None, None, None, None)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(ns.exit_code(report), 0)

    def test_review_time_cluster_state_fires_warn(self):
        # レビュー時の実測 (P-9037): 終端 pod を除いた Running のみで
        # 3924m/4000m = 98%。終端 pod 除外後の現状態では正しく warn が鳴る
        # (計器の役割どおり。レビュー文言で確認済み)。
        report = ns.judge(3924, 4000, 2.0, 4)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reasons"], ["requests_ratio"])


if __name__ == "__main__":
    unittest.main()