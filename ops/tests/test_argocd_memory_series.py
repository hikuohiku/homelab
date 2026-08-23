"""ops/tools/argocd_memory_series.py の集計ロジックを fixture で固定する (P-0181)。

リポジトリルートから `python3 -m unittest ops.tests.test_argocd_memory_series`。

「今たまたま通っている」と「正しい」を区別するため、判定はすべて合成 fixture で
両方向に固定する。CLI 結合試験は tempfile の jsonl を --dir で読ませ、git や
リモートブランチには一切依存しない (CI は shallow clone の可能性があるため)。
--check の「未来の追記で落ちない」性質 (窓ピン留め再計算) もここで証明する。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ops.tools import argocd_memory_series as ams

TOOL = Path(__file__).resolve().parents[2] / "ops" / "tools" / "argocd_memory_series.py"


def report(ts, entries):
    """pod_metrics 1 回分を作る。entries は (pod名, [(container名, memory)]) の列。"""
    return {
        "generated_at": ts,
        "pod_metrics": [
            {
                "namespace": "argocd",
                "name": pod,
                "containers": [{"name": c, "cpu": "1n", "memory": m} for c, m in cs],
            }
            for pod, cs in entries
        ],
    }


TARGET = ("argocd-application-controller-0", [("application-controller", "100000Ki")])


class TestParseQuantityBytes(unittest.TestCase):
    def test_examples(self):
        cases = {
            "320908Ki": 320908 * 1024,
            "239Mi": 239 * 1024 * 1024,
            "1Gi": 1024**3,
            "204800": 204800,
        }
        for raw, want in cases.items():
            self.assertEqual(ams.parse_quantity_bytes(raw), want, raw)

    def test_rejects_unknown_forms(self):
        # metrics-server は整数 + Ki/Mi/Gi で載せる。それ以外の揺れは黙って
        # 読み飛ばさない (ValueError で呼び出し側に数えさせる)
        for bad in ["1.5Ki", "-5Ki", "100xi", "", None, "12m"]:
            with self.assertRaises((ValueError, TypeError)):
                ams.parse_quantity_bytes(bad)


class TestParseTs(unittest.TestCase):
    def test_roundtrip(self):
        ts = ams.parse_ts("2026-08-23T09:30:05Z")
        self.assertEqual(ts.strftime(ams.TS_FORMAT), "2026-08-23T09:30:05Z")
        self.assertIsNotNone(ts.tzinfo)

    def test_rejects_other_formats(self):
        with self.assertRaises(ValueError):
            ams.parse_ts("2026-08-23 09:30:05")


class TestExtractSeries(unittest.TestCase):
    def test_filters_and_sorts(self):
        reports = [
            report("2026-08-06T08:00:04Z", [TARGET]),
            # 別 pod (StatefulSet 名の揺れ) と別コンテナは拾わない
            report(
                "2026-08-05T20:00:04Z",
                [
                    (
                        "argocd-application-controller-9",
                        [("application-controller", "999999Ki")],
                    ),
                    TARGET,
                ],
            ),
            report(
                "2026-08-05T08:00:04Z",
                [("argocd-application-controller-0", [("redis", "888888Ki")])],
            ),
            # collect() が例外を畳んだ回 / 壊れた行 (JSON パース済み None)
            {"error": "HTTPError: 500"},
            None,
            {"generated_at": "2026-08-07T00:00:00Z"},  # pod_metrics キー無し
        ]
        series, skipped = ams.extract_series(reports)
        self.assertEqual([b for _, b in sorted(series)], [102400000, 102400000])
        self.assertEqual([ts.strftime("%Y-%m-%dT%H:%M:%SZ") for ts, _ in series],
                         ["2026-08-05T20:00:04Z", "2026-08-06T08:00:04Z"])
        self.assertEqual(skipped, 1)  # 壊れた行のみ。欠損回は数えない

    def test_counts_broken_quantity_as_skipped(self):
        reports = [
            {
                "generated_at": "2026-08-05T08:00:04Z",
                "pod_metrics": [
                    {
                        "namespace": "argocd",
                        "name": "argocd-application-controller-0",
                        "containers": [{"name": "application-controller", "memory": "junk"}],
                    }
                ],
            }
        ]
        series, skipped = ams.extract_series(reports)
        self.assertEqual(series, [])
        self.assertEqual(skipped, 1)


class TestPercentile(unittest.TestCase):
    def test_interpolation(self):
        self.assertEqual(ams.percentile([10.0, 20.0], 0.95), 19.5)
        self.assertEqual(ams.percentile([1.0], 0.95), 1.0)
        vals = sorted(float(v) for v in range(1, 101))
        self.assertAlmostEqual(ams.percentile(vals, 0.95), 95.05)
        self.assertEqual(ams.percentile(vals, 0.5), 50.5)

    def test_edges(self):
        with self.assertRaises(ValueError):
            ams.percentile([], 0.95)
        with self.assertRaises(ValueError):
            ams.percentile([1.0], 1.5)


class TestSummarize(unittest.TestCase):
    def make_series(self):
        rows = [
            ("2026-08-05T08:00:04Z", 100000),
            ("2026-08-05T20:00:04Z", 150000),
            ("2026-08-06T08:00:04Z", 120000),
            ("2026-08-06T20:00:04Z", 160000),
        ]
        return [(ams.parse_ts(t), b * 1024) for t, b in rows]

    def test_fixed_values(self):
        s = ams.summarize(self.make_series())
        self.assertEqual(s["sample_count"], 4)
        self.assertEqual(s["peak_bytes"], 160000 * 1024)
        self.assertEqual(s["peak_timestamp"], "2026-08-06T20:00:04Z")
        self.assertEqual(s["min_bytes"], 100000 * 1024)
        # 中央値: (120000+150000)/2、p95: 150000+(160000-150000)x0.85 (線形補間)
        self.assertEqual(s["median_bytes"], 135000 * 1024)
        self.assertEqual(s["p95_bytes"], int(round(158500 * 1024)))
        self.assertEqual(
            s["daily_peak_bytes"], {"2026-08-05": 150000 * 1024, "2026-08-06": 160000 * 1024}
        )

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            ams.summarize([])


class TestAnalyzeGrowth(unittest.TestCase):
    def series_from(self, rows):
        return [(ams.parse_ts(t), b * 1024) for t, b in rows]

    def test_stable_series(self):
        g = ams.analyze_growth(self.series_from([
            ("2026-08-05T00:00:04Z", 100000),
            ("2026-08-06T00:00:04Z", 100020),
            ("2026-08-07T00:00:04Z", 100010),
            ("2026-08-08T00:00:04Z", 100030),
        ]))
        # 手計算: 傾き +8 Ki/day (実データの +15.9 Ki/day 同等の微小トレンド)。
        # 中央値比の有意閾値に遠く及ばない
        self.assertEqual(g["slope_ki_per_day"], 8.0)
        self.assertFalse(g["leak_suspect"])
        self.assertFalse(g["significant"])

    def test_monotonic_is_leak_suspect(self):
        g = ams.analyze_growth(self.series_from([
            ("2026-08-05T08:00:04Z", 100000),
            ("2026-08-05T08:30:04Z", 101000),
            ("2026-08-05T09:00:04Z", 102000),
            ("2026-08-05T10:00:04Z", 104000),
        ]))
        self.assertTrue(g["leak_suspect"])
        self.assertTrue(g["significant"])
        # 2 時間で +4000Ki → 日割り +48000Ki
        self.assertEqual(g["slope_ki_per_day"], 48000.0)

    def test_restart_resets_break_monotonic(self):
        # 再起動で使用量がリセットされる現実の形。傾きは有意でも単調ではない
        rows = [
            ("2026-08-01T00:00:00Z", 100000),
            ("2026-08-01T12:00:00Z", 190000),
            ("2026-08-02T00:00:00Z", 105000),  # リセット
            ("2026-08-03T00:00:00Z", 250000),
            ("2026-08-04T00:00:00Z", 110000),  # リセット
            ("2026-08-05T00:00:00Z", 300000),
        ]
        g = ams.analyze_growth(self.series_from(rows))
        self.assertFalse(g["leak_suspect"])
        self.assertTrue(g["significant"])  # 1 日あたり数十 Mi の上昇トレンド
        self.assertGreater(g["slope_ki_per_day"], 10000)

    def test_negative_slope_not_significant(self):
        g = ams.analyze_growth(self.series_from([
            ("2026-08-01T00:00:00Z", 300000),
            ("2026-08-02T00:00:00Z", 290000),
            ("2026-08-03T00:00:00Z", 280000),
        ]))
        self.assertFalse(g["significant"])

    def test_single_point_has_no_slope(self):
        g = ams.analyze_growth(self.series_from([("2026-08-01T00:00:00Z", 100000)]))
        self.assertIsNone(g["slope_ki_per_day"])
        self.assertFalse(g["significant"])
        self.assertFalse(g["leak_suspect"])


class TestCheckDocument(unittest.TestCase):
    def doc(self):
        series = [(ams.parse_ts(t), b * 1024) for t, b in [
            ("2026-08-05T08:00:04Z", 100000),
            ("2026-08-05T20:00:04Z", 150000),
        ]]
        return ams.build_document(series, {"kind": "dir", "path": "/x"}, 0)

    def test_self_consistent(self):
        d = self.doc()
        self.assertEqual(ams.check_document(d, d), [])

    def test_source_and_notes_not_compared(self):
        d = self.doc()
        other = json.loads(json.dumps(d))
        other["source"] = {"kind": "git", "ref": "origin/ops-health-report"}
        other["notes"] = []
        self.assertEqual(ams.check_document(other, d), [])

    def test_detects_nested_diff_with_path(self):
        d = self.doc()
        tampered = json.loads(json.dumps(d))
        day = sorted(d["stats"]["daily_peak_bytes"])[0]
        tampered["stats"]["daily_peak_bytes"][day] += 1024
        diffs = ams.check_document(tampered, d)
        self.assertEqual(len(diffs), 1)
        self.assertIn("stats.daily_peak_bytes.{}".format(day), diffs[0])


class TestCli(unittest.TestCase):
    """--json / --check の結合試験。git を使わず --dir のみで通す。"""

    def write_fixtures(self, root):
        def line(obj):
            return json.dumps(obj, ensure_ascii=False)

        decoy = report(
            "2026-08-05T20:00:04Z",
            [("argocd-application-controller-9", [("application-controller", "999999Ki")])],
        )
        err = {"error": "HTTPError: 500"}
        day1 = "\n".join([
            line(report("2026-08-05T08:00:04Z", [TARGET])),
            line(decoy),
            line(err),
            "{ this is broken json",
        ]) + "\n"
        day2 = "\n".join([
            line(report("2026-08-06T08:00:04Z", [TARGET])),
            line(report("2026-08-06T20:00:04Z", [
                ("argocd-application-controller-0", [("application-controller", "160000Ki")])
            ])),
        ]) + "\n"
        (root / "2026-08-05.jsonl").write_text(day1, encoding="utf-8")
        (root / "2026-08-06.jsonl").write_text(day2, encoding="utf-8")

    def run_tool(self, args):
        return subprocess.run(
            [sys.executable, str(TOOL)] + args, capture_output=True, text=True
        )

    def test_json_report_and_check_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hist = root / "history"
            hist.mkdir()
            self.write_fixtures(hist)
            ev = root / "memory-series.json"

            # --json が機械可読ドキュメントを出す (壊れた行・decoy を除いた実系列)
            out = self.run_tool(["--dir", str(hist), "--json"])
            self.assertEqual(out.returncode, 0, out.stderr)
            doc = json.loads(out.stdout)
            self.assertEqual(doc["sample_count"], 3)  # 100000Ki + 100000Ki + 160000Ki
            self.assertEqual(doc["skipped_lines"], 1)  # 壊れた JSON 1 行
            self.assertEqual(doc["stats"]["peak_bytes"], 160000 * 1024)
            self.assertIn("peak_timestamp", doc["stats"])
            ev.write_text(out.stdout, encoding="utf-8")

            # 人間可読出力にもピーク/p95/成長率が出る
            human = self.run_tool(["--dir", str(hist)])
            self.assertEqual(human.returncode, 0)
            self.assertIn("ピーク", human.stdout)
            self.assertIn("p95", human.stdout)
            self.assertIn("成長率", human.stdout)

            # --check: 再計算が証跡と一致 (冪等)
            ok = self.run_tool(["--dir", str(hist), "--check", "--evidence", str(ev)])
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

            # 未来の追記 (履歴は append-only) でも窓ピン留めにより落ちない
            future = json.dumps(report("2027-01-01T00:00:00Z", [TARGET]), ensure_ascii=False)
            (hist / "2027-01-01.jsonl").write_text(future + "\n", encoding="utf-8")
            ok2 = self.run_tool(["--dir", str(hist), "--check", "--evidence", str(ev)])
            self.assertEqual(ok2.returncode, 0, ok2.stdout + ok2.stderr)

            # 窓内の書き換えは不一致として落ちる (沈黙しない)
            tampered = json.loads(ev.read_text(encoding="utf-8"))
            tampered["stats"]["peak_bytes"] += 1
            ev.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            bad = self.run_tool(["--dir", str(hist), "--check", "--evidence", str(ev)])
            self.assertEqual(bad.returncode, 1)
            self.assertIn("--check 不一致", bad.stdout)

    def test_missing_data_is_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()
            out = self.run_tool(["--dir", str(root)])
            self.assertEqual(out.returncode, 2)
            self.assertIn("error:", out.stderr)

    def test_missing_evidence_is_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hist = root / "history"
            hist.mkdir()
            self.write_fixtures(hist)
            out = self.run_tool(
                ["--dir", str(hist), "--check", "--evidence", str(root / "nope.json")]
            )
            self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
