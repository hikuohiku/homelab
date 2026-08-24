"""指標を git から出す (設計 state-out-of-git Phase 1) の不変条件。

守りたいのは 2 つ。
  1. **1 行に終端プロジェクトを載せない。** ここが緩むと 1 行 8 KB に戻る。
  2. **集計の答えが変わらない。** 行を細くしたせいで summarize_beats の出力が
     ずれたら、critic に渡す材料が静かに嘘になる。
"""

import unittest
from datetime import datetime, timedelta, timezone

from .. import metrics
from ..statefiles import now_iso

T0 = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def doc(states):
    return {"projects": [{"id": pid, "state": st} for pid, st in states.items()]}


class BeatRecordTest(unittest.TestCase):
    def test_terminal_projects_are_folded_into_counts(self):
        rec = metrics.beat_record(
            T0, 7, doc({"P-1": "active", "P-2": "delivered", "P-3": "stalled",
                        "P-4": "stalled", "P-5": "vetoed"})
        )
        self.assertEqual(rec["projects"], {"P-1": "active"})
        self.assertEqual(
            rec["terminal_counts"], {"delivered": 1, "stalled": 2, "vetoed": 1}
        )

    def test_extra_fields_pass_through(self):
        rec = metrics.beat_record(T0, 1, doc({}), jobs=3, actions=["spawn"])
        self.assertEqual(rec["jobs"], 3)
        self.assertEqual(rec["actions"], ["spawn"])
        self.assertEqual(rec["beat"], 1)
        self.assertEqual(rec["at"], now_iso(T0))

    def test_line_is_small(self):
        """終端 100 件 + 非終端 5 件で 1 KB を超えないこと (旧形式は約 8 KB)。"""
        import json

        states = {f"P-{i:04d}": "delivered" for i in range(100)}
        states.update({f"P-9{i:03d}": "active" for i in range(5)})
        line = json.dumps(metrics.beat_record(T0, 1, doc(states)))
        self.assertLess(len(line), 1024, f"1 行が {len(line)} B に膨らんでいる")


class TerminalNowCompatTest(unittest.TestCase):
    """保持窓の入れ替わり中は新旧の行が混ざる。両方から同じ答えが出ること。"""

    def _summary(self, last_rec):
        return metrics.summarize_beats([last_rec], T0 + timedelta(minutes=1))

    def _expected(self, **counts):
        """終端の内訳は TERMINAL_STATES の全項目を持つ。

        rejected (4b-1) は projects.json に載らない state なので常に 0 だが、
        欄自体は他の終端と同じように出る。
        """
        from ops.heart.statefiles import TERMINAL_STATES

        return {s: counts.get(s, 0) for s in TERMINAL_STATES}

    def test_new_record_uses_terminal_counts(self):
        rec = metrics.beat_record(T0, 1, doc({"P-1": "delivered", "P-2": "active"}))
        self.assertEqual(self._summary(rec)["terminal_now"], self._expected(delivered=1))

    def test_old_record_counts_from_projects(self):
        old = {
            "at": now_iso(T0),
            "beat": 1,
            "projects": {"P-1": "delivered", "P-2": "active"},
        }
        self.assertEqual(self._summary(old)["terminal_now"], self._expected(delivered=1))

    def test_both_forms_agree(self):
        states = {"P-1": "delivered", "P-2": "stalled", "P-3": "active",
                  "P-4": "in_review", "P-5": "vetoed"}
        new = metrics.beat_record(T0, 1, doc(states))
        old = {"at": now_iso(T0), "beat": 1, "projects": dict(states)}
        a = self._summary(new)
        b = self._summary(old)
        for key in ("terminal_now", "state_seconds", "project_seconds",
                    "working_seconds", "waiting_only_seconds", "empty_seconds"):
            self.assertEqual(a[key], b[key], f"{key} が新旧で食い違う")


class PruneBeatsTest(unittest.TestCase):
    def test_keeps_only_the_window(self):
        recs = [
            {"at": now_iso(T0 - timedelta(hours=h)), "beat": h} for h in (0, 1, 47, 49)
        ]
        kept = metrics.prune_beats(recs, T0, keep_hours=48)
        self.assertEqual([r["beat"] for r in kept], [0, 1, 47])

    def test_unreadable_at_is_dropped(self):
        recs = [{"beat": 1}, {"at": "壊れている", "beat": 2},
                {"at": now_iso(T0), "beat": 3}]
        self.assertEqual([r["beat"] for r in metrics.prune_beats(recs, T0)], [3])


if __name__ == "__main__":
    unittest.main()
