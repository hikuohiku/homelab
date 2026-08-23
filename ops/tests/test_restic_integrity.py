"""ops/tools/restic_integrity.py (P-0187, restic 実データ回転読みの選択ロジック) を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_restic_integrity`。
純関数のみのモジュールなのでパッケージ import で直接ロードする
(test_version_watch.py 流儀)。**一切ネットワークに出ない**。

固定する契約:
- 決定論的 & 再実行可能: 同じ (repo, 日付) は何度呼んでも同じスライス。
  同じ月内なら日付によらず同じスライス (当月内のリトライで読む場所が変わらない)
- 周期ごとに全スライスを巡る: 任意の repo・任意の開始月から T か月連続すれば
  スライスはちょうど 1..T を一巡する (「約 3 ヶ月で一周」の中身)
- 範囲外の日付で壊れない: date の最小値〜最大値・閏日・年跨ぎでも例外を出さず、
  常に 1..cycle に収まる
- カバー率は嘘をつかない: 実行記録の日付から導いた期待スロットと一致しない記録、
  未来日、窓の外は採用しない。同一月の再実行記録は重複として潰れる

ゴールデン値 (offset / slot) は sha256(repo)[:8 バイト big endian] % 3 を Python と
独立に `sha256sum` + bc の多倍長計算で二度出し、一致したものをピンしている。
(bash の $((0x...)) は int64 に wrap するため上位ビット立つ repo で負数になり、
素のシェル算術では検証にならない — この罠自体が PROGRESS.md に記録されている)
"""

import datetime
import io
import unittest
from contextlib import redirect_stdout

from ops.tools import restic_integrity as ri

REPOS = ri.REPOSITORIES


def d(year, month, day):
    return datetime.date(year, month, day)


def advance_month(year, month, n):
    total = year * 12 + (month - 1) + n
    return divmod(total, 12)[0], divmod(total, 12)[1] + 1


class RepositoriesTest(unittest.TestCase):
    def test_repositories_match_initializer_measurement(self):
        # 2026-08-23 に initializer が全 CronJob の RESTIC_REPOSITORY から実測した 5 本。
        # 「autopilot-core 相当」の restic リポジトリは実在しない (PROJECT.md 前提)。
        self.assertEqual(
            REPOS,
            ("coder-postgres", "coder-workspace-homes",
             "immich", "syncthing", "vaultwarden"))


class SlotSelectionTest(unittest.TestCase):
    def test_same_inputs_same_output(self):
        for repo in REPOS:
            for day in (d(2026, 8, 23), d(2000, 1, 1), d(2099, 12, 31)):
                self.assertEqual(ri.slot_for_date(repo, day),
                                 ri.slot_for_date(repo, day))

    def test_rerun_same_day_reads_same_slice(self):
        # 再実行可能性の契約: 失敗リトライで読む場所が動かない
        p1 = ri.plan("vaultwarden", date=d(2026, 8, 23))
        p2 = ri.plan("vaultwarden", date=d(2026, 8, 23))
        self.assertEqual(p1, p2)

    def test_same_month_different_day_is_same_slice(self):
        # 月次 CronJob の実行日がずれても (1 日でも月末でも) 当月のスライスは不変
        for repo in REPOS:
            first = ri.slot_for_date(repo, d(2026, 8, 1))
            last = ri.slot_for_date(repo, d(2026, 8, 31))
            self.assertEqual(first, last)

    def test_golden_slots_at_2026_08_23(self):
        # sha256(repo)[:8 bytes] big endian % 3 の独立計算 (sha256sum + bc) と一致した値。
        expected = {
            "vaultwarden": (0, 2, "2/3"),
            "immich": (1, 3, "3/3"),
            "coder-postgres": (0, 2, "2/3"),
            "coder-workspace-homes": (0, 2, "2/3"),
            "syncthing": (2, 1, "1/3"),
        }
        for repo, (offset, slot, subset) in expected.items():
            self.assertEqual(ri.repo_offset(repo), offset, repo)
            self.assertEqual(ri.slot_for_date(repo, d(2026, 8, 23)), slot, repo)
            self.assertEqual(
                ri.plan(repo, date=d(2026, 8, 23))["subset"], subset, repo)

    def test_slot_stays_in_bounds_for_wild_dates(self):
        days = [
            d(1, 1, 1), d(9999, 12, 31), d(1900, 3, 1),
            d(2000, 2, 29), d(2024, 2, 29), d(2100, 2, 28), d(2026, 8, 23),
        ]
        for repo in REPOS:
            for day in days:
                slot = ri.slot_for_date(repo, day)
                self.assertTrue(1 <= slot <= 3, (repo, day, slot))

    def test_rotation_continues_across_year_boundary(self):
        # 年をまたいでも位相が 1 進むだけで初期化されない
        for repo in REPOS:
            december = ri.slot_for_date(repo, d(2026, 12, 15))
            january = ri.slot_for_date(repo, d(2027, 1, 15))
            self.assertEqual(january, december % 3 + 1, repo)

    def test_full_rotation_over_cycle_months(self):
        # 任意の repo・任意の開始月から T=3 か月連続で走れば全スライスを一巡する
        for repo in REPOS:
            for start in ((2025, 1), (2026, 6), (1999, 12)):
                seen = set()
                for n in range(3):
                    year, month = advance_month(*start, n)
                    seen.add(ri.slot_for_date(repo, d(year, month, 15)))
                self.assertEqual(seen, {1, 2, 3}, (repo, start))

    def test_invalid_inputs_raise(self):
        for bad_repo in (None, "", 123, b"vaultwarden"):
            with self.assertRaises(ValueError):
                ri.slot_for_date(bad_repo, d(2026, 8, 23))
        for bad_date in ("not-a-date", "2026-13-01", "2026-02-30", "20260823"):
            with self.assertRaises(ValueError):
                ri.slot_for_date("vaultwarden", bad_date)
        with self.assertRaises(TypeError):
            ri.slot_for_date("vaultwarden", 123)
        for bad_cycle in (0, -1, True, "3", None):
            with self.assertRaises(ValueError):
                ri.slot_for_date("vaultwarden", d(2026, 8, 23), bad_cycle)


class SubsetArgTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(ri.subset_arg(1), "1/3")
        self.assertEqual(ri.subset_arg(3), "3/3")
        self.assertEqual(ri.subset_arg(2, cycle=4), "2/4")

    def test_out_of_range_slot_raises(self):
        for bad in (0, -1, 4, True, "2", None):
            with self.assertRaises(ValueError):
                ri.subset_arg(bad)

    def test_default_cycle_constant(self):
        # spec の「約 3 ヶ月で一周」。勝手に変わったら回転の意味が変わるのでピン
        self.assertEqual(ri.DEFAULT_CYCLE_MONTHS, 3)


class PlanTest(unittest.TestCase):
    def test_shape_and_values(self):
        p = ri.plan("vaultwarden", date=d(2026, 8, 23))
        self.assertEqual(p, {
            "repo": "vaultwarden",
            "date": "2026-08-23",
            "cycle": 3,
            "offset": 0,
            "slot": 2,
            "subset": "2/3",
        })

    def test_accepts_iso_string_date(self):
        self.assertEqual(ri.plan("immich", date="2026-08-23")["slot"],
                         ri.plan("immich", date=d(2026, 8, 23))["slot"])

    def test_default_date_is_today_utc(self):
        utc_today = datetime.datetime.now(datetime.timezone.utc).date()
        p = ri.plan("syncthing")
        self.assertEqual(p["date"], utc_today.isoformat())


