"""apps/ops-health-reporter/backup_freshness.py (P-0157, backup 鮮度計) の純関数を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_backup_freshness`。
backup_freshness.py は report.py と同じく ConfigMap から直接起動される単一ファイルの
ためパッケージではなく、テストからは importlib で実ファイルをロードする
(test_download_budget.py と同じ形)。

固定する契約:
- 換算: 最終成功時刻 → hours_since_success。未成功は None、clock skew (未来時刻) は
  0.0 に丸める (skew で鳴らない)
- 閾値判定: 閾値ちょうどは鳴る側 (warn)。閾値が壊れていれば決め打ちせず
  unconfigured を正直に返す (download_budget.judge() と同じ倒し方)
- 成功時刻のソース: 主系統 CronJob status.lastSuccessfulTime、副系統 Complete=True の
  子 Job completionTime。Failed Job の completionTime は成功と数えない
- 収集できない経路も黙って落とさず error エントリとして載せる。verify (#2) の契約上
  全エントリが repo / hours_since_success キーを持つこと
"""

import datetime
import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "backup_freshness.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("backup_freshness_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bf = _load_module()

NOW = datetime.datetime(2026, 8, 23, 12, 0, 0, tzinfo=datetime.timezone.utc)


def iso(hours_ago):
    """NOW から hours_ago 時間前の RFC3339 文字列 (k8s の timestamp 形)。"""
    dt = NOW - datetime.timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def cronjob(last_success=None):
    status = {"lastSuccessfulTime": last_success} if last_success else {}
    return {"metadata": {"name": "x-restic-backup", "namespace": "x"}, "status": status}


def job(cronjob_name, completion=None, condition="Complete", namespace="x"):
    j = {
        "metadata": {
            "name": "child-1",
            "namespace": namespace,
            "ownerReferences": [{"kind": "CronJob", "name": cronjob_name}],
        },
        "status": {},
    }
    if completion:
        j["status"]["completionTime"] = completion
        j["status"]["conditions"] = [{"type": condition, "status": "True"}]
    return j


class ParseIsoTest(unittest.TestCase):
    def test_z_suffix_becomes_utc(self):
        dt = bf.parse_iso("2026-08-22T03:00:00Z")
        self.assertEqual(dt, datetime.datetime(2026, 8, 22, 3, 0, 0,
                                               tzinfo=datetime.timezone.utc))

    def test_garbage_is_none_not_crash(self):
        self.assertIsNone(bf.parse_iso("not-a-time"))
        self.assertIsNone(bf.parse_iso(""))
        self.assertIsNone(bf.parse_iso(None))
        self.assertIsNone(bf.parse_iso(123))


class HoursSinceTest(unittest.TestCase):
    def test_none_means_never_succeeded(self):
        self.assertIsNone(bf.hours_since(None, NOW))
        self.assertIsNone(bf.hours_since("garbage", NOW))

    def test_converts_elapsed_time_to_hours(self):
        # 換算の上側: 72h 前の成功 → 72.0h
        self.assertEqual(bf.hours_since(iso(72), NOW), 72.0)

    def test_fractional_hours(self):
        self.assertAlmostEqual(bf.hours_since(iso(1.5), NOW), 1.5)

    def test_future_timestamp_clamps_to_zero(self):
        # clock skew で未来に見える成功は 0 扱い。鮮度計が skew だけで鳴らない
        future = "2026-08-23T15:00:00Z"
        self.assertEqual(bf.hours_since(future, NOW), 0.0)


class JudgeTest(unittest.TestCase):
    WARN = 72

    def test_below_threshold_is_ok(self):
        self.assertEqual(bf.judge(71.99, self.WARN), "ok")

    def test_exactly_at_threshold_warns(self):
        # 境界は鳴る側に倒す (download_budget.judge() と同じ)
        self.assertEqual(bf.judge(72.0, self.WARN), "warn")

    def test_above_threshold_warns(self):
        self.assertEqual(bf.judge(100.0, self.WARN), "warn")

    def test_unmeasurable_is_no_data(self):
        self.assertEqual(bf.judge(None, self.WARN), "no_data")

    def test_broken_threshold_is_unconfigured(self):
        # 閾値が無い/壊れているときに適当な値で判定しない
        for bad in (None, 0, -1, "72", True):
            self.assertEqual(bf.judge(100.0, bad), "unconfigured")

    def test_non_number_hours_is_no_data(self):
        for weird in ("72", True, [1]):
            self.assertEqual(bf.judge(weird, self.WARN), "no_data")


