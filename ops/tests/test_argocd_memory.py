"""apps/ops-health-reporter/argocd_memory.py (P-0181, 近接警報) の純関数を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_argocd_memory`。
argocd_memory.py は report.py と同じく ConfigMap から直接起動される単一ファイルの
ためパッケージではなく、テストからは importlib で実ファイルをロードする
(test_download_budget.py と同じ形)。

固定する契約:
- quantity パース: 無印バイト / Ki / Mi / Gi の整数のみ。未知の揺れは黙って読まず
  例外で呼び出し側に見せる (ops/tools/argocd_memory_series.parse_quantity_bytes と
  同一 fixture 表で両方向に固定 — 2 つの実装の drift をここで捕まえる)
- 閾値検査: bool・非 int・範囲外は例外。同期コピーの破損を no_data に畳んで沈黙させない
- 判定: 境界は鳴る側 (warn/exceed) に倒す。limit 無しは決め打ちせず unconfigured を正直に返す
- 合成: pod_metrics / 実機 pod GET 応答の fixture から usage・limit を取り違えない。
  別 pod・別コンテナ・quantity 破損で落ちない (no_data として語る)

近接警報の閾値そのもの (rules.json ↔ argocd-alerts.json) の一致は CI の
ops/check_argocd_alert_sync.py の管轄。こちらでは checker の純関数も試す。
"""

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "argocd_memory.py"
CHECKER_PATH = REPO / "ops" / "check_argocd_alert_sync.py"


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


am = _load_module(MODULE_PATH, "argocd_memory_under_test")
checker = _load_module(CHECKER_PATH, "check_argocd_alert_sync_under_test")

MIB = 1024 * 1024


class TestParseQuantityBytes(unittest.TestCase):
    def test_examples(self):
        # argocd_memory_series.parse_quantity_bytes と同じ表。片方だけ変わったらここで割れる
        cases = {
            "320908Ki": 320908 * 1024,
            "239Mi": 239 * 1024 * 1024,
            "1Gi": 1024**3,
            "204800": 204800,
            "768Mi": 768 * MIB,
        }
        for raw, want in cases.items():
            self.assertEqual(am.parse_quantity_bytes(raw), want, raw)
        # 前後空白は許容する (metrics-server が付けることは無いが、手編集の JSON では出る)
        self.assertEqual(am.parse_quantity_bytes(" 100Ki "), 100 * 1024)

    def test_rejects_unknown_forms(self):
        for bad in ["1.5Ki", "-5Ki", "100xi", "", None, "12m"]:
            with self.assertRaises((ValueError, TypeError), msg=repr(bad)):
                am.parse_quantity_bytes(bad)


class TestCoerceWarnPercent(unittest.TestCase):
    def test_accepts_valid_range(self):
        self.assertEqual(am.coerce_warn_percent(1), 1)
        self.assertEqual(am.coerce_warn_percent(80), 80)
        self.assertEqual(am.coerce_warn_percent(100), 100)

    def test_rejects_bool_and_non_int(self):
        # True は int の派生なので明示的に弾く (download_budget.coerce_bytes 同型)
        for bad in [True, False, "80", 80.0, None]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                am.coerce_warn_percent(bad)

    def test_rejects_out_of_range(self):
        for bad in [0, -5, 101]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                am.coerce_warn_percent(bad)


class TestJudge(unittest.TestCase):
    LIMIT = 768 * MIB

    def test_no_data_when_usage_missing(self):
        v = am.judge(None, self.LIMIT, 80)
        self.assertEqual(v["status"], "no_data")
        self.assertIsNone(v["limit_usage_percent"])

    def test_unconfigured_when_limit_missing(self):
        # limit が読めない世界で決め打ちの判定を出さないのが契約
        v = am.judge(100 * MIB, None, 80)
        self.assertEqual(v["status"], "unconfigured")
        self.assertIsNone(v["limit_usage_percent"])
        v = am.judge(100 * MIB, 0, 80)  # 0 も比較軸として無効
        self.assertEqual(v["status"], "unconfigured")

    def test_ok_below_threshold(self):
        v = am.judge(int(self.LIMIT * 0.5), self.LIMIT, 80)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["limit_usage_percent"], 50.0)

    def test_warn_at_exact_boundary(self):
        # 境界は鳴る側 (>=)。上限近傍の計器が沈黙するのは手遅れ型 (download_budget 同じ倒し方)
        v = am.judge(int(self.LIMIT * 0.8), self.LIMIT, 80)
        self.assertEqual(v["status"], "warn")
        self.assertEqual(v["limit_usage_percent"], 80.0)

    def test_rounded_display_value_decides_the_boundary(self):
        # 判定は丸め後の表示値で行う。真値 79.99% は表示 80.0% なので鳴る側
        v = am.judge(7999, 10000, 80)
        self.assertEqual(v["status"], "warn")
        self.assertEqual(v["limit_usage_percent"], 80.0)
        # 真値 79.94% は表示 79.9% なので ok 側
        v = am.judge(7994, 10000, 80)
        self.assertEqual(v["status"], "ok")

    def test_exceed_at_exact_100_percent(self):
        v = am.judge(self.LIMIT, self.LIMIT, 80)
        self.assertEqual(v["status"], "exceed")

    def test_exceed_above_100_percent(self):
        # OOMKill 直前の世界。usage > limit は metrics-server のサンプル間隔で実際に起きる
        v = am.judge(self.LIMIT + 10 * MIB, self.LIMIT, 80)
        self.assertEqual(v["status"], "exceed")