class CoverageFromRecordsTest(unittest.TestCase):
    # vaultwarden (offset=0) のスロット: mi%3 → slot。2026-06 ≡ 2 → 3,
    # 2026-07 ≡ 0 → 1, 2026-08 ≡ 1 → 2 (手計算。ゴールデン試験と独立に確認済み)
    TODAY = "2026-08-23"

    def good_records(self):
        return [
            {"date": "2026-06-05", "slot": 3},
            {"date": "2026-07-10", "slot": 1},
            {"date": "2026-08-15", "slot": 2},
        ]

    def test_three_consecutive_months_cover_everything(self):
        r = ri.coverage_from_records("vaultwarden", self.good_records(),
                                     today=self.TODAY)
        self.assertEqual(r["slices_seen"], [1, 2, 3])
        self.assertEqual(r["missing_slices"], [])
        self.assertEqual(r["coverage_fraction"], 1.0)
        self.assertEqual(r["coverage_percent"], 100.0)
        self.assertEqual(r["window_first_month"], "2026-06")
        self.assertEqual(r["window_last_month"], "2026-08")
        self.assertEqual(r["skipped"],
                         {"malformed": 0, "future": 0,
                          "out_of_window": 0, "inconsistent": 0})

    def test_partial_coverage_reports_missing_slice(self):
        r = ri.coverage_from_records(
            "vaultwarden", self.good_records()[:1] + self.good_records()[2:],
            today=self.TODAY)
        self.assertEqual(r["slices_seen"], [2, 3])
        self.assertEqual(r["missing_slices"], [1])
        self.assertEqual(r["coverage_fraction"], 2 / 3)
        self.assertEqual(r["coverage_percent"], 66.7)

    def test_rerun_in_same_month_does_not_inflate(self):
        records = self.good_records() + [{"date": "2026-08-20", "slot": 2}]
        r = ri.coverage_from_records("vaultwarden", records, today=self.TODAY)
        self.assertEqual(r["records_total"], 4)
        self.assertEqual(r["slices_seen"], [1, 2, 3])
        self.assertEqual(r["coverage_fraction"], 1.0)

    def test_empty_records_show_zero_coverage_without_lies(self):
        r = ri.coverage_from_records("vaultwarden", [], today=self.TODAY)
        self.assertEqual(r["coverage_fraction"], 0.0)
        self.assertEqual(r["missing_slices"], [1, 2, 3])
        self.assertEqual(r["records_total"], 0)

    def test_inconsistent_slot_record_is_not_adopted(self):
        records = self.good_records() + [{"date": "2026-08-15", "slot": 99}]
        r = ri.coverage_from_records("vaultwarden", records, today=self.TODAY)
        self.assertEqual(r["skipped"]["inconsistent"], 1)
        self.assertEqual(r["coverage_fraction"], 1.0)

    def test_future_date_is_skipped_as_skew(self):
        records = self.good_records() + [{"date": "2026-08-25", "slot": 2}]
        r = ri.coverage_from_records("vaultwarden", records, today=self.TODAY)
        self.assertEqual(r["skipped"]["future"], 1)
        self.assertEqual(r["coverage_fraction"], 1.0)

    def test_out_of_window_old_record_does_not_count(self):
        records = [{"date": "2026-05-30", "slot": 2}] + self.good_records()
        r = ri.coverage_from_records("vaultwarden", records, today=self.TODAY)
        self.assertEqual(r["skipped"]["out_of_window"], 1)
        self.assertEqual(r["coverage_fraction"], 1.0)

    def test_malformed_records_are_skipped_without_raising(self):
        records = [
            None,
            "not-a-dict",
            {},
            {"date": "garbage", "slot": 1},
            {"date": "2026-08-15"},                    # slot 欠損
            {"date": "2026-08-15", "slot": True},      # bool は int の派生なので明示的に弾く
            {"date": "2026-08-15", "slot": "2"},       # 文字列は不可
        ] + self.good_records()
        r = ri.coverage_from_records("vaultwarden", records, today=self.TODAY)
        self.assertEqual(r["skipped"]["malformed"], 7)
        self.assertEqual(r["coverage_fraction"], 1.0)

    def test_extra_fields_are_tolerated(self):
        records = [{"date": "2026-06-05", "slot": 3, "job": "vw-integrity",
                    "packs_read": 42}]
        r = ri.coverage_from_records("vaultwarden", records, today=self.TODAY)
        self.assertEqual(r["slices_seen"], [3])

    def test_records_are_per_repo(self):
        # 他 repo 分の記録を流用しても成立しない (期待スロットが一致しない)
        r = ri.coverage_from_records("immich", self.good_records(),
                                     today=self.TODAY)
        self.assertEqual(r["coverage_fraction"], 0.0)
        self.assertEqual(r["skipped"]["inconsistent"], 3)

    def test_today_string_and_garbage(self):
        r = ri.coverage_from_records("vaultwarden", self.good_records(),
                                     today="2026-08-23")
        self.assertEqual(r["coverage_fraction"], 1.0)
        with self.assertRaises(ValueError):
            ri.coverage_from_records("vaultwarden", [], today="garbage")

    def test_cycle_two_window(self):
        # offset は cycle 依存 (sha256 % cycle)。cycle=2 の immich は offset=0 で
        # 2026-07 (mi 24318 ≡ 偶数) → slot 1。窓は today の月を含む直近 2 か月
        r = ri.coverage_from_records(
            "immich",
            [{"date": "2026-07-01", "slot": 1}],
            today=self.TODAY, cycle=2)
        self.assertEqual(r["window_first_month"], "2026-07")
        self.assertEqual(r["coverage_fraction"], 0.5)


class CliTest(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ri.main(argv)
        return rc, out.getvalue()

    def test_plan_prints_key_value_lines(self):
        rc, stdout = self.run_cli(
            ["plan", "--repo", "immich", "--date", "2026-08-23"])
        self.assertEqual(rc, 0)
        lines = dict(line.split("=", 1) for line in stdout.strip().splitlines())
        self.assertEqual(lines["REPO"], "immich")
        self.assertEqual(lines["DATE"], "2026-08-23")
        self.assertEqual(lines["CYCLE"], "3")
        self.assertEqual(lines["OFFSET"], "1")
        self.assertEqual(lines["SLOT"], "3")
        self.assertEqual(lines["SUBSET"], "3/3")

    def test_plan_is_deterministic(self):
        argv = ["plan", "--repo", "syncthing", "--date", "2026-08-23"]
        _, first = self.run_cli(argv)
        _, second = self.run_cli(argv)
        self.assertEqual(first, second)
        self.assertIn("SUBSET=1/3", first)

    def test_missing_repo_flag_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["plan"])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
