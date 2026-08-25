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

    def test_volume_fs_truthy_non_dict_is_skipped_not_crash(self):
        # pod volume[].fs が truthy な非 dict (list 等) だと、従来は
        # `(vol.get("fs") or {}).get("usedBytes")` が AttributeError を漏らし、
        # node.fs (総量) が読めるのに計測全体が source=error に落ちていた
        # (1 項目の壊れで計測全体を止めない契約 — P-9062)。該当 volume の寄与を
        # 数えずに次へ進み、健全な volume の合計と総量は載せる
        summary = {
            "node": {
                "nodeName": "node01",
                "fs": {
                    "availableBytes": 179000000000,
                    "capacityBytes": 270000000000,
                    "usedBytes": 74000000000,
                },
                "pods": [
                    {"podRef": {"name": "p1"}, "volume": [{"name": "data", "fs": ["x"]}]},
                    {"podRef": {"name": "p2"}, "volume": [{"name": "home", "fs": {"usedBytes": 250000000}}]},
                ],
            }
        }
        s = ru.sample_from_summary(summary)
        self.assertIsNotNone(s)
        self.assertEqual(s["source"], "kubelet_summary")
        self.assertEqual(s["used_bytes"], 74000000000)
        # 壊れた fs の volume は数えず、健全な volume の合計だけ載せる
        self.assertEqual(s["local_path_pvc_bytes"], 250000000)

    def test_volume_fs_truthy_non_dict_in_build_report_keeps_source(self):
        # 実測経路の結合: fs が truthy 非 dict の volume が混じっても build_report は
        # クラッシュせず source=kubelet_summary の正規 section + fill_days キーを返す
        summary = {
            "node": {
                "nodeName": "node01",
                "fs": {
                    "availableBytes": 179000000000,
                    "capacityBytes": 270000000000,
                    "usedBytes": 74000000000,
                },
                "pods": [
                    {"podRef": {"name": "p1"}, "volume": [{"name": "data", "fs": "oops"}]},
                ],
            }
        }
        section, _ = ru.build_report(
            [], "2026-08-25T00:00:00Z", node_name="node01", summary_doc=summary
        )
        self.assertEqual(section["source"], "kubelet_summary")
        self.assertIn("fill_days", section)


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

    def test_append_survives_corrupt_non_dict_tail(self):
        # 末尾が dict でない壊れた履歴 (ConfigMap の手動編集・旧版の書き込み等)。
        # 従来は AttributeError を漏らし、collect() が root_disk 節を {"error": ...}
        # にして fill_days キーの契約 (受入検証) を壊していた。追記して壊れは
        # forecast の _usable_samples に委ねる
        for bad_tail in (None, "corrupt", 5):
            hist = [{"ts": "2026-08-23T00:00:00Z", "used_bytes": 100}, bad_tail]
            samples = ru.append_sample(hist, 200, "2026-08-25T00:00:00Z")
            self.assertEqual(len(samples), 3)
            self.assertEqual(samples[-1], {"ts": "2026-08-25T00:00:00Z", "used_bytes": 200})

    def test_append_dedup_still_replaces_dict_tail(self):
        # 健全な dict 末尾の同一 ts 置き換えは従来どおり (二重カウントを防ぐ)
        samples = ru.append_sample(
            [{"ts": "2026-08-25T00:00:00Z", "used_bytes": 100}],
            210, "2026-08-25T00:00:00Z",
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["used_bytes"], 210)


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

    def test_corrupt_used_bytes_sample_is_dropped(self):
        # used_bytes が欠落・非数値のサンプル (ConfigMap 履歴の壊れ) で予報が
        # KeyError を漏らし root_disk 節を {"error": ...} にしてはいけない
        # (fill_days キーの契約が壊れる — P-9062)。健全な 2 点だけから計算する
        hist = [
            {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100},
            {"ts": "2026-08-24T00:00:00Z"},  # used_bytes 欠落
            {"ts": "2026-08-24T00:00:00Z", "used_bytes": "abc"},  # 非数値
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": 300},
        ]
        self.assertEqual(ru.daily_increase_bytes(hist), 100.0)

    def test_infinite_used_bytes_sample_is_dropped(self):
        # JSON は `1e999` / `Infinity` を float('inf') としてパースするため、
        # ConfigMap 履歴に手動編集等で入った巨大な値は int(inf) で OverflowError を
        # 漏らす (TypeError/ValueError とは違う例外族)。_num が None に倒し、
        # 予報がクラッシュしない (fill_days キーの契約 — P-9062)
        self.assertIsNone(ru._num(float("inf")))
        self.assertIsNone(ru._num(float("nan")))
        hist = [
            {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100},
            {"ts": "2026-08-24T00:00:00Z", "used_bytes": float("inf")},
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": 300},
        ]
        self.assertEqual(ru.daily_increase_bytes(hist), 100.0)

    def test_forecast_note_reports_dropped_corrupt_samples(self):
        # 履歴はあるが使えるサンプルが 2 点未満のとき、note は「若い」ではなく
        # 「破損で捨てた件数」を正直に載せる (計測不能をデータとして出す — P-9062)。
        # used_bytes 欠落 1 件 + 非数値 1 件を捨て、残りが 1 点しか無い場合
        hist = [
            {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100},
            {"ts": "2026-08-24T00:00:00Z"},  # used_bytes 欠落
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": "abc"},  # 非数値
        ]
        fc = ru.forecast(hist, 100000)
        self.assertIsNone(fc["fill_days"])
        self.assertIn("2 件が壊れている", fc["note"])
        self.assertIn("3", fc["note"])  # 生の履歴件数

    def test_forecast_with_corrupt_history_keeps_fill_days_contract(self):
        # 実測経路の結合: 壊れた履歴を与えても build_report は root_disk 節を
        # 必ず作り、fill_days キーを持つ (受入検証の契約。summary パース失敗の
        # fallback と同じ思想)
        hist = [
            {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100},
            {"ts": "2026-08-24T00:00:00Z"},  # used_bytes 欠落
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": 300},
        ]
        section, _ = ru.build_report(
            hist, "2026-08-25T12:00:00Z", node_name="node01", summary_doc=None
        )
        self.assertIn("fill_days", section)
        self.assertEqual(section["source"], "statvfs")

    def test_build_report_keeps_fill_days_with_non_dict_history_entry(self):
        # 実測経路の結合: 履歴に dict でないエントリ (None 等) が混じっても
        # append_sample はクラッシュせず、build_report は root_disk 節を必ず作り
        # fill_days キーを持つ (受入検証の契約。ed22bfba の _usable_samples 硬化が
        # 非 dict 末尾の AttributeError を塞ぎ損ねていた経路)
        hist = [
            {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100},
            None,
            {"ts": "2026-08-25T00:00:00Z", "used_bytes": 300},
        ]
        section, samples = ru.build_report(
            hist, "2026-08-25T12:00:00Z", node_name="node01", summary_doc=None
        )
        self.assertIn("fill_days", section)
        self.assertEqual(section["source"], "statvfs")
        # 非 dict の None は潰されず残り、新サンプルが末尾に追記される
        self.assertEqual(len(samples), 4)
        self.assertIsNone(samples[1])
        self.assertEqual(samples[-1]["ts"], "2026-08-25T12:00:00Z")


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

    def test_total_measurement_failure_keeps_fill_days_contract(self):
        # 計測が完全に失敗 (summary が None で statvfs も例外) でも build_report は
        # 例外を漏らさず正規の section (source=error) + fill_days キーを返す。
        # 例外を漏らすと report.py の collect() が root_disk 節を {"error": ...}
        # にして fill_days キーの契約 (受入検証) が壊れる — summary パース失敗・
        # 履歴の壊れと同じ論理で塞ぐ (P-9062)
        real_disk_usage = ru.shutil.disk_usage
        self.addCleanup(setattr, ru.shutil, "disk_usage", real_disk_usage)
        ru.shutil.disk_usage = lambda p: (_ for _ in ()).throw(OSError("device busy"))
        hist = [{"ts": "2026-08-23T00:00:00Z", "used_bytes": 100}]
        section, samples = ru.build_report(hist, "2026-08-25T12:00:00Z")
        self.assertEqual(section["source"], "error")
        self.assertIn("fill_days", section)
        self.assertIsNone(section["fill_days"])
        self.assertIsNotNone(section["fill_days_note"])
        # 履歴は汚さない (計測不能のエントリを混ぜない)
        self.assertEqual(samples, hist)
        # collect() の wrap でも root_disk 節を {"error": ...} にしない
        def collect(fn):
            try:
                return fn()
            except Exception as e:
                return {"error": "{}: {}".format(type(e).__name__, e)}
        wrapped = collect(lambda: ru.build_report([], "2026-08-25T12:00:00Z"))[0]
        self.assertIn("fill_days", wrapped)

    def test_infinite_used_bytes_history_keeps_fill_days_contract(self):
        # 実測経路の結合: 履歴の used_bytes に inf (JSON の `1e999`/`Infinity` の
        # パース結果) が混じっても build_report は例外を漏らさず root_disk 節を
        # 必ず作り fill_days キーを持つ。_num の OverflowError 取りこぼしは
        # _usable_samples → forecast → build_report を突き抜けて collect() が
        # root_disk 節を {"error": ...} にし受入検証の契約を壊す (P-9062)
        hist = [
            {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100},
            {"ts": "2026-08-24T00:00:00Z", "used_bytes": float("inf")},
        ]
        section, samples = ru.build_report(
            hist, "2026-08-25T12:00:00Z", node_name="node01", summary_doc=None
        )
        self.assertIn("fill_days", section)
        self.assertEqual(section["source"], "statvfs")
        # inf エントリは捨てられ、健全な古い 1 点 + 今回の 1 点が残る
        self.assertEqual(len(samples), 3)


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