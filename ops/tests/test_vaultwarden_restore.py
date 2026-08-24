"""ops/vaultwarden_restore.py (P-9025) の純粋関数と CLI 契約 (rc) を固定する。

本物の restic / B2 には出ない。restic はフェイクの実行ファイルで代替し、
snapshots/restore/dump の契約だけを模倣する。リポジトリルートから
`python3 -m unittest discover -s ops/tests -t .`。
"""

import datetime
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ops import vaultwarden_restore as vwr

NOW = datetime.datetime(2026, 8, 24, 23, 0, 0, tzinfo=datetime.timezone.utc)
RECENT_TIME = "2026-08-24T18:40:00Z"  # 直近 24h 以内 (03:40 JST backup 相当)


def recent_snapshot(short_id="c51d21fa", time=RECENT_TIME):
    return {"id": short_id + "0" * 32, "short_id": short_id, "time": time, "paths": ["/mnt/vaultwarden-data", "/staging/db.sqlite3"]}


class TestPickLatest(unittest.TestCase):
    def test_picks_latest_by_time(self):
        old = {"id": "a", "time": "2026-08-01T00:00:00Z"}
        new = {"id": "b", "time": "2026-08-24T18:40:00Z"}
        self.assertEqual(vwr.pick_latest([old, new])["id"], "b")

    def test_empty_raises(self):
        with self.assertRaises(RuntimeError):
            vwr.pick_latest([])


class TestSnapshotAge(unittest.TestCase):
    def test_age_within_24h(self):
        snap = recent_snapshot()
        age = vwr.snapshot_age_seconds(snap, NOW)
        self.assertGreater(age, 0)
        self.assertLess(age, 24 * 3600)

    def test_missing_time_raises(self):
        with self.assertRaises(RuntimeError):
            vwr.snapshot_age_seconds({"id": "x"}, NOW)





class TestVerifySqlite(unittest.TestCase):
    def test_integrity_ok_and_rows(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "db.sqlite3"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            con.execute("INSERT INTO users VALUES (1)")
            con.execute("CREATE TABLE ciphers (id INTEGER PRIMARY KEY)")
            con.execute("INSERT INTO ciphers VALUES (1)")
            con.execute("INSERT INTO ciphers VALUES (2)")
            con.commit()
            con.close()
            integrity, rows, users, table_rows = vwr.verify_sqlite(str(db))
            self.assertEqual(integrity, "ok")
            self.assertEqual(rows, 3)
            self.assertEqual(users, 1)
            self.assertEqual(table_rows["ciphers"], 2)


class FakeRestic:
    """snapshots/restore/dump の契約だけを模倣するフェイク restic 実行ファイル。

    snapshot は recent_snapshot() を 1 本返し、restore は vaultwarden の 2 パス構成
    (mnt/vaultwarden-data 本体 + staging/db.sqlite3) を作る。dump は db のバイト列を返す。
    """

    def __init__(self, tmp, db_bytes):
        self.tmp = Path(tmp)
        self.db_bytes = db_bytes
        self.bin = self.tmp / "restic"
        self.bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, shutil, sys\n"
            "args = sys.argv[1:]\n"
            "mode = args[0]\n"
            f"DB = {self.db_bytes!r}\n"
            "SNAP = " + json.dumps(recent_snapshot()) + "\n"
            "if mode == 'snapshots':\n"
            "    print(json.dumps([SNAP]))\n"
            "elif mode == 'restore':\n"
            "    import time\n"
            "    time.sleep(1.1)  # duration_seconds > 0 を決定論的に満たす\n"
            "    sid, target = args[1], args[3]\n"
            "    data = os.path.join(target, 'mnt', 'vaultwarden-data')\n"
            "    os.makedirs(data, exist_ok=True)\n"
            "    open(os.path.join(data, 'config.json'), 'wb').write(b'{}')\n"
            "    open(os.path.join(data, 'rsa_key.pem'), 'wb').write(b'key')\n"
            "    os.makedirs(os.path.join(target, 'staging'), exist_ok=True)\n"
            "    open(os.path.join(target, 'staging', 'db.sqlite3'), 'wb').write(DB)\n"
            "    print('restoring snapshot %s' % sid)\n"
            "    print('Summary: Restored 4 files/dirs (1.000 KiB) in 0:01')\n"
            "elif mode == 'dump':\n"
            "    sys.stdout.buffer.write(DB)\n"
            "else:\n"
            "    sys.exit(3)\n",
            encoding="utf-8",
        )
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IEXEC)