class ExtractLastSuccessTest(unittest.TestCase):
    def test_prefers_cronjob_last_successful_time(self):
        value, source = bf.extract_last_success(
            cronjob(iso(30)), [job("x-restic-backup", iso(40))]
        )
        self.assertEqual(value, iso(30))
        self.assertEqual(source, "cronjob.status.lastSuccessfulTime")

    def test_falls_back_to_complete_job_completion_time(self):
        # 主系統が空でも子 Job (Complete=True) から読める。
        # ttlSecondsAfterFinished で子 Job が消えるまではこの経路も有効
        value, source = bf.extract_last_success(
            cronjob(), [job("x-restic-backup", iso(28))]
        )
        self.assertEqual(value, iso(28))
        self.assertEqual(source, "job.status.completionTime")

    def test_failed_job_does_not_count_as_success(self):
        # completionTime は Failed Job にも付く。失敗を成功と数えたら鮮度計は
        # 「失敗し続けている」状態を「新鮮」と誤報する
        value, source = bf.extract_last_success(
            cronjob(), [job("x-restic-backup", iso(1), condition="Failed")]
        )
        self.assertIsNone(value)
        self.assertIsNone(source)

    def test_takes_max_of_multiple_complete_jobs(self):
        older = job("x-restic-backup", iso(50))
        older["metadata"]["name"] = "child-old"
        newer = job("x-restic-backup", iso(20))
        newer["metadata"]["name"] = "child-new"
        value, source = bf.extract_last_success(cronjob(), [older, newer])
        self.assertEqual(value, iso(20))

    def test_job_owned_by_other_cronjob_is_ignored(self):
        value, source = bf.extract_last_success(
            cronjob(), [job("other-cronjob", iso(1))]
        )
        self.assertIsNone(value)

    def test_no_success_record_anywhere(self):
        value, source = bf.extract_last_success(cronjob(), [])
        self.assertIsNone(value)
        self.assertIsNone(source)


class BuildEntryTest(unittest.TestCase):
    SPEC = {"repo": "myrepo", "namespace": "x", "cronjob": "x-restic-backup"}

    def test_fresh_entry_is_ok(self):
        entry = bf.build_entry(self.SPEC, cronjob(iso(24)), [], NOW, 72)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["hours_since_success"], 24.0)
        self.assertEqual(entry["last_success_at"], iso(24))
        self.assertEqual(entry["source"], "cronjob.status.lastSuccessfulTime")

    def test_stale_entry_warns(self):
        entry = bf.build_entry(self.SPEC, cronjob(iso(73)), [], NOW, 72)
        self.assertEqual(entry["status"], "warn")

    def test_missing_cronjob_is_error_but_keeps_contract_keys(self):
        # verify (#2) が全要素に repo / hours_since_success を要求する。
        # 収集失敗を黙って落とすと静停止が「対象外」に化ける
        entry = bf.build_entry(self.SPEC, None, [], NOW, 72)
        self.assertEqual(entry["status"], "error")
        self.assertIn("repo", entry)
        self.assertIn("hours_since_success", entry)
        self.assertIsNone(entry["hours_since_success"])
        self.assertIn("detail", entry)

    def test_never_succeeded_is_no_data(self):
        entry = bf.build_entry(self.SPEC, cronjob(), [], NOW, 72)
        self.assertEqual(entry["status"], "no_data")
        self.assertIsNone(entry["hours_since_success"])

    def test_status_uses_raw_hours_not_rounded_value(self):
        # 71.9999h は小数第 2 位で丸めると 72.0 になるが、判定は丸め前の値で倒す。
        # 逆にすると「閾値未満なのに warn」の境界誤発報が起きる
        almost = NOW - datetime.timedelta(hours=71, minutes=59, seconds=59.964)
        entry = bf.build_entry(
            self.SPEC, cronjob(almost.strftime("%Y-%m-%dT%H:%M:%S.%fZ")), [], NOW, 72
        )
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["hours_since_success"], 72.0)


