"""ops/life/reminders.py (暦レンダラ) と validate.check_reminders を固定する (P-0231)。

リポジトリルートから `python3 -m unittest ops.tests.test_reminders`。
now をすべて注入し、時刻が変わっても判定が動かないよう fixture で固定する。
固定する対象:

- 年 recurrence (誕生日など毎年くるもの。年またぎ・2/29 の平年扱いを含む)
- 48h 窓の端 (今日・明日・明後日・窓の外・ちょうど境界)
- JST 起点の「今日」(heart pod は UTC なので、UTC のまま判定すると
  人間の夜に「明日」が入れ替わる)
"""

import datetime
import sys
import unittest
from pathlib import Path

# ops/validate.py は `python3 ops/validate.py` (スクリプト実行) 前提で
# `import ledger` しているため、モジュールとして import するときは ops/ を
# sys.path に足す必要がある (既存テストに import 前例が無いのでここでやる)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import validate  # noqa: E402
from ops.life import reminders as rem  # noqa: E402

JST = rem.JST


def jst(*args):
    return datetime.datetime(*args, tzinfo=JST)


def entry(date, repeat="none", title="テスト用件", note=""):
    e = {"date": date, "title": title, "repeat": repeat}
    if note:
        e["note"] = note
    return e


class OccurrenceTest(unittest.TestCase):
    def test_yearly_ignores_anchor_year(self):
        today = datetime.date(2026, 8, 23)
        a1900 = rem.occurrence_date(datetime.date(1900, 9, 1), "year", today)
        a2000 = rem.occurrence_date(datetime.date(2000, 9, 1), "year", today)
        self.assertEqual(a1900, a2000)
        self.assertEqual(a1900, datetime.date(2026, 9, 1))

    def test_yearly_bumps_to_next_year_across_boundary(self):
        # 12/31 時点で 1/2 の誕生日は来年の分を指す
        occ = rem.occurrence_date(datetime.date(2000, 1, 2), "year", datetime.date(2026, 12, 31))
        self.assertEqual(occ, datetime.date(2027, 1, 2))

    def test_feb29_falls_back_to_mar1_in_common_year(self):
        occ = rem.occurrence_date(datetime.date(2004, 2, 29), "year", datetime.date(2027, 2, 27))
        self.assertEqual(occ, datetime.date(2027, 3, 1))

    def test_feb29_stays_feb29_in_leap_year(self):
        occ = rem.occurrence_date(datetime.date(2004, 2, 29), "year", datetime.date(2028, 2, 27))
        self.assertEqual(occ, datetime.date(2028, 2, 29))

    def test_one_off_past_is_never_again(self):
        self.assertIsNone(
            rem.occurrence_date(datetime.date(2026, 8, 22), "none", datetime.date(2026, 8, 23))
        )

    def test_parse_date_is_strict(self):
        for bad in ("2026-9-1", "26-09-01", "2026/08/24", "", None, "2026-08-24T00:00:00"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                rem.parse_date(bad)


class CollectBoundaryTest(unittest.TestCase):
    """48h 窓の端。now = JST 2026-08-23 12:00 → 窓は [8/23 00:00, 8/25 12:00]"""

    NOW = jst(2026, 8, 23, 12, 0)

    def collect(self, entries, now=None):
        return [i["date"] for i in rem.collect(entries, now=now or self.NOW)]

    def test_today_counts_even_after_midnight_passed(self):
        # 今日 00:00 はもう過ぎているが、夜まで告げ続けてよい (始端の契約)
        self.assertEqual(self.collect([entry("2026-08-23")]), ["2026-08-23"])

    def test_tomorrow_and_day_after_are_in(self):
        self.assertEqual(
            self.collect([entry("2026-08-24"), entry("2026-08-25")]),
            ["2026-08-24", "2026-08-25"],
        )

    def test_beyond_horizon_is_out(self):
        # now+48h = 8/25 12:00 より後の 8/26 は窓の外
        self.assertEqual(self.collect([entry("2026-08-26")]), [])

    def test_horizon_edge_is_inclusive(self):
        # now を真夜中にすると now+48h が 00:00 ちょうどに重なる (境界の実測)
        midnight = jst(2026, 8, 23, 0, 0)
        self.assertEqual(
            self.collect([entry("2026-08-25")], now=midnight), ["2026-08-25"]
        )
        self.assertEqual(
            self.collect([entry("2026-08-25", )], now=midnight - datetime.timedelta(seconds=1)),
            [],
        )

    def test_yesterday_is_out_even_with_graceful_start(self):
        self.assertEqual(self.collect([entry("2026-08-22")]), [])

    def test_utc_now_reads_jst_calendar(self):
        # 2026-08-23 15:30Z = JST 8/24 00:30。UTC の日付 (8/23) で判定すると
        # 「今日」が丸一日ずれる — これが JST 起点を固定する理由
        now_utc = datetime.datetime(2026, 8, 23, 15, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(self.collect([entry("2026-08-24")], now=now_utc), ["2026-08-24"])
        self.assertEqual(self.collect([entry("2026-08-23")], now=now_utc), [])

    def test_sorted_by_occurrence_not_by_ledger_order(self):
        items = rem.collect(
            [entry("2026-08-25"), entry("2026-08-23")], now=self.NOW
        )
        self.assertEqual([i["date"] for i in items], ["2026-08-23", "2026-08-25"])

    def test_labels(self):
        items = rem.collect(
            [entry("2026-08-23", title="a"), entry("2026-08-24", title="b"),
             entry("2026-08-25", title="c")],
            now=self.NOW,
        )
        self.assertEqual([i["label"] for i in items], ["今日", "明日", "明後日"])

    def test_broken_entry_raises_loudly(self):
        with self.assertRaises(ValueError):
            rem.collect([{"title": "日付が無い"}], now=self.NOW)
        with self.assertRaises(ValueError):
            rem.collect([entry("2026-02-30")], now=self.NOW)


class RenderTest(unittest.TestCase):
    NOW = jst(2026, 8, 23, 12, 0)

    def test_line_format_with_note(self):
        text = rem.render(
            [entry("2026-08-24", title="ゴミ収集", note="燃えるごみ")], now=self.NOW
        )
        self.assertEqual(text, "明日 8/24 ゴミ収集（燃えるごみ）")

    def test_nothing_due_says_so_in_one_line(self):
        text = rem.render([entry("2027-01-01")], now=self.NOW)
        self.assertEqual(text, "直近 48 時間で告げる日はありません。")
        self.assertEqual(len(text.splitlines()), 1)

    def test_three_lines_max_with_overflow_counted(self):
        entries = [
            entry("2026-08-23", title=f"用件{i}", note="")
            for i in range(5)
        ]
        text = rem.render(entries, now=self.NOW)
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[-1], "ほか 3 件")

    def test_exactly_three_items_fit_without_truncation(self):
        entries = [entry("2026-08-23", title=f"用件{i}") for i in range(3)]
        self.assertEqual(len(rem.render(entries, now=self.NOW).splitlines()), 3)


class LoadLedgerTest(unittest.TestCase):
    def test_non_list_rejected(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text(json.dumps({"entries": []}))
            with self.assertRaises(ValueError):
                rem.load_ledger(p)


class ValidateRemindersTest(unittest.TestCase):
    """ops.validate.check_reminders の両方向。グローバルな errors を使い回すので
    1 テストごとに掃除する。"""

    def setUp(self):
        validate.errors.clear()
        validate.warnings.clear()
        self.addCleanup(validate.errors.clear)
        self.addCleanup(validate.warnings.clear)

    def errs(self):
        return "\n".join(validate.errors)

    def test_good_entries_pass(self):
        validate.check_reminders(
            [
                {"date": "2026-08-24", "title": "ゴミ収集", "repeat": "none"},
                {"date": "2000-09-01", "title": "防災の日", "repeat": "year", "note": "点検"},
            ]
        )
        self.assertEqual(validate.errors, [])

    def test_bad_repeat_and_bad_date_fail(self):
        validate.check_reminders(
            [
                {"date": "2026-08-24", "title": "a", "repeat": "monthly"},
                {"date": "2026-02-30", "title": "b", "repeat": "none"},
                {"date": "24/08/2026", "title": "c", "repeat": "none"},
                {"date": "2026-08-24", "repeat": "none"},  # title 無し
            ]
        )
        self.assertIn("repeat='monthly'", self.errs())
        self.assertIn("実在しない日付", self.errs())
        self.assertIn("YYYY-MM-DD", self.errs())
        self.assertIn("title が空", self.errs())

    def test_duplicate_date_title_fails(self):
        validate.check_reminders(
            [
                {"date": "2026-08-24", "title": "同じ", "repeat": "none"},
                {"date": "2026-08-24", "title": "同じ", "repeat": "none"},
            ]
        )
        self.assertIn("重複", self.errs())

    def test_top_level_must_be_list(self):
        validate.check_reminders({"entries": []})
        self.assertEqual(len(validate.errors), 1)


if __name__ == "__main__":
    unittest.main()
