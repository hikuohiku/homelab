"""タスク依頼キュー (P-0091) の遷移表テスト。

このテーブルが仕様。受領 (merge_new) → 原料渡し (for_env) → 処理済み化
(mark_processed / done_ids) の純関数だけを固定する。I/O は heart.py の仕事。
"""

import json
import unittest
from datetime import datetime, timezone

from ops.heart import tasks

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


def rec(rid, status="pending", **kw):
    base = {
        "id": rid,
        "source": f"src-{rid}",
        "body": f"body {rid}",
        "received_at": "2026-08-22T00:00:00Z",
        "status": status,
    }
    base.update(kw)
    return base


class TestMakeId(unittest.TestCase):
    def test_deterministic_from_source(self):
        """同じ source なら必ず同じ id — 処理済み化の対応づけはここに乗る。"""
        self.assertEqual(tasks.make_id("ops/feedback/inbox/a.json"),
                         tasks.make_id("ops/feedback/inbox/a.json"))

    def test_distinct_per_source(self):
        self.assertNotEqual(tasks.make_id("a"), tasks.make_id("b"))

    def test_hex_and_short(self):
        rid = tasks.make_id("x")
        self.assertEqual(len(rid), 16)
        int(rid, 16)  # 16 進として解釈できる


class TestMergeNew(unittest.TestCase):
    def test_appends_unknown_ids_in_order(self):
        out = tasks.merge_new([], [{"source": "s1", "body": "b1"}], now=NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "s1")
        self.assertEqual(out[0]["status"], "pending")
        self.assertEqual(out[0]["received_at"], "2026-08-22T12:00:00Z")

    def test_duplicate_id_is_ignored(self):
        """カーソル巻き戻り等で同じ note を再取り込みしても積み直さない。"""
        first = tasks.merge_new([], [{"source": "s1", "body": "b1"}])
        second = tasks.merge_new(first, [{"source": "s1", "body": "b1 again"}])
        self.assertEqual(len(second), 1)

    def test_does_not_mutate_input(self):
        original = []
        out = tasks.merge_new(original, [{"source": "s", "body": "b"}])
        self.assertEqual(original, [])
        self.assertEqual(len(out), 1)


class TestPendingAndForEnv(unittest.TestCase):
    def test_pending_filters_and_keeps_fifo_order(self):
        records = [rec("old"), rec("done-one", status="processed"), rec("new")]
        self.assertEqual(
            [r["id"] for r in tasks.pending(records)], ["old", "new"]
        )

    def test_for_env_is_valid_json_of_pending_only(self):
        records = [rec("r1"), rec("r2", status="processed")]
        doc = json.loads(tasks.for_env(records))
        self.assertEqual([r["id"] for r in doc], ["r1"])

    def test_for_env_caps_count_oldest_first(self):
        records = [rec(f"r{i:02d}") for i in range(30)]
        doc = json.loads(tasks.for_env(records, max_requests=5))
        self.assertEqual([r["id"] for r in doc], ["r00", "r01", "r02", "r03", "r04"])

    def test_for_env_truncates_long_bodies(self):
        records = [rec("big", body="あ" * 5000)]
        doc = json.loads(tasks.for_env(records, max_body_chars=100))
        self.assertEqual(doc[0]["body"], "あ" * 100)

    def test_for_env_empty_is_empty_array_string(self):
        self.assertEqual(tasks.for_env([]), "[]")


class TestMarkProcessed(unittest.TestCase):
    def test_flips_matching_pending_only(self):
        records = [rec("a"), rec("b")]
        out = tasks.mark_processed(records, ["a"])
        self.assertEqual(out[0]["status"], "processed")
        self.assertEqual(out[1]["status"], "pending")

    def test_stamps_processed_at(self):
        out = tasks.mark_processed([rec("a")], ["a"], now=NOW)
        self.assertEqual(out[0]["processed_at"], "2026-08-22T12:00:00Z")

    def test_idempotent_keeps_first_processed_at(self):
        """consume と mark の間で落ちて再実行しても、processed_at は最初の刻みのまま。"""
        once = tasks.mark_processed([rec("a")], ["a"],
                                    now=datetime(2026, 8, 22, 13, 0, 0,
                                                 tzinfo=timezone.utc))
        twice = tasks.mark_processed(once, ["a"], now=NOW)
        self.assertEqual(twice, once)
        self.assertEqual(twice[0]["processed_at"], "2026-08-22T13:00:00Z")

    def test_unknown_ids_are_ignored(self):
        """案が存在しない依頼 id を捏造してもキューは壊れない。"""
        records = [rec("a")]
        out = tasks.mark_processed(records, ["nope"])
        self.assertEqual(out[0]["status"], "pending")


class TestDoneIds(unittest.TestCase):
    def test_collects_unique_sorted_request_ids(self):
        specs = [
            {"id": "P-0002", "request_id": "bbb"},
            {"id": "P-0003", "request_id": "aaa"},
            {"id": "P-0004", "request_id": "bbb"},  # 重複
            {"id": "P-0005"},  # 通常の案
        ]
        self.assertEqual(tasks.done_ids(specs), ["aaa", "bbb"])

    def test_no_request_ids_means_nothing_to_mark(self):
        self.assertEqual(tasks.done_ids([{"id": "P-0001"}]), [])
        self.assertEqual(tasks.done_ids([]), [])


if __name__ == "__main__":
    unittest.main()


class TestCommandLedger(unittest.TestCase):
    """コア発 command の処理済み台帳 (設計 D21)。

    二重実行を防ぐ鍵は command_id。キューの id は source から導くので、
    同じ command からは必ず同じ依頼 id になる。
    """

    def test_source_is_stable_and_distinct_from_notes(self):
        src = tasks.command_source("core-abc")
        self.assertEqual(src, tasks.command_source("core-abc"))
        self.assertNotEqual(tasks.make_id(src),
                            tasks.make_id("ops/feedback/inbox/core-abc.json"))

    def test_same_command_never_queues_twice(self):
        src = tasks.command_source("core-abc")
        records = tasks.merge_new([], [{"source": src, "body": "本文"}], NOW)
        again = tasks.merge_new(records, [{"source": src, "body": "本文"}], NOW)
        self.assertEqual(len(again), 1)

    def test_ledger_ids_ignores_broken_rows(self):
        rows = [{"command_id": "core-a"}, {}, {"command_id": None},
                {"command_id": "core-b"}]
        self.assertEqual(tasks.ledger_ids(rows), {"core-a", "core-b"})

    def test_ledger_entry_records_what_happened(self):
        e = tasks.ledger_entry("core-a", "task-request", "accepted", NOW)
        self.assertEqual(e["command_id"], "core-a")
        self.assertEqual(e["type"], "task-request")
        self.assertEqual(e["status"], "accepted")
        self.assertTrue(e["at"].endswith("Z"))