class TestFindContainerUsage(unittest.TestCase):
    METRICS = [
        {"namespace": "argocd", "name": "argocd-repo-server-xyz",
         "containers": [{"name": "repo-server", "memory": "999999Ki"}]},
        {"namespace": "argocd", "name": "argocd-application-controller-0",
         "containers": [
             {"name": "application-controller", "cpu": "5m", "memory": "204328960"},
         ]},
    ]

    def test_finds_target_container(self):
        self.assertEqual(
            am.find_container_usage(self.METRICS), "204328960"
        )

    def test_wrong_pod_or_namespace_is_none(self):
        self.assertIsNone(am.find_container_usage([]))
        self.assertIsNone(am.find_container_usage([{"namespace": "immich"}]))
        self.assertIsNone(
            am.find_container_usage(
                [{"namespace": "argocd", "name": "argocd-application-controller-9"}]
            )
        )

    def test_error_entry_shape_does_not_raise(self):
        # collect() が畳んだ error dict が混ざっても列挙で壊れない
        self.assertIsNone(
            am.find_container_usage([{"error": "HTTPError: 503"}])
        )


class TestFindContainerLimit(unittest.TestCase):
    POD = {
        "spec": {
            "containers": [
                {"name": "application-controller",
                 "resources": {"requests": {"memory": "320Mi"},
                               "limits": {"memory": "768Mi"}}},
                {"name": "sidecar"},
            ]
        }
    }

    def test_reads_live_limit_not_hardcoded(self):
        self.assertEqual(am.find_container_limit(self.POD), "768Mi")

    def test_missing_pieces_are_none(self):
        self.assertIsNone(am.find_container_limit(None))
        self.assertIsNone(am.find_container_limit({}))
        self.assertIsNone(am.find_container_limit({"spec": {}}))
        self.assertIsNone(
            am.find_container_limit({"spec": {"containers": [{"name": "other"}]}})
        )
        # limits 自体が無いコンテナ
        self.assertIsNone(
            am.find_container_limit({"spec": {"containers": [{"name": "application-controller"}]}})
        )


