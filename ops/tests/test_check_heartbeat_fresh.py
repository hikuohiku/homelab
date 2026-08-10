"""ops/check_heartbeat_fresh.py の純粋関数を固定する。

ops/heart/tests/ に置かないのは、あちらが CODEOWNERS 保護パスで、非保護スクリプトの
テストを直すだけで人間レビューが要るようになるのを避けるため (P-0027)。
リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`。
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from ops import check_heartbeat_fresh as chf
from ops.heart import triage

REPO = Path(__file__).resolve().parents[2]
with open(REPO / "ops" / "rules.json") as f:
    RULES = json.load(f)

NOW = datetime.datetime(2026, 8, 9, 12, 0, 0, tzinfo=datetime.timezone.utc)
STALE_SECONDS = 7200


def at(delta_seconds):
    """NOW から delta_seconds 前の時刻を heart と同じ書式で。"""
    t = NOW - datetime.timedelta(seconds=delta_seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def comment(body, created_delta_seconds):
    return {"body": body, "created_at": at(created_delta_seconds)}


class TestJudge(unittest.TestCase):
    def test_fresh(self):
        v = chf.judge({"beat": 1, "at": at(60), "writer": "heart"}, NOW, STALE_SECONDS)
        self.assertFalse(v["stale"])
        self.assertEqual(v["age_seconds"], 60)

    def test_exactly_stale_seconds_is_stale(self):
        # 境界は stale 側に含める (>=)。閾値ちょうどで「まだ元気」に倒さない
        v = chf.judge({"at": at(STALE_SECONDS)}, NOW, STALE_SECONDS)
        self.assertTrue(v["stale"])

    def test_just_under_threshold_is_fresh(self):
        v = chf.judge({"at": at(STALE_SECONDS - 1)}, NOW, STALE_SECONDS)
        self.assertFalse(v["stale"])

    def test_far_past_is_stale(self):
        v = chf.judge({"at": "2000-01-01T00:00:00Z"}, NOW, STALE_SECONDS)
        self.assertTrue(v["stale"])
        self.assertGreater(v["age_seconds"], STALE_SECONDS)

    def test_future_at_is_not_stale_but_reported(self):
        v = chf.judge({"at": at(-300)}, NOW, STALE_SECONDS)
        self.assertFalse(v["stale"])
        self.assertIn("skew", v["reason"])

    def test_missing_at_is_stale(self):
        v = chf.judge({"beat": 1, "writer": "heart"}, NOW, STALE_SECONDS)
        self.assertTrue(v["stale"])
        self.assertIsNone(v["age_seconds"])

    def test_unparsable_at_is_stale(self):
        v = chf.judge({"at": "yesterday"}, NOW, STALE_SECONDS)
        self.assertTrue(v["stale"])

    def test_load_error_is_stale(self):
        v = chf.judge(None, NOW, STALE_SECONDS, load_error="読めない")
        self.assertTrue(v["stale"])


class TestReadHeartbeat(unittest.TestCase):
    """fail-closed の入口。ファイル系の異常は judge に「stale」として渡ること。"""

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            doc, err = chf.read_heartbeat(Path(d) / "nope.json")
        self.assertIsNone(doc)
        self.assertTrue(chf.judge(doc, NOW, STALE_SECONDS, err)["stale"])

    def test_broken_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "heartbeat.json"
            p.write_text("{ not json")
            doc, err = chf.read_heartbeat(p)
        self.assertIsNone(doc)
        self.assertTrue(chf.judge(doc, NOW, STALE_SECONDS, err)["stale"])

    def test_non_object_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "heartbeat.json"
            p.write_text("[]")
            doc, err = chf.read_heartbeat(p)
        self.assertIsNone(doc)
        self.assertTrue(chf.judge(doc, NOW, STALE_SECONDS, err)["stale"])

    def test_reads_real_document(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "heartbeat.json"
            p.write_text(json.dumps({"beat": 3, "at": at(10), "writer": "heart"}))
            doc, err = chf.read_heartbeat(p)
        self.assertIsNone(err)
        self.assertFalse(chf.judge(doc, NOW, STALE_SECONDS)["stale"])


class TestLoadStaleSeconds(unittest.TestCase):
    def test_reads_rules_json(self):
        # 閾値を決め打ちせず rules.json (単一情報源) から読んでいること
        self.assertEqual(chf.load_stale_seconds(), RULES["heartbeat"]["stale_seconds"])
        self.assertEqual(chf.load_stale_seconds(), 7200)


class TestShouldPost(unittest.TestCase):
    COOLDOWN = chf.REPOST_COOLDOWN_SECONDS

    def test_no_comments(self):
        self.assertTrue(chf.should_post([], NOW, self.COOLDOWN, chf.MARKER)["post"])

    def test_recent_marker_suppresses(self):
        c = [comment("前回の検知\n" + chf.MARKER, 3600)]
        v = chf.should_post(c, NOW, self.COOLDOWN, chf.MARKER)
        self.assertFalse(v["post"])
        self.assertEqual(v["last_at"], at(3600))

    def test_old_marker_does_not_suppress(self):
        c = [comment("前回の検知\n" + chf.MARKER, self.COOLDOWN + 60)]
        self.assertTrue(chf.should_post(c, NOW, self.COOLDOWN, chf.MARKER)["post"])

    def test_human_comments_do_not_suppress(self):
        c = [comment("ダッシュボードの配色が見づらい", 60)]
        self.assertTrue(chf.should_post(c, NOW, self.COOLDOWN, chf.MARKER)["post"])

    def test_drill_marker_does_not_suppress_production(self):
        """訓練が本番を黙らせない。MARKER は DRILL_MARKER の部分文字列ではない。"""
        self.assertNotIn(chf.MARKER, chf.DRILL_MARKER)
        c = [comment("[訓練] 疎通確認\n" + chf.DRILL_MARKER, 60)]
        self.assertTrue(chf.should_post(c, NOW, self.COOLDOWN, chf.MARKER)["post"])

    def test_production_marker_does_not_suppress_drill(self):
        c = [comment("検知\n" + chf.MARKER, 60)]
        self.assertTrue(chf.should_post(c, NOW, self.COOLDOWN, chf.DRILL_MARKER)["post"])

    def test_latest_marker_wins(self):
        c = [
            comment(chf.MARKER, self.COOLDOWN + 600),
            comment(chf.MARKER, 120),
        ]
        self.assertFalse(chf.should_post(c, NOW, self.COOLDOWN, chf.MARKER)["post"])

    def test_short_lookback_falls_back_to_posting(self):
        """取得窓が cooldown より短いと抑止の可否を判断できない。沈黙より重複を選ぶ。"""
        v = chf.should_post([], NOW, self.COOLDOWN, chf.MARKER, comments_since=at(600))
        self.assertTrue(v["post"])
        self.assertIn("判断できない", v["reason"])

    def test_short_lookback_still_suppresses_when_evidence_found(self):
        """窓が短くても、窓内に自分のコメントが見つかったなら抑止してよい
        (見つかった事実は lookback の長さに依存しない)。"""
        c = [comment(chf.MARKER, 300)]
        v = chf.should_post(c, NOW, self.COOLDOWN, chf.MARKER, comments_since=at(600))
        self.assertFalse(v["post"])

    def test_long_lookback_allows_suppression_judgement(self):
        v = chf.should_post([], NOW, self.COOLDOWN, chf.MARKER, comments_since=at(48 * 3600))
        self.assertTrue(v["post"])
        self.assertNotIn("判断できない", v["reason"])

    def test_workflow_lookback_is_longer_than_cooldown(self):
        # watchdog.yml が使う lookback (48h)。cooldown より短いと抑止が常に不能になる
        self.assertGreater(48 * 3600, chf.REPOST_COOLDOWN_SECONDS)

    def test_unparsable_created_at_is_ignored(self):
        c = [comment(chf.MARKER, 60)]
        c[0]["created_at"] = "???"
        self.assertTrue(chf.should_post(c, NOW, self.COOLDOWN, chf.MARKER)["post"])


class TestBuildBody(unittest.TestCase):
    def body(self, **kw):
        j = chf.judge({"at": "2000-01-01T00:00:00Z"}, NOW, STALE_SECONDS)
        return chf.build_body(j, kw.pop("marker", chf.MARKER), NOW, STALE_SECONDS, **kw)

    def test_not_eaten_by_triage(self):
        """最大の罠: 投稿した本文は次のビートで triage.classify に食われる。
        全停止 (stop_all) や veto に倒れたら、watchdog が器を止めてしまう。"""
        for kw in ({}, {"drill": True}, {"run_url": "https://example.invalid/run/1"}):
            body = self.body(**kw)
            self.assertEqual(triage.classify(body, RULES)["kind"], "review_needed", kw)

    def test_body_is_long_enough(self):
        # 50 文字以下だと triage が部分一致で停止キーワードを拾う
        self.assertGreater(len(self.body()), 50)

    def test_no_stop_keyword_at_line_start(self):
        for line in self.body().splitlines():
            for kw in RULES["veto"]["stop_keywords"]:
                self.assertFalse(line.strip().lower().startswith(kw.lower()), (line, kw))

    def test_marker_is_present(self):
        self.assertIn(chf.MARKER, self.body())
        self.assertNotIn(chf.MARKER, self.body(marker=chf.DRILL_MARKER, drill=True))
        self.assertIn(chf.DRILL_MARKER, self.body(marker=chf.DRILL_MARKER, drill=True))

    def test_drill_body_says_it_is_a_drill(self):
        self.assertIn("訓練", self.body(marker=chf.DRILL_MARKER, drill=True))

    def test_body_survives_missing_age(self):
        j = chf.judge(None, NOW, STALE_SECONDS, load_error="読めない")
        body = chf.build_body(j, chf.MARKER, NOW, STALE_SECONDS)
        self.assertIn(chf.MARKER, body)
        self.assertEqual(triage.classify(body, RULES)["kind"], "review_needed")


class TestMainExitCodes(unittest.TestCase):
    """workflow が rc で分岐するので、rc は契約そのもの。"""

    def run_main(self, doc, extra=None, write=True):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "heartbeat.json"
            if write:
                p.write_text(json.dumps(doc))
            argv = ["--file", str(p), "--now", NOW.strftime("%Y-%m-%dT%H:%M:%SZ")]
            if extra:
                argv += [a.replace("{d}", d) for a in extra]
            return chf.main(argv)

    def test_fresh_is_rc0(self):
        self.assertEqual(self.run_main({"at": at(60)}), chf.RC_FRESH)

    def test_stale_is_rc1(self):
        self.assertEqual(self.run_main({"at": "2000-01-01T00:00:00Z"}), chf.RC_STALE_NOTIFY)

    def test_missing_file_is_rc1(self):
        self.assertEqual(self.run_main({}, write=False), chf.RC_STALE_NOTIFY)

    def test_suppressed_is_rc3(self):
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "comments.json"
            cf.write_text(json.dumps([comment(chf.MARKER, 60)]))
            rc = self.run_main({"at": "2000-01-01T00:00:00Z"}, extra=["--comments-file", str(cf)])
        self.assertEqual(rc, chf.RC_STALE_SUPPRESSED)

    def test_unreadable_comments_falls_back_to_posting(self):
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "comments.json"
            cf.write_text("{ broken")
            rc = self.run_main({"at": "2000-01-01T00:00:00Z"}, extra=["--comments-file", str(cf)])
        self.assertEqual(rc, chf.RC_STALE_NOTIFY)

    def test_bad_now_is_rc2(self):
        self.assertEqual(self.run_main({"at": at(60)}, extra=None) or 0, chf.RC_FRESH)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "heartbeat.json"
            p.write_text(json.dumps({"at": at(60)}))
            self.assertEqual(chf.main(["--file", str(p), "--now", "not-a-time"]), 2)

    def test_body_out_written_on_stale(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "body.md"
            rc = self.run_main({"at": "2000-01-01T00:00:00Z"}, extra=["--body-out", str(out)])
            self.assertEqual(rc, chf.RC_STALE_NOTIFY)
            self.assertIn(chf.MARKER, out.read_text())


if __name__ == "__main__":
    unittest.main()
