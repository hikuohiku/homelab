import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ops.heart import metrics

RULES = {
    "transcripts": {"retention_days": 30, "max_total_gb": 10},
}
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def claude_result(cost, tokens_in=0, tokens_out=0):
    """claude CLI (stream-json) の 1 セッション終端イベント。"""
    return {"type": "result", "subtype": "success", "total_cost_usd": cost,
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}


def opencode_step_finish(cost, tokens_in, tokens_out):
    """opencode v1.18.21 実測形 (ops/runner/tests/test_engine.py と同じ形)。"""
    return {"type": "step_finish", "part": {
        "type": "step-finish", "cost": cost,
        "tokens": {"total": tokens_in + tokens_out, "input": tokens_in,
                   "output": tokens_out, "reasoning": 0,
                   "cache": {"write": 0, "read": 0}}}}


def write_events(dir_, name, events):
    """1 transcript = 1 セッション。events をそのまま 1 行 1 JSON で書く。

    ファイル名は live の実物と同じ `<YYYY-MM-DD>T<HHMMSS>-...jsonl`
    (runner.transcript_path / loop.sh の命名)。"""
    path = Path(dir_) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return path


class TestDailyUsage(unittest.TestCase):
    """当日の消費量は**計測するだけ**。閾値も判定も持たない
    (2026-08-24 にサーキットブレーカーを廃止)。"""

    def test_opencode_only_transcript_is_counted(self):
        """回帰 (2026-08-24): 全ロールが opencode に移った後、`result` しか見て
        いなかったせいで当日消費が常に 0 を返していた。step_finish を数えること。"""
        with tempfile.TemporaryDirectory() as d:
            write_events(d, "curriculum/2026-08-07T101010-p-0001-gen.jsonl", [
                {"type": "step_start", "part": {"type": "step-start"}},
                opencode_step_finish(0.0, 8718, 19),
                opencode_step_finish(0.0, 100, 5),
            ])
            info = metrics.daily_usage(Path(d), NOW)
            self.assertEqual(info["tokens"], 8842)
            self.assertEqual(info["sessions"], 1)

    def test_claude_only_transcript_is_counted(self):
        with tempfile.TemporaryDirectory() as d:
            write_events(d, "loop/2026-08-07T101010-i3.jsonl", [
                {"type": "assistant", "message": {}},
                claude_result(0.7, 1000, 250),
            ])
            info = metrics.daily_usage(Path(d), NOW)
            self.assertAlmostEqual(info["cost_usd"], 0.7)
            self.assertEqual(info["tokens"], 1250)
            self.assertEqual(info["sessions"], 1)

    def test_both_engines_are_summed(self):
        with tempfile.TemporaryDirectory() as d:
            write_events(d, "worker/2026-08-07T101010-p-0278-s0-init.jsonl", [claude_result(0.5, 10, 5)])
            write_events(d, "curriculum/2026-08-07T111010-system-gen.jsonl",
                         [opencode_step_finish(0.25, 200, 50)])
            info = metrics.daily_usage(Path(d), NOW)
            self.assertAlmostEqual(info["cost_usd"], 0.75)
            self.assertEqual(info["tokens"], 265)
            self.assertEqual(info["sessions"], 2)
            self.assertEqual(sorted(info), ["cost_usd", "day", "empty_sessions",
                                            "sessions", "tokens"])

    def test_no_amount_is_a_verdict(self):
        # いくら積み上がっても返るのは数字だけ。「止める」を意味する値は無い
        with tempfile.TemporaryDirectory() as d:
            write_events(d, "worker/2026-08-07T101010-p-0278-s0-init.jsonl", [claude_result(500.0)])
            write_events(d, "worker/2026-08-07T111010-p-0278-s1-work.jsonl", [claude_result(500.0)])
            info = metrics.daily_usage(Path(d), NOW)
            self.assertAlmostEqual(info["cost_usd"], 1000.0)

    def test_other_days_ignored(self):
        """当日でない日付プレフィクスのファイルは数えない。"""
        with tempfile.TemporaryDirectory() as d:
            write_events(d, "worker/2026-08-06T235959-p-0001-s0-init.jsonl",
                         [claude_result(9.9, 100, 100)])
            write_events(d, "curriculum/2026-08-08T000001-system-gen.jsonl",
                         [opencode_step_finish(0.0, 100, 100)])
            info = metrics.daily_usage(Path(d), NOW)
            self.assertEqual(info["cost_usd"], 0.0)
            self.assertEqual(info["tokens"], 0)
            self.assertEqual(info["sessions"], 0)

    def test_empty_sessions_counted_separately(self):
        """出力ゼロで死んだセッション (2026-08-23 は 88 本中 82 本) を、走った本数と
        取り違えない。"""
        with tempfile.TemporaryDirectory() as d:
            write_events(d, "worker/2026-08-07T070445-p-0278-s0-init.jsonl", [
                {"type": "step_start", "part": {"type": "step-start"}},
                {"type": "error", "error": {"name": "APIError", "data": {
                    "message": "Provider finish_reason: network_error"}}},
            ])
            write_events(d, "worker/2026-08-07T071000-p-0278-s1-work.jsonl",
                         [opencode_step_finish(0.0, 100, 10)])
            info = metrics.daily_usage(Path(d), NOW)
            self.assertEqual(info["sessions"], 2)
            self.assertEqual(info["empty_sessions"], 1)
            self.assertEqual(info["tokens"], 110)

    def test_broken_lines_do_not_break_the_tally(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_events(d, "worker/2026-08-07T101010-p-0278-s0-init.jsonl",
                                [opencode_step_finish(0.0, 100, 10)])
            with open(path, "a") as f:
                f.write('{"type": "step_finish", "part": {"tok\n')  # 途中で切れた行
                f.write('{"type": "result", "total_cost_usd": "?"}\n')  # 型が違う
                f.write("\x00\x00 not json at all\n")
                f.write(json.dumps(claude_result(0.25, 4, 1)) + "\n")
            info = metrics.daily_usage(Path(d), NOW)
            self.assertAlmostEqual(info["cost_usd"], 0.25)
            self.assertEqual(info["tokens"], 115)
            self.assertEqual(info["sessions"], 1)

    def test_unreadable_file_does_not_break_the_tally(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            bad = write_events(d, "worker/2026-08-07T090000-p-0278-bad.jsonl",
                               [claude_result(1.0, 1, 1)])
            os.chmod(bad, 0o000)
            write_events(d, "worker/2026-08-07T100000-p-0278-ok.jsonl",
                         [opencode_step_finish(0.0, 50, 5)])
            try:
                info = metrics.daily_usage(Path(d), NOW)
            finally:
                os.chmod(bad, 0o600)
            self.assertEqual(info["tokens"], 55)

    def test_missing_dir_is_zero(self):
        info = metrics.daily_usage(Path("/nonexistent-heart-test"), NOW)
        self.assertEqual(info["cost_usd"], 0.0)
        self.assertEqual(info["tokens"], 0)
        self.assertEqual(info["empty_sessions"], 0)


class TestEngineInterpretationMatchesRunner(unittest.TestCase):
    """heart (metrics) と runner (consume_stream_event) の使用量解釈を一致させる。

    heart は常駐プロセス、runner は Job 側の wrapper なので import で結ばない。
    代わりに、同じイベントを両方に食わせて同じ数字になることをここで固定する。
    片方だけ直したら落ちる。
    """

    CASES = [
        {"type": "result", "total_cost_usd": 0.5,
         "usage": {"input_tokens": 100, "output_tokens": 20}},
        {"type": "result", "is_error": True, "subtype": "error_during_execution",
         "result": "usage limit reached"},
        opencode_step_finish(0.0, 8718, 19),
        opencode_step_finish(0.125, 1, 2),
        {"type": "error", "error": {"name": "APIError",
         "data": {"message": "Provider finish_reason: network_error"}}},
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "text", "part": {"text": "hi"}},
    ]

    def test_same_cost_and_tokens_as_runner(self):
        from ops.runner.runner import consume_stream_event

        for ev in self.CASES:
            with self.subTest(ev=ev.get("type")):
                usage = {"tokens": 0, "cost": 0.0}
                consume_stream_event(ev, usage, [])
                cost, tokens = metrics.transcript_usage_from_event(ev)
                self.assertAlmostEqual(cost, usage["cost"])
                self.assertEqual(tokens, usage["tokens"])


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

    def test_wall_clock_split_is_exhaustive_and_exclusive(self):
        """working / waiting_only / empty の 3 分割は排他で、合計は elapsed に一致する。"""
        records = [
            beat(20, {"P-1": "active", "P-2": "announced"}),  # 仕事あり (窓待ちと同時)
            beat(15, {"P-1": "announced"}),                   # 着手待ちだけ
            beat(10, {"P-1": "delivered"}),                   # 終端だけ = 空
            beat(5, {}),                                      # 何も無い
        ]
        s = metrics.summarize_beats(records, NOW)
        self.assertEqual(s["working_seconds"], 300)
        self.assertEqual(s["waiting_only_seconds"], 300)
        self.assertEqual(s["empty_seconds"], 600)
        self.assertEqual(
            s["working_seconds"] + s["waiting_only_seconds"] + s["empty_seconds"],
            s["elapsed_seconds"],
        )

    def test_idle_beats_are_not_idle_time(self):
        """action の無いビートでも、別 Job で仕事は進んでいる。
        idle_ratio を「空費」と読むと逆の結論になる (2026-08-10 の実測で判明:
        idle_ratio 0.968 に対し実際に仕事が走っていたのは 86.8%)。"""
        records = [beat(10, {"P-1": "active"}), beat(5, {"P-1": "active"})]
        s = metrics.summarize_beats(records, NOW)
        self.assertEqual(s["idle_ratio"], 1.0)      # heart は何もしていない
        self.assertEqual(s["busy_seconds"], 0)
        self.assertEqual(s["working_seconds"], 600)  # が、仕事は走っている

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
