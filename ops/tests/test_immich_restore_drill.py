"""ops/tools/immich_restore_drill.py (P-9047) の純粋関数と CLI 契約 (rc) を固定する。

本物の restic / B2 / postgres には出ない。restic / psql / pg_isready はフェイクの実行
ファイルで代替し、API probe はローカルの ThreadingHTTPServer (127.0.0.1:2283) で代替する。
リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`。
"""

import datetime
import gzip
import http.server
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ops.tools import immich_restore_drill as imd

NOW = datetime.datetime(2026, 8, 25, 0, 0, 0, tzinfo=datetime.timezone.utc)
RECENT_TIME = "2026-08-24T17:45:10Z"  # 直近 24h 以内 (日次 backup 相当)
DUMP_NAME = "immich-db-backup-20260824T020000-v3.1.0-pg16.14.sql.gz"


def recent_snapshot(short_id="61c022b6", time=RECENT_TIME):
    return {
        "id": short_id + "0" * 32,
        "short_id": short_id,
        "time": time,
        "paths": ["/mnt/immich-library"],
    }


class TestPickLatest(unittest.TestCase):
    def test_picks_latest_by_time(self):
        old = {"id": "a", "time": "2026-08-23T00:00:00Z"}
        new = {"id": "b", "time": "2026-08-24T17:45:10Z"}
        self.assertEqual(imd.pick_latest([old, new])["id"], "b")

    def test_empty_raises(self):
        with self.assertRaises(RuntimeError):
            imd.pick_latest([])


class TestSnapshotAge(unittest.TestCase):
    def test_age_within_24h(self):
        age = imd.snapshot_age_seconds(recent_snapshot(), NOW)
        self.assertGreater(age, 0)
        self.assertLess(age, 24 * 3600)

    def test_missing_time_raises(self):
        with self.assertRaises(RuntimeError):
            imd.snapshot_age_seconds({"id": "x"}, NOW)


class TestParseDumpVersion(unittest.TestCase):
    def test_parses_server_and_pg(self):
        self.assertEqual(imd.parse_dump_version(DUMP_NAME), ("3.1.0", "16.14"))

    def test_unknown_returns_none(self):
        self.assertEqual(imd.parse_dump_version("garbage.sql.gz"), (None, None))


class TestParseMaxAge(unittest.TestCase):
    def test_units(self):
        self.assertEqual(imd.parse_max_age("3d"), 3 * 86400)
        self.assertEqual(imd.parse_max_age("12h"), 12 * 3600)
        self.assertEqual(imd.parse_max_age("90m"), 90 * 60)
        self.assertEqual(imd.parse_max_age("60s"), 60)
        self.assertEqual(imd.parse_max_age("2"), 2 * 86400)

    def test_bad_raises(self):
        with self.assertRaises(ValueError):
            imd.parse_max_age("xyz")


class TestFindLatestDump(unittest.TestCase):
    def test_picks_newest_dump(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "mnt" / "immich-library" / "backups"
            lib.mkdir(parents=True)
            (lib / "immich-db-backup-20260823T020000-v3.1.0-pg16.14.sql.gz").write_text("a")
            (lib / DUMP_NAME).write_text("b")
            p = imd.find_latest_dump(d)
            self.assertEqual(Path(p).name, DUMP_NAME)

    def test_missing_backups_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                imd.find_latest_dump(d)


class TestCmdCheck(unittest.TestCase):
    def test_check_returns_zero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = imd.cmd_check()
        self.assertEqual(rc, 0)
        self.assertIn("self-check ok", buf.getvalue())


class TestComposeReport(unittest.TestCase):
    def test_schema_and_scratch(self):
        report = imd.compose_report(
            recent_snapshot(), 19, 19, "16.14", "1.1.1", 200, 120, 373334885, 82, DUMP_NAME, NOW,
        )
        for key in imd.REPORT_KEYS:
            self.assertIn(key, report)
        self.assertIn("scratch", report["target"])
        self.assertTrue(report["photo_count_matches"])
        self.assertEqual(report["api_status"], 200)


class FakeRestic:
    """snapshots / restore の契約だけを模倣するフェイク restic 実行ファイル。

    restore は immich のライブラリ構成 (mnt/immich-library/backups/<dump>) を作る。
    """

    def __init__(self, tmp, dump_gz):
        self.tmp = Path(tmp)
        self.dump_gz = dump_gz
        self.bin = self.tmp / "restic"
        self.bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "mode = sys.argv[1]\n"
            "SNAP = " + json.dumps(recent_snapshot()) + "\n"
            "if mode == 'snapshots':\n"
            "    print(json.dumps([SNAP]))\n"
            "elif mode == 'restore':\n"
            "    time.sleep(1.1)  # duration_seconds > 0 を決定論的に満たす\n"
            "    target = sys.argv[sys.argv.index('--target') + 1]\n"
            "    lib = os.path.join(target, 'mnt', 'immich-library')\n"
            "    os.makedirs(os.path.join(lib, 'backups'), exist_ok=True)\n"
            "    os.makedirs(os.path.join(lib, 'upload'), exist_ok=True)\n"
            "    open(os.path.join(lib, 'upload', 'IMG_0001.jpg'), 'wb').write(b'fake-photo')\n"
            "    dump = os.path.join(lib, 'backups', " + repr(DUMP_NAME) + ")\n"
            "    open(dump, 'wb').write(" + repr(self.dump_gz) + ")\n"
            "    print('restoring snapshot %s' % SNAP['short_id'])\n"
            "    print('Summary: Restored 82 files/dirs (1.000 MiB) in 0:01')\n"
            "else:\n"
            "    sys.exit(3)\n",
            encoding="utf-8",
        )
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IEXEC)


