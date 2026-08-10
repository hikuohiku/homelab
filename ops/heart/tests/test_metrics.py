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


def beat(minutes_ago, states=None, actions=None, beat_no=1):
    """NOW から minutes_ago 分前のビート 1 行 (metrics.jsonl の実際の書式)。"""
    from datetime import timedelta

    at = NOW - timedelta(minutes=minutes_ago)
    return {
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "beat": beat_no,
        "projects": states or {},
        "actions": actions or [],
    }


class TestSummarizeBeats(unittest.TestCase):
    def test_empty_window(self):
        s = metrics.summarize_beats([], NOW)
        self.assertEqual(s["beats"], 0)
        self.assertEqual(s["idle_ratio"], 0.0)
        self.assertEqual(s["state_seconds"], {})

    def test_dwell_uses_at_deltas_not_beat_counts(self):
        """ビート間隔が不定でも、滞留は at の差分で数える。
        「ビート数 × 固定間隔」で換算すると静かに嘘になる。"""
        records = [
            beat(10, {"P-1": "announced"}),   # 次まで 7 分
            beat(3, {"P-1": "active"}),       # 次 (= now) まで 3 分
        ]
        s = metrics.summarize_beats(records, NOW)
        self.assertEqual(s["beats"], 2)
        self.assertEqual(s["state_seconds"]["announced"], 420)
        self.assertEqual(s["state_seconds"]["active"], 180)
        self.assertEqual(s["elapsed_seconds"], 600)

    def test_beat_number_reset_does_not_confuse_ordering(self):
        """heart 再起動で beat がリセットされても (2 → 1)、時刻順に数える。"""
        records = [
            beat(9, {"P-1": "active"}, beat_no=7),
            beat(6, {"P-1": "active"}, beat_no=1),  # 再起動で 1 に戻った
            beat(3, {"P-1": "merging"}, beat_no=2),
        ]
        s = metrics.summarize_beats(records, NOW)
        self.assertEqual(s["state_seconds"]["active"], 360)
        self.assertEqual(s["state_seconds"]["merging"], 180)

    def test_records_out_of_order_are_sorted(self):
        records = [beat(3, {"P-1": "merging"}), beat(9, {"P-1": "active"})]
        s = metrics.summarize_beats(records, NOW)
        self.assertEqual(s["state_seconds"]["active"], 360)

    def test_idle_ratio_and_action_counts(self):
        records = [
            beat(4, actions=["spawn_runner", "announce"]),
            beat(3),
            beat(2),
            beat(1, actions=["merge_pr"]),
        ]
        s = metrics.summarize_beats(records, NOW)
        self.assertEqual(s["idle_beats"], 2)
        self.assertEqual(s["idle_ratio"], 0.5)
        self.assertEqual(s["busy_seconds"], 120)  # 4 分前と 1 分前のビート
        self.assertEqual(s["actions"]["spawn_runner"], 1)
        self.assertEqual(s["actions"]["merge_pr"], 1)

    def test_downtime_is_split_out_of_dwell(self):
        """heart が止まっていた区間を滞留に混ぜない
        (「詰まっていた」と「動いていなかった」は別の所見になる)。"""
        records = [beat(200, {"P-1": "active"}), beat(5, {"P-1": "active"})]
        s = metrics.summarize_beats(records, NOW)
        # 1 本目は 195 分の空きだが 600s しか帰属させない。残りは downtime
        self.assertEqual(s["state_seconds"]["active"], 600 + 300)
        self.assertEqual(s["downtime_seconds"], 195 * 60 - 600)

    def test_terminal_states_are_snapshot_not_dwell(self):
        """終端は projects.json に残り続けるので、延べ秒を数えると全部を覆い隠す。"""
        states = {"P-1": "delivered", "P-2": "stalled", "P-3": "active"}
        s = metrics.summarize_beats([beat(5, states)], NOW)
        self.assertNotIn("delivered", s["state_seconds"])
        self.assertNotIn("stalled", s["state_seconds"])
        self.assertEqual(s["state_seconds"]["active"], 300)
        self.assertEqual(s["terminal_now"]["delivered"], 1)
        self.assertEqual(s["terminal_now"]["stalled"], 1)

    def test_window_blocked_is_wall_clock_not_per_project(self):
        """窓待ちは壁時計。2 件同時に待っていても実時間は 2 倍にならない。"""
        s = metrics.summarize_beats(
            [beat(5, {"P-1": "announced", "P-2": "announced"})], NOW
        )
        self.assertEqual(s["window_blocked_seconds"], 300)
        self.assertEqual(s["state_seconds"]["announced"], 600)  # 延べは 2 件分

    def test_outside_window_and_broken_lines_are_skipped(self):
        records = [
            beat(60 * 30, {"P-1": "active"}),   # 30h 前 = 窓の外
            {"beat": 1, "projects": {}},        # at が無い壊れた行
            {"at": "not-a-time"},
            beat(5, {"P-1": "active"}),
        ]
        s = metrics.summarize_beats(records, NOW)
        self.assertEqual(s["beats"], 1)
        self.assertEqual(s["state_seconds"]["active"], 300)

    def test_per_project_dwell(self):
        records = [beat(10, {"P-1": "merging", "P-2": "active"}), beat(5, {"P-1": "merging"})]
        s = metrics.summarize_beats(records, NOW)
        self.assertEqual(s["project_seconds"]["P-1"]["merging"], 600)
        self.assertEqual(s["project_seconds"]["P-2"]["active"], 300)


class TestSummarizeStalled(unittest.TestCase):
    def test_counts_reasons(self):
        doc = {"projects": [
            {"id": "P-1", "state": "stalled", "stalled_reason": "review_rejected"},
            {"id": "P-2", "state": "stalled", "stalled_reason": "review_rejected"},
            {"id": "P-3", "state": "stalled"},           # reason 未設定
            {"id": "P-4", "state": "delivered"},
        ]}
        self.assertEqual(
            metrics.summarize_stalled(doc),
            {"review_rejected": 2, "unknown": 1},
        )

    def test_empty(self):
        self.assertEqual(metrics.summarize_stalled(None), {})


if __name__ == "__main__":
    unittest.main()
