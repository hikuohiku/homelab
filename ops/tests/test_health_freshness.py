"""ops/check_health_freshness.py の純粋関数と CLI 契約 (rc) を固定する。

雛形は ops/tests/test_check_heartbeat_fresh.py (P-0027)。ops/heart/tests/ に置かない
理由も同じ — 非保護スクリプトのテストで CODEOWNERS レビューを発生させない。
リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`。
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from ops import check_health_freshness as chf
from ops.heart import triage

REPO = Path(__file__).resolve().parents[2]
FIXTURE_STALE = REPO / "ops" / "tests" / "fixtures" / "health" / "stale-latest.json"
with open(REPO / "ops" / "rules.json") as f:
    RULES = json.load(f)

NOW = datetime.datetime(2026, 8, 23, 16, 0, 0, tzinfo=datetime.timezone.utc)
MAX_AGE_SECONDS = 3 * 3600


def generated_at(delta_seconds):
    """NOW から delta_seconds 前の時刻を reporter と同じ書式で。"""
    t = NOW - datetime.timedelta(seconds=delta_seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestJudge(unittest.TestCase):
    def test_fresh(self):
        v = chf.judge({"generated_at": generated_at(60)}, NOW, MAX_AGE_SECONDS)
        self.assertFalse(v["stale"])
        self.assertEqual(v["age_seconds"], 60)

    def test_exactly_max_age_is_stale(self):
        # 境界は stale 側に含める (>=)。閾値ちょうどで「まだ元気」に倒さない
        v = chf.judge({"generated_at": generated_at(MAX_AGE_SECONDS)}, NOW, MAX_AGE_SECONDS)
        self.assertTrue(v["stale"])

    def test_just_under_threshold_is_fresh(self):
        v = chf.judge({"generated_at": generated_at(MAX_AGE_SECONDS - 1)}, NOW, MAX_AGE_SECONDS)
        self.assertFalse(v["stale"])

    def test_far_past_is_stale(self):
        v = chf.judge({"generated_at": "2000-01-01T00:00:00Z"}, NOW, MAX_AGE_SECONDS)
        self.assertTrue(v["stale"])
        self.assertGreater(v["age_seconds"], MAX_AGE_SECONDS)

    def test_future_generated_at_is_not_stale_but_reported(self):
        v = chf.judge({"generated_at": generated_at(-300)}, NOW, MAX_AGE_SECONDS)
        self.assertFalse(v["stale"])
        self.assertIn("skew", v["reason"])

    def test_missing_generated_at_is_stale(self):
        v = chf.judge({"report": {}}, NOW, MAX_AGE_SECONDS)
        self.assertTrue(v["stale"])
        self.assertIsNone(v["age_seconds"])

    def test_non_string_generated_at_is_stale(self):
        v = chf.judge({"generated_at": 12345}, NOW, MAX_AGE_SECONDS)
        self.assertTrue(v["stale"])

    def test_unparsable_generated_at_is_stale(self):
        v = chf.judge({"generated_at": "yesterday"}, NOW, MAX_AGE_SECONDS)
        self.assertTrue(v["stale"])

    def test_load_error_is_stale(self):
        v = chf.judge(None, NOW, MAX_AGE_SECONDS, load_error="読めない")
        self.assertTrue(v["stale"])


class TestReadLatest(unittest.TestCase):
    """fail-closed の入口。ファイル系の異常は judge に「stale」として渡ること。"""

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            doc, err = chf.read_latest(Path(d) / "nope.json")
        self.assertIsNone(doc)
        self.assertTrue(chf.judge(doc, NOW, MAX_AGE_SECONDS, err)["stale"])

    def test_broken_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            p.write_text("{ not json")
            doc, err = chf.read_latest(p)
        self.assertIsNone(doc)
        self.assertTrue(chf.judge(doc, NOW, MAX_AGE_SECONDS, err)["stale"])

    def test_non_object_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            p.write_text("[]")
            doc, err = chf.read_latest(p)
        self.assertIsNone(doc)
        self.assertTrue(chf.judge(doc, NOW, MAX_AGE_SECONDS, err)["stale"])

    def test_reads_real_document(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            p.write_text(json.dumps({"generated_at": generated_at(10), "checks": {}}))
            doc, err = chf.read_latest(p)
        self.assertIsNone(err)
        self.assertFalse(chf.judge(doc, NOW, MAX_AGE_SECONDS)["stale"])


class TestDefaults(unittest.TestCase):
    def test_default_threshold_covers_several_cycles(self):
        # 書き手の CronJob は 30 分毎。既定値は一時的な失敗数回では鳴らない余裕を持つ
        self.assertGreaterEqual(chf.DEFAULT_MAX_AGE_HOURS, 1)


class TestBuildBody(unittest.TestCase):
    def body(self, **kw):
        j = chf.judge({"generated_at": "2000-01-01T00:00:00Z"}, NOW, MAX_AGE_SECONDS)
        return chf.build_body(j, kw.pop("marker", chf.MARKER), NOW, 3.0, **kw)

    def test_not_eaten_by_triage(self):
        """最大の罠: 投稿した本文は次のビートで triage.classify に食われる。
        全停止 (stop_all) や veto に倒れたら、watchdog が器を止めてしまう。"""
        for kw in ({}, {"run_url": "https://example.invalid/run/1"}):
            body = self.body(**kw)
            self.assertEqual(triage.classify(body, RULES)["kind"], "review_needed", kw)

    def test_body_is_long_enough(self):
        # 50 文字以下だと triage が部分一致で停止キーワードを拾う
        self.assertGreater(len(self.body()), 50)

    def test_no_stop_or_resume_keyword_at_line_start(self):
        for line in self.body().splitlines():
            for kw in RULES["veto"]["stop_keywords"] + RULES["veto"].get("resume_keywords", []):
                self.assertFalse(line.strip().lower().startswith(kw.lower()), (line, kw))

    def test_no_veto_or_ack_pattern(self):
        for pattern in (r"(?i)veto\s+P-\d{4}", r"(?i)ack\s+P-\d{4}"):
            self.assertNotRegex(self.body(), pattern)

    def test_marker_is_present(self):
        self.assertIn(chf.MARKER, self.body())

    def test_body_survives_missing_age(self):
        j = chf.judge(None, NOW, MAX_AGE_SECONDS, load_error="読めない")
        body = chf.build_body(j, chf.MARKER, NOW, 3.0)
        self.assertIn(chf.MARKER, body)
        self.assertEqual(triage.classify(body, RULES)["kind"], "review_needed")


class TestMainExitCodes(unittest.TestCase):
    """workflow が rc で分岐するので、rc は契約そのもの。"""

    def run_main(self, doc, extra=None, write=True):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            if write:
                p.write_text(json.dumps(doc))
            argv = [str(p), "--now", NOW.strftime("%Y-%m-%dT%H:%M:%SZ")]
            if extra:
                argv += [a.replace("{d}", d) for a in extra]
            return chf.main(argv)

    def test_fresh_is_rc0(self):
        self.assertEqual(self.run_main({"generated_at": generated_at(60)}), chf.RC_FRESH)

    def test_stale_is_rc3(self):
        self.assertEqual(
            self.run_main({"generated_at": "2000-01-01T00:00:00Z"}), chf.RC_STALE
        )

    def test_missing_file_is_rc3(self):
        self.assertEqual(self.run_main({}, write=False), chf.RC_STALE)

    def test_bad_now_is_rc2(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            p.write_text(json.dumps({"generated_at": generated_at(60)}))
            self.assertEqual(chf.main([str(p), "--now", "not-a-time"]), 2)

    def test_negative_max_age_hours_is_rc2(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            p.write_text(json.dumps({"generated_at": generated_at(60)}))
            self.assertEqual(chf.main([str(p), "--max-age-hours", "-1"]), 2)

    def test_body_out_written_on_stale(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "body.md"
            rc = self.run_main(
                {"generated_at": "2000-01-01T00:00:00Z"}, extra=["--body-out", str(out)]
            )
            self.assertEqual(rc, chf.RC_STALE)
            self.assertIn(chf.MARKER, out.read_text())

    def test_fixture_with_explicit_threshold_is_rc3(self):
        # verify コマンドと同じ形 (--now を固定した決定論版)
        rc = chf.main([str(FIXTURE_STALE), "--max-age-hours", "3", "--now", "2026-08-23T16:00:00Z"])
        self.assertEqual(rc, chf.RC_STALE)

    def test_fixture_without_now_is_rc3_for_years(self):
        # 実際の verify は --now を渡さない。fixture の日付が古いので
        # いつ実行されても stale であること
        self.assertEqual(chf.main([str(FIXTURE_STALE), "--max-age-hours", "3"]), chf.RC_STALE)
        with open(FIXTURE_STALE) as f:
            fixture_at = json.load(f)["generated_at"]
        parsed = chf.parse_iso(fixture_at)
        self.assertIsNotNone(parsed)
        self.assertLess(parsed, NOW - datetime.timedelta(days=365))


if __name__ == "__main__":
    unittest.main()