class FakePostgresTools:
    """pg_isready は常に 0。psql は検証クエリに canned 値、ダンプ投入は stdin を読んで 0。

    -tA -c <sql> の形が検証クエリ。それ以外 (ON_ERROR_STOP 付き) は投入とみなす。
    """

    def __init__(self, tmp, asset_count):
        self.tmp = Path(tmp)
        self.psql = self.tmp / "psql"
        self.psql.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "ASSET = " + repr(str(asset_count)) + "\n"
            "args = sys.argv[1:]\n"
            "if '-tA' in args:\n"
            "    sql = args[args.index('-c') + 1]\n"
            "    if 'SHOW server_version' in sql:\n"
            "        print('16.14')\n"
            "    elif 'vchord' in sql and 'extversion' in sql:\n"
            "        print('1.1.1')\n"
            "    elif 'smart_search' in sql:\n"
            "        print('0')\n"
            "    elif 'FROM asset' in sql:\n"
            "        print(ASSET)\n"
            "    else:\n"
            "        print('')\n"
            "else:\n"
            "    data = sys.stdin.read()\n"
            "    if 'CREATE TABLE asset' not in data:\n"
            "        sys.exit(4)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        self.psql.chmod(self.psql.stat().st_mode | stat.S_IEXEC)
        self.pg_isready = self.tmp / "pg_isready"
        self.pg_isready.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        self.pg_isready.chmod(self.pg_isready.stat().st_mode | stat.S_IEXEC)


class ApiProbe:
    """127.0.0.1:2283 で 200 を返すローカル HTTP サーバ。API probe の代替。"""

    def __enter__(self):
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 2283),
            type(
                "H",
                (http.server.BaseHTTPRequestHandler,),
                {
                    "do_GET": lambda self: (
                        self.send_response(200),
                        self.end_headers(),
                        self.wfile.write(b'{"res":"pong"}'),
                    ),
                    "log_message": lambda *a, **k: None,
                },
            ),
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def make_dump_gz():
    return gzip.compress(
        b"CREATE TABLE asset (id uuid PRIMARY KEY);\n"
        b"INSERT INTO asset VALUES (1);\n"
        b"CREATE EXTENSION IF NOT EXISTS vchord;\n"
        b"SELECT pg_catalog.setval(pg_get_serial_sequence('asset','id'), 19);\n"
    )


class TestDrillEndToEnd(unittest.TestCase):
    def run_drill(self, asset_count, expected, now="2026-08-25T00:00:00Z", extra=None):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fake_restic = FakeRestic(root, make_dump_gz())
            fake_pg = FakePostgresTools(root, asset_count)
            scratch = root / "scratch"
            work = root / "work"
            env = {
                "PATH": f"{root}:{os.environ.get('PATH', '')}",
                "RESTIC_REPOSITORY": "b2:hikuohiku-homelab:immich",
                "RESTIC_PASSWORD": "pw",
                "B2_ACCOUNT_ID": "id",
                "B2_ACCOUNT_KEY": "key",
                "HOME": str(root),
            }
            old = os.environ.copy()
            os.environ.update(env)
            try:
                argv = [
                    "--scratch-dir", str(scratch),
                    "--work-dir", str(work),
                    "--restic-binary", str(fake_restic.bin),
                    "--expected-photo-count", str(expected),
                    "--now", now,
                ]
                if extra:
                    argv += extra
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = imd.main(argv)
                report = json.loads((work / "report.json").read_text()) if (work / "report.json").exists() else None
                return rc, report, buf.getvalue()
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_success_matches_production_count(self):
        rc, report, out = self.run_drill(19, 19)
        self.assertEqual(rc, 0, out)
        self.assertIsNotNone(report)
        self.assertEqual(report["photo_count"], 19)
        self.assertEqual(report["expected_photo_count"], 19)
        self.assertTrue(report["photo_count_matches"])
        self.assertTrue(report["postgres_ok"])
        self.assertEqual(report["vchord_version"], "1.1.1")
        self.assertTrue(report["vchord_ok"])
        # driver は API probe をしない (後段の server Job + --probe モードの仕事)。
        # マージして --publish するのは wrapper/人間の役割なので、driver の report は
        # api_status が null のまま完走できることだけをここでは固定する。
        self.assertIsNone(report["api_status"])
        self.assertGreater(report["duration_seconds"], 0)
        self.assertEqual(report["snapshot_id"], "61c022b6")
        self.assertIn("scratch", report["target"])
        self.assertIn("REPORT: ", out)

    def test_photo_count_mismatch_fails_closed(self):
        rc, report, out = self.run_drill(19, 42)
        self.assertEqual(rc, 1)
        self.assertIsNotNone(report)
        self.assertFalse(report["photo_count_matches"])
        self.assertIn("不一致", report["error"])

    def test_too_old_snapshot_fails_closed(self):
        rc, report, out = self.run_drill(19, 19, now="2026-09-01T00:00:00Z")
        self.assertEqual(rc, 1)
        self.assertIn("24h", report["error"])


class TestProbeMode(unittest.TestCase):
    """--probe モード: 後段に立てた immich-server の API が 200 を返すかの実測。"""

    def test_probe_returns_200(self):
        buf = io.StringIO()
        with ApiProbe(), redirect_stdout(buf):
            rc = imd.main(["--probe"])
        self.assertEqual(rc, 0)
        self.assertIn('"api_status": 200', buf.getvalue())

    def test_probe_reports_failure(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = imd.main(["--probe", "--api-url", "http://127.0.0.1:1/api/server/ping",
                           "--probe-timeout", "1"])
        self.assertEqual(rc, 1)
        self.assertIn('"api_status": null', buf.getvalue())


if __name__ == "__main__":
    unittest.main()