def make_db():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "db.sqlite3"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO users VALUES (1, 'human')")
        con.execute("CREATE TABLE ciphers (id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO ciphers VALUES (1)")
        con.commit()
        con.close()
        return db.read_bytes()


class TestMainEndToEnd(unittest.TestCase):
    def run_main(self, db_bytes, env_extra=None, now="2026-08-24T23:00:00Z", extra=None):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeRestic(d, db_bytes)
            scratch = Path(d) / "scratch"
            summary = Path(d) / "restore-summary.json"
            env = {
                "RESTIC_REPOSITORY": "b2:hikuohiku-homelab:vaultwarden",
                "RESTIC_PASSWORD": "pw",
                "B2_ACCOUNT_ID": "id",
                "B2_ACCOUNT_KEY": "key",
            }
            if env_extra:
                env.update(env_extra)
            old = os.environ.copy()
            os.environ.update(env)
            try:
                argv = [
                    "--scratch-dir", str(scratch),
                    "--summary-path", str(summary),
                    "--restic-binary", str(fake.bin),
                    "--now", now,
                ]
                if extra:
                    argv += extra
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = vwr.main(argv)
                self.stdout = buf.getvalue()
                # 一時ディレクトリは with を抜けると消えるため、観測結果を先に握る
                summary_doc = json.loads(summary.read_text()) if summary.exists() else None
                scratch_state = {
                    "db": (scratch / "db.sqlite3").is_file(),
                    "config": (scratch / "config.json").is_file(),
                    "mnt_gone": not (scratch / "mnt").exists(),
                }
                return rc, summary_doc, scratch_state
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_success_writes_summary_with_verify_keys(self):
        rc, d, scratch = self.run_main(make_db())
        self.assertEqual(rc, 0)
        self.assertIsNotNone(d)
        self.assertEqual(d["integrity"], "ok")
        self.assertGreater(d["rows"], 0)
        self.assertGreater(d["duration_seconds"], 0)
        self.assertIn("scratch", d["target"])
        self.assertTrue(d["checksum_match"])
        self.assertEqual(d["users"], 1)
        # verify コマンドの形がそのまま通ること
        self.assertEqual(d["integrity"], "ok")
        self.assertGreater(d["rows"], 0)
        self.assertGreater(d["duration_seconds"], 0)
        # 復元結果は vaultwarden の /data レイアウト (直下に db.sqlite3) になっている
        self.assertTrue(scratch["db"])
        self.assertTrue(scratch["config"])
        self.assertTrue(scratch["mnt_gone"])

    def test_too_old_snapshot_fails_closed(self):
        rc, d, _ = self.run_main(make_db(), now="2026-09-01T00:00:00Z")
        self.assertEqual(rc, 1)
        self.assertIsNotNone(d)
        self.assertIn("error", d)
        self.assertIsNone(d["rows"])

    def test_integrity_failure_fails_closed(self):
        broken = make_db()[:16]  # SQLite ヘッダだけの破損 DB
        rc, d, _ = self.run_main(broken)
        self.assertEqual(rc, 1)
        self.assertIsNotNone(d)
        self.assertNotEqual(d["integrity"], "ok")
        self.assertIn("error", d)


if __name__ == "__main__":
    unittest.main()