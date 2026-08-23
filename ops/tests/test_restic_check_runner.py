"""P-0102 — restic-check runner の判定ロジックの単体テスト。

対象: ops/restic_check_runner.py (判定はすべて純関数。クラスタ内 CronJob から
ConfigMap 経由で実行される単一ソース)。test_backup_coverage.py の流儀に従い、
純関数側は合成入力で両方向 (落ちること / 通ること) を固定する — 実環境だけを
見るテストは「今たまたま通っている」と「正しい」を区別できない。

リポジトリルートから:
  python3 -m unittest ops.tests.test_restic_check_runner
  (CI は python3 -m unittest discover -s ops/tests -t . で回る)
"""

import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest import mock

from ops import restic_check_runner as rcr

UTC = timezone.utc
NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def _iso(dt):
    """restic 風の RFC3339 (ナノ秒 + Z) 文字列を作る。パーサが丸めることを含めた確認。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"


def _snap_json(hours_ago=None, *, dt=None):
    if hours_ago is not None:
        dt = NOW - timedelta(hours=hours_ago)
    return json.dumps([{"id": "deadbeef", "time": _iso(dt), "hostname": "node01"}])


def make_records(check_rcs=None, ages_hours=None):
    """全 5 リポジトリ分の正常系レコード。指定のないものは check 成功・5 時間前とする。"""
    check_rcs = check_rcs or {}
    ages_hours = ages_hours or {}
    out = []
    for name in rcr.REPOS:
        out.append({
            "repo": name,
            "check_rc": check_rcs.get(name, 0),
            "snapshots_rc": 0,
            "snapshots_json": _snap_json(ages_hours.get(name, 5.0)),
        })
    return out


class TestParseRfc3339(unittest.TestCase):
    def test_restic_style_timestamp(self):
        t = rcr._parse_rfc3339("2026-08-21T17:45:03.123456789Z")
        self.assertEqual(t, datetime(2026, 8, 21, 17, 45, 3, 123456, tzinfo=UTC))

    def test_offset_timezone(self):
        t = rcr._parse_rfc3339("2026-08-21T17:45:03+09:00")
        self.assertEqual(t.utcoffset(), timedelta(hours=9))
        self.assertEqual(t.hour, 17)

    def test_invalid_inputs_return_none(self):
        for bad in ("", "not-a-time", "2026-13-99T99:99:99Z", None, 42):
            with self.subTest(bad=bad):
                self.assertIsNone(rcr._parse_rfc3339(bad))


class TestParseLatestSnapshotTime(unittest.TestCase):
    def test_single_snapshot(self):
        text = json.dumps([{"time": "2026-08-22T07:00:00.500000000Z", "id": "x"}])
        t = rcr.parse_latest_snapshot_time(text)
        self.assertEqual(t, datetime(2026, 8, 22, 7, 0, 0, 500000, tzinfo=UTC))

    def test_multiple_snapshots_picks_newest(self):
        # --latest 1 でも将来の挙動変更に備え最大を取る契約
        text = json.dumps([
            {"time": "2026-08-20T00:00:00Z"},
            {"time": "2026-08-22T00:00:00Z"},
        ])
        t = rcr.parse_latest_snapshot_time(text)
        self.assertEqual(t, datetime(2026, 8, 22, tzinfo=UTC))

    def test_broken_inputs_return_none(self):
        for bad in ("", "not json", "{}", "[]",
                    json.dumps([{"no_time_key": 1}]),
                    json.dumps([{"time": "garbage"}])):
            with self.subTest(bad=bad):
                self.assertIsNone(rcr.parse_latest_snapshot_time(bad))


class TestEvaluate(unittest.TestCase):
    def test_all_green_is_exit_zero_and_silent(self):
        ev = rcr.evaluate(make_records(), now=NOW)
        self.assertEqual(ev["exit_code"], 0)
        self.assertFalse(ev["notify"])
        self.assertEqual(len(ev["results"]), len(rcr.REPOS))
        self.assertTrue(all(r["freshness"] == "ok" for r in ev["results"]))

    def test_exactly_24h_is_ok_over_24h_is_stale(self):
        """閾値は「24h 超で warn」。ちょうど 24h は健全側、超えたら stale。"""
        ok = rcr.evaluate(make_records(ages_hours={"vaultwarden": 24.0}), now=NOW)
        self.assertFalse(ok["notify"])
        stale = rcr.evaluate(make_records(ages_hours={"vaultwarden": 24.1}), now=NOW)
        self.assertTrue(stale["notify"])
        self.assertEqual(stale["exit_code"], 2)
        vw = next(r for r in stale["results"] if r["repo"] == "vaultwarden")
        self.assertEqual(vw["freshness"], "warn")

    def test_check_failure_does_not_short_circuit(self):
        """1 リポジトリの失敗で打ち切らない。残りも全部判定する (PROJECT.md 方針 1)。"""
        ev = rcr.evaluate(
            make_records(check_rcs={"immich": 1, "syncthing": 3}), now=NOW
        )
        self.assertEqual(ev["exit_code"], 1)
        by_repo = {r["repo"]: r for r in ev["results"]}
        self.assertEqual(by_repo["immich"]["check_rc"], 1)
        self.assertEqual(by_repo["syncthing"]["check_rc"], 3)
        # 失敗していない 3 リポジトリはちゃんと鮮度まで判定されている
        self.assertEqual(by_repo["vaultwarden"]["freshness"], "ok")
        self.assertEqual(sorted(ev["failed_repos"]), ["immich", "syncthing"])

    def test_missing_record_fails_loudly(self):
        records = [r for r in make_records() if r["repo"] != "coder-postgres"]
        ev = rcr.evaluate(records, now=NOW)
        self.assertEqual(ev["exit_code"], 1)
        cp = next(r for r in ev["results"] if r["repo"] == "coder-postgres")
        self.assertEqual(cp["check_rc"], rcr.MISSING_RC)
        self.assertIn("coder-postgres", ev["failed_repos"])

    def test_unexpected_repo_fails_as_table_drift(self):
        """REPOS 表に無いリポジトリのレコード → backup 対象追加漏れの検知線。"""
        records = make_records() + [
            {"repo": "newapp-data", "check_rc": 0, "snapshots_rc": 0,
             "snapshots_json": _snap_json(2.0)}
        ]
        ev = rcr.evaluate(records, now=NOW)
        self.assertEqual(ev["exit_code"], 1)
        extra = next(r for r in ev["results"] if r["repo"] == "newapp-data")
        self.assertEqual(extra["freshness"], "unexpected")

    def test_snapshots_command_failure_counts_stale_not_check_failure(self):
        """check は通ったが snapshots 一覧が取れない = 調査の入り口が違う別状態。"""
        records = make_records()
        records[0]["snapshots_rc"] = 10
        records[0]["snapshots_json"] = ""
        ev = rcr.evaluate(records, now=NOW)
        self.assertEqual(ev["exit_code"], 2)
        first = ev["results"][0]
        self.assertEqual(first["freshness"], "unknown")
        self.assertEqual(ev["stale_repos"], [first["repo"]])

    def test_empty_snapshot_list_is_no_snapshot(self):
        """夜間 backup が動いているなら空配列はありえない。沈黙を健全と解釈しない。"""
        records = make_records()
        records[1]["snapshots_json"] = "[]"
        ev = rcr.evaluate(records, now=NOW)
        self.assertEqual(ev["exit_code"], 2)
        second = ev["results"][1]
        self.assertEqual(second["freshness"], "no-snapshot")

    def test_check_failure_wins_over_staleness_in_exit_code(self):
        records = make_records(check_rcs={"immich": 1},
                               ages_hours={"syncthing": 72.0})
        ev = rcr.evaluate(records, now=NOW)
        self.assertEqual(ev["exit_code"], 1)
        self.assertIn("syncthing", ev["stale_repos"])

    def test_naive_now_treated_as_utc(self):
        naive = datetime(2026, 8, 22, 12, 0, 0)  # tzinfo 無し
        ev = rcr.evaluate(make_records(), now=naive)
        self.assertEqual(ev["exit_code"], 0)


class TestEvidenceRecords(unittest.TestCase):
    def test_green_evidence_satisfies_acceptance_shape(self):
        """受入 verify #3 のアサーションそのものを合成入力で再現する。

        実鍵での evidence (#3 セッション) はこの形状で保存される。
        """
        evidence = rcr.evidence_records(rcr.evaluate(make_records(), now=NOW))
        dumped = json.dumps(evidence, ensure_ascii=False)
        d = json.loads(dumped)
        self.assertGreaterEqual(len(d), 5)
        self.assertTrue(all(x["exit_code"] == 0 for x in d))
        for x in d:
            self.assertIn("repo", x)
            self.assertIn("check_rc", x)
            self.assertIn("snapshot_age_hours", x)

    def test_failure_evidence_keeps_nonzero_visible(self):
        evidence = rcr.evidence_records(
            rcr.evaluate(make_records(check_rcs={"immich": 1}), now=NOW))
        immich = next(x for x in evidence if x["repo"] == "immich")
        self.assertNotEqual(immich["exit_code"], 0)


class TestRenderReport(unittest.TestCase):
    def test_report_contains_repos_and_machine_readable_line(self):
        report = rcr.render_report(rcr.evaluate(make_records(), now=NOW))
        for repo in rcr.REPOS:
            self.assertIn(repo, report)
        self.assertIn("overall=OK(0)", report)
        lines = report.splitlines()
        evidence_line = next(l for l in lines if l.startswith("EVIDENCE_JSON "))
        parsed = json.loads(evidence_line[len("EVIDENCE_JSON "):])
        self.assertEqual(len(parsed), len(rcr.REPOS))

    def test_stale_repo_marked_warn(self):
        report = rcr.render_report(
            rcr.evaluate(make_records(ages_hours={"immich": 48.0}), now=NOW))
        immich_line = next(l for l in report.splitlines() if l.strip().startswith("immich"))
        self.assertIn("[WARN]", immich_line)


class TestIncidentMessage(unittest.TestCase):
    def test_mentions_failed_repo_and_stays_bounded(self):
        ev = rcr.evaluate(make_records(check_rcs={"vaultwarden": 1},
                                       ages_hours={"immich": 30.0}), now=NOW)
        msg = rcr.incident_message(ev)
        self.assertIn("vaultwarden", msg)
        self.assertIn("immich", msg)
        self.assertIn("docs/backup.md", msg)
        self.assertLessEqual(len(msg), 1900)


class TestPostDiscord(unittest.TestCase):
    """webhook POST の形。2026-08-23 実機障害 (Cloudflare が python-urllib 既定 UA を
    error 1010 で 403 ブロック) を契約に昇格させたもの — UA 上書きを忘れると
    incident 通知だけが静かに消えるので、ここで機械的に守る。"""

    def post(self):
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            sent["headers"] = {k.lower(): v for k, v in req.header_items()}
            sent["data"] = req.data
            return mock.MagicMock(__enter__=lambda s: mock.MagicMock(
                read=lambda: b"", status=204))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rcr.post_discord("https://example.invalid/hook", "本文")
        return sent

    def test_user_agent_is_overridden(self):
        """既定 UA (`Python-urllib/...`) で出ていかないこと。"""
        sent = self.post()
        ua = sent["headers"].get("user-agent", "")
        self.assertTrue(ua)
        self.assertNotIn("python-urllib", ua.lower())

    def test_payload_shape_matches_heart(self):
        """payload は heart/notify.py と同じ {"content": ...} 形。"""
        sent = self.post()
        self.assertEqual(sent["url"], "https://example.invalid/hook")
        self.assertEqual(
            json.loads(sent["data"].decode()), {"content": "本文"})

    def test_http_error_propagates_to_caller(self):
        """403 等の HTTPError は握り潰さず main() 側の stderr 報告に委ねる。"""
        err = urllib.error.HTTPError(
            "https://example.invalid/hook", 403, "blocked",
            hdrs=None, fp=None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                rcr.post_discord("https://example.invalid/hook", "x")


class TestMain(unittest.TestCase):
    """main() は ConfigMap 埋め込み後のエントリポイント。ファイル群からの集約を通す。"""

    def run_main(self, records_dir):
        env = {"RESTIC_CHECK_RESULTS_DIR": records_dir}
        with mock.patch.dict(os.environ, env, clear=True), \
                redirect_stdout(StringIO()) as stdout:
            code = rcr.main()
        return code, stdout.getvalue()

    def write_records(self, tmpdir, records, name="{}.json"):
        for rec in records:
            path = os.path.join(tmpdir, name.format(rec["repo"]))
            with open(path, "w") as fh:
                fh.write(json.dumps(rec))

    def test_all_green_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_records(tmp, make_records())
            code, out = self.run_main(tmp)
        self.assertEqual(code, 0)
        self.assertIn("EVIDENCE_JSON ", out)

    def test_one_check_failure_returns_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_records(tmp, make_records(check_rcs={"syncthing": 1}))
            code, _ = self.run_main(tmp)
        self.assertEqual(code, 1)

    def test_all_checks_pass_but_stale_returns_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(UTC)
            records = make_records(ages_hours={})
            for rec in records:
                rec["snapshots_json"] = _snap_json(
                    dt=now - timedelta(hours=5)) if rec["repo"] != "immich" \
                    else _snap_json(dt=now - timedelta(hours=48))
            self.write_records(tmp, records)
            code, _ = self.run_main(tmp)
        self.assertEqual(code, 2)

    def test_empty_dir_means_everything_missing(self):
        """レコードが 1 つも無い状態を黙って緑にしない (initContainer が死んでいたら赤)。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_main(tmp)
        self.assertEqual(code, 1)
        self.assertIn("EVIDENCE_JSON ", out)

    def test_corrupt_record_file_does_not_crash_or_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_records(tmp, make_records())
            with open(os.path.join(tmp, "broken.json"), "w") as fh:
                fh.write("{not json")
            code, _ = self.run_main(tmp)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
