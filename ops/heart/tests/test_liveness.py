"""heart の心拍鮮度判定 (ops/heart/liveness.py) のテスト。

kubelet の exec probe が呼ぶ check() の分岐 (fresh / stale / heartbeat.json が無い /
壊れている) を仕様として固定する。ファイル名に "liveness" を含めることで
`pytest -k liveness` にも `unittest discover` にもヒットする。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ops.heart import liveness

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestMaxAgeSeconds(unittest.TestCase):
    def test_scales_with_beat_seconds(self):
        self.assertEqual(liveness.max_age_seconds(60), 60 * 5 + 60)
        self.assertEqual(liveness.max_age_seconds(120), 120 * 5 + 60)


class TestIsStale(unittest.TestCase):
    def test_within_max_age_is_fresh(self):
        at = iso(NOW - timedelta(seconds=100))
        self.assertFalse(liveness.is_stale(at, NOW, max_age=360))

    def test_beyond_max_age_is_stale(self):
        at = iso(NOW - timedelta(seconds=361))
        self.assertTrue(liveness.is_stale(at, NOW, max_age=360))

    def test_exactly_at_boundary_is_not_stale(self):
        at = iso(NOW - timedelta(seconds=360))
        self.assertFalse(liveness.is_stale(at, NOW, max_age=360))


class TestCheck(unittest.TestCase):
    def _write_heartbeat(self, data_dir, at, beat=1):
        state_dir = Path(data_dir) / "ops-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "heartbeat.json", "w") as f:
            json.dump({"beat": beat, "at": at, "writer": "heart"}, f)

    def test_fresh_heartbeat_is_liveness_ok(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_heartbeat(d, iso(NOW - timedelta(seconds=30)))
            self.assertTrue(liveness.check(data_dir=d, beat_seconds=60, now=NOW))

    def test_stale_heartbeat_fails_liveness(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_heartbeat(d, iso(NOW - timedelta(hours=1)))
            self.assertFalse(liveness.check(data_dir=d, beat_seconds=60, now=NOW))

    def test_missing_heartbeat_fails_liveness(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(liveness.check(data_dir=d, beat_seconds=60, now=NOW))

    def test_corrupt_heartbeat_fails_liveness(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d) / "ops-state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "heartbeat.json").write_text("{not json")
            self.assertFalse(liveness.check(data_dir=d, beat_seconds=60, now=NOW))

    def test_heartbeat_missing_at_field_fails_liveness(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d) / "ops-state"
            state_dir.mkdir(parents=True, exist_ok=True)
            with open(state_dir / "heartbeat.json", "w") as f:
                json.dump({"beat": 1}, f)
            self.assertFalse(liveness.check(data_dir=d, beat_seconds=60, now=NOW))


if __name__ == "__main__":
    unittest.main()
