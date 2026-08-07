import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ops.heart import metrics

RULES = {
    "breaker": {"daily_cost_usd": 1.0},
    "transcripts": {"retention_days": 30, "max_total_gb": 10},
}
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def write_transcript(dir_, name, costs):
    path = Path(dir_) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for c in costs:
            f.write(json.dumps({"type": "assistant", "message": {}}) + "\n")
            f.write(
                json.dumps({"type": "result", "subtype": "success",
                            "total_cost_usd": c}) + "\n"
            )


class TestBreaker(unittest.TestCase):
    def test_under_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            write_transcript(d, "2026-08-07T10-loop.jsonl", [0.3, 0.4])
            tripped, info = metrics.breaker_tripped(None, RULES, Path(d), NOW)
            self.assertFalse(tripped)
            self.assertAlmostEqual(info["cost_usd"], 0.7)
            self.assertEqual(info["sessions"], 2)

    def test_over_threshold_trips(self):
        with tempfile.TemporaryDirectory() as d:
            write_transcript(d, "2026-08-07T10-a.jsonl", [0.8])
            write_transcript(d, "sub/2026-08-07T11-b.jsonl", [0.5])
            tripped, _ = metrics.breaker_tripped(None, RULES, Path(d), NOW)
            self.assertTrue(tripped)

    def test_other_days_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            write_transcript(d, "2026-08-06T10-a.jsonl", [9.9])
            tripped, info = metrics.breaker_tripped(None, RULES, Path(d), NOW)
            self.assertFalse(tripped)
            self.assertEqual(info["sessions"], 0)

    def test_missing_dir_is_zero(self):
        tripped, info = metrics.breaker_tripped(
            None, RULES, Path("/nonexistent-heart-test"), NOW
        )
        self.assertFalse(tripped)
        self.assertEqual(info["cost_usd"], 0.0)


class TestRotate(unittest.TestCase):
    def test_size_cap_deletes_oldest_first(self):
        import os

        rules = {"transcripts": {"retention_days": 30, "max_total_gb": 3 / 1024**3}}
        with tempfile.TemporaryDirectory() as d:
            base = NOW.timestamp()
            for i, name in enumerate(["2026-08-05T00-a.jsonl", "2026-08-06T00-b.jsonl",
                                      "2026-08-07T00-c.jsonl"]):
                p = Path(d) / name
                p.write_bytes(b"xx")  # 2 bytes each, cap = 3 bytes
                # retention (30日) 内に収まる範囲で古→新の mtime を付ける
                ts = base - (3 - i) * 86400
                os.utime(p, (ts, ts))
            removed = metrics.rotate_transcripts(Path(d), rules, NOW)
            self.assertEqual(removed, 2)  # 古い 2 つが消え、最新だけ残る
            self.assertTrue((Path(d) / "2026-08-07T00-c.jsonl").exists())

    def test_retention_deletes_old_files(self):
        import os

        rules = {"transcripts": {"retention_days": 1, "max_total_gb": 10}}
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "2026-07-01T00-old.jsonl"
            old.write_bytes(b"x")
            os.utime(old, (0, 0))
            fresh = Path(d) / "2026-08-07T00-new.jsonl"
            fresh.write_bytes(b"x")
            removed = metrics.rotate_transcripts(Path(d), rules, NOW)
            self.assertEqual(removed, 1)
            self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