class TestBuildReport(unittest.TestCase):
    POD = {
        "metadata": {"name": "argocd-application-controller-0"},
        "spec": {
            "containers": [
                {"name": "application-controller",
                 "resources": {"limits": {"memory": "768Mi"}}},
            ]
        },
    }

    def metrics(memory):
        return [
            {"namespace": "argocd", "name": "argocd-application-controller-0",
             "containers": [{"name": "application-controller", "memory": memory}]},
        ]

    def test_happy_path_matches_observed_peak(self):
        # 実測ピーク 398.0Mi @2026-08-23T04:30:08Z vs 新 limit 768Mi ≈ 51.8%
        section = am.build_report(TestBuildReport.metrics("407552Ki"), TestBuildReport.POD, 80)
        self.assertEqual(section["usage_bytes"], 407552 * 1024)
        self.assertEqual(section["limit_bytes"], 768 * MIB)
        self.assertEqual(section["limit_usage_percent"], 51.8)
        self.assertEqual(section["status"], "ok")
        self.assertEqual(section["pod"], "argocd-application-controller-0")
        self.assertEqual(section["container"], "application-controller")
        self.assertEqual(section["warn_percent"], 80)
        # limit が実機由来であることがセクション自体にも載る (ハードコードで無い証明)
        self.assertIn("live pod", section["limit_source"])

    def test_warn_wired_to_threshold(self):
        section = am.build_report(TestBuildReport.metrics("614400Ki"), TestBuildReport.POD, 80)
        # 600Mi / 768Mi = 78.125% → 丸め 78.1 → ok。境界直下は ok で通す
        self.assertEqual(section["status"], "ok")
        section = am.build_report(TestBuildReport.metrics("645120Ki"), TestBuildReport.POD, 80)
        # 630Mi / 768Mi = 82.03% → warn
        self.assertEqual(section["status"], "warn")

    def test_unparsable_usage_becomes_no_data_with_reason(self):
        section = am.build_report(TestBuildReport.metrics("12m"), TestBuildReport.POD, 80)
        self.assertEqual(section["status"], "no_data")
        self.assertIn("使用量サンプルを読めない", section["reason"])
        self.assertIn("12m", section["reason"])

    def test_missing_usage_becomes_no_data(self):
        section = am.build_report([], TestBuildReport.POD, 80)
        self.assertEqual(section["status"], "no_data")

    def test_missing_pod_becomes_unconfigured(self):
        # usage はあるのに pod GET 応答が空 → 比較軸が無い。usage=0 で誤魔化さない
        section = am.build_report(TestBuildReport.metrics("204328960"), None, 80)
        self.assertEqual(section["status"], "unconfigured")
        self.assertEqual(section["usage_bytes"], 204328960)
        self.assertIsNone(section["limit_bytes"])

    def test_broken_limit_quantity_becomes_unconfigured_with_reason(self):
        pod = {"spec": {"containers": [
            {"name": "application-controller",
             "resources": {"limits": {"memory": "512Mib"}}},
        ]}}
        section = am.build_report(TestBuildReport.metrics("204328960"), pod, 80)
        self.assertEqual(section["status"], "unconfigured")
        self.assertIn("512Mib", section["reason"])

    def test_invalid_threshold_raises_instead_of_silence(self):
        # 同期コピー破損は reporter 設定事故なので no_data に畳まず落とす
        with self.assertRaises(ValueError):
            am.build_report(TestBuildReport.metrics("1Ki"), TestBuildReport.POD, None)
        with self.assertRaises(ValueError):
            am.build_report(TestBuildReport.metrics("1Ki"), TestBuildReport.POD, 200)

    def test_non_list_metrics_raises(self):
        # report.py 側で error dict を弾いている前提だが、来たら黙らせず落とす
        with self.assertRaises(TypeError):
            am.build_report({"error": "boom"}, TestBuildReport.POD, 80)


class TestCheckArgocdAlertSyncChecker(unittest.TestCase):
    """CI チェッカー (ops/check_argocd_alert_sync.py) の純関数を固定する。"""

    KUSTOMIZATION_OK = """
configMapGenerator:
  files:
    - report.py
    - argocd-alerts.json
"""

    def good_docs(self):
        rules = {"argocd_controller": {"memory_limit_warn_percent": 80}}
        copy = {"source": checker.EXPECTED_SOURCE, "memory_limit_warn_percent": 80}
        return rules, copy

    def test_consistent_inputs_have_no_problems(self):
        rules, copy = self.good_docs()
        self.assertEqual(
            checker.collect_problems(rules, copy, self.KUSTOMIZATION_OK), []
        )

    def test_drift_between_rules_and_copy(self):
        rules, copy = self.good_docs()
        copy["memory_limit_warn_percent"] = 90
        problems = checker.collect_problems(rules, copy, self.KUSTOMIZATION_OK)
        joined = "\n".join(problems)
        self.assertTrue(any("一致しません" in p for p in problems))
        self.assertIn("80", joined)
        self.assertIn("90", joined)

    def test_missing_section_or_key_fails_closed(self):
        rules, copy = self.good_docs()
        del rules["argocd_controller"]
        problems = checker.collect_problems(rules, copy, self.KUSTOMIZATION_OK)
        self.assertTrue(any("節" in p for p in problems))

        rules, copy = self.good_docs()
        del copy["memory_limit_warn_percent"]
        problems = checker.collect_problems(rules, copy, self.KUSTOMIZATION_OK)
        self.assertTrue(any(checker.COPY_REL in p for p in problems))

    def test_wrong_source_pointer_rejected(self):
        rules, copy = self.good_docs()
        copy["source"] = "somewhere/else"
        problems = checker.collect_problems(rules, copy, self.KUSTOMIZATION_OK)
        self.assertTrue(any("source" in p for p in problems))

    def test_out_of_range_values_rejected_on_both_sides(self):
        rules, copy = self.good_docs()
        rules["argocd_controller"]["memory_limit_warn_percent"] = 0
        copy["memory_limit_warn_percent"] = 0
        problems = checker.collect_problems(rules, copy, self.KUSTOMIZATION_OK)
        self.assertEqual(len(problems), 2)  # rules 側と copy 側の両方

    def test_copy_not_listed_in_kustomization(self):
        rules, copy = self.good_docs()
        problems = checker.collect_problems(
            rules, copy, "configMapGenerator:\n  files:\n    - report.py\n"
        )
        self.assertTrue(any("configMapGenerator" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