class BuildReportTest(unittest.TestCase):
    def test_reports_all_five_repositories_in_order(self):
        report = bf.build_report([], [], now=NOW, warn_hours=72)
        self.assertEqual(len(report), 5)
        self.assertEqual([e["repo"] for e in report],
                         [spec["repo"] for spec in bf.REPOSITORIES])

    def test_every_entry_satisfies_verify_contract(self):
        # verify (#2) と同じ条件を合成入力で先回りして固定する
        report = bf.build_report([cronjob(iso(3))], [], now=NOW, warn_hours=72)
        self.assertTrue(all("repo" in e and "hours_since_success" in e for e in report))

    def test_happy_path_matches_by_namespace_and_name(self):
        items = []
        for spec in bf.REPOSITORIES:
            cj = cronjob(iso(2))
            cj["metadata"] = {"name": spec["cronjob"], "namespace": spec["namespace"]}
            items.append(cj)
        report = bf.build_report(items, [], now=NOW, warn_hours=72)
        self.assertTrue(all(e["status"] == "ok" for e in report))
        self.assertTrue(all(e["hours_since_success"] == 2.0 for e in report))

    def test_unrelated_cronjobs_do_not_leak_into_report(self):
        stranger = cronjob(iso(1))
        stranger["metadata"] = {"name": "unrelated-job", "namespace": "default"}
        report = bf.build_report([stranger], [], now=NOW, warn_hours=72)
        self.assertEqual(len(report), 5)
        self.assertTrue(all(e["status"] == "error" for e in report))

    def test_realistic_fixture_with_child_jobs(self):
        """batch API の実物に近い形 (ownerReferences / conditions) での結合確認。"""
        spec = bf.REPOSITORIES[1]  # coder-postgres
        cron = {
            "metadata": {"name": spec["cronjob"], "namespace": "coder",
                         "uid": "cj-uid-1"},
            "status": {},
        }
        jobs = [
            # Complete な子 Job → フォールバックで読まれる
            {"metadata": {"name": "coder-restic-backup-1", "namespace": "coder",
                          "ownerReferences": [{"kind": "CronJob",
                                               "name": spec["cronjob"],
                                               "controller": True}]},
             "status": {"completionTime": iso(30),
                        "conditions": [{"type": "Complete", "status": "True"}]}},
            # 失敗した子 Job → 数えない
            {"metadata": {"name": "coder-restic-backup-2", "namespace": "coder",
                          "ownerReferences": [{"kind": "CronJob",
                                               "name": spec["cronjob"],
                                               "controller": True}]},
             "status": {"completionTime": iso(1),
                        "conditions": [{"type": "Failed", "status": "True"}]}},
        ]
        report = bf.build_report(
            [cron], jobs, now=NOW, warn_hours=72
        )
        entry = next(e for e in report if e["repo"] == spec["repo"])
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["hours_since_success"], 30.0)
        self.assertEqual(entry["source"], "job.status.completionTime")


class LoadWarnHoursTest(unittest.TestCase):
    def setUp(self):
        import json as _json
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "rules.json"
        self._json = _json

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, doc):
        self.path.write_text(self._json.dumps(doc))

    def test_coerce_accepts_positive_numbers_only(self):
        # bool は int の派生なので明示的に弾く (True=1 で warn になったら即鳴く)
        self.assertEqual(bf.coerce_warn_hours(48), 48)
        for bad in (None, 0, -1, True, "72"):
            self.assertEqual(bf.coerce_warn_hours(bad), bf.DEFAULT_WARN_HOURS)

    def test_reads_value_from_rules_json(self):
        self.write({"backup_freshness": {"warn_hours": 48}})
        self.assertEqual(bf.load_warn_hours(self.path), 48)

    def test_missing_file_falls_back_to_default(self):
        self.assertEqual(bf.load_warn_hours(self.path.parent / "nope.json"),
                         bf.DEFAULT_WARN_HOURS)

    def test_broken_values_do_not_become_thresholds(self):
        for bad in (None, 0, -1, True, "72"):
            self.write({"backup_freshness": {"warn_hours": bad}})
            self.assertEqual(bf.load_warn_hours(self.path), bf.DEFAULT_WARN_HOURS)

    def test_broken_json_falls_back_to_default(self):
        self.path.write_text("{not json")
        self.assertEqual(bf.load_warn_hours(self.path), bf.DEFAULT_WARN_HOURS)

    def test_repo_rules_json_is_the_live_source(self):
        # repo 本体の rules.json が壊れていないか、モジュール既定値との一致も見る。
        # report.py はこのファイルを GitHub 経由で読む (kustomize の root 外参照は
        # load restrictor が拒むため embed しない — backup_freshness.py 冒頭参照)
        value = bf.load_warn_hours(REPO / "ops" / "rules.json")
        self.assertEqual(value, bf.DEFAULT_WARN_HOURS)


if __name__ == "__main__":
    unittest.main()
