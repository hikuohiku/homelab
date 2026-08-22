"""apps/openclaw/bridge.py (P-0107 決定論パススルー) の純関数を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_openclaw_bridge`。
bridge.py は sidecar コンテナ内で ConfigMap から直接起動される単一ファイルのため
パッケージではなく、テストからは importlib で実ファイルをロードする
(ConfigMap に入る「そのもの」を検査している。コピー先の再検査はしない)。

固定する契約:
- 受信レコード (channel_ingress_events の payload) → inbox note 形式への変換
  ({id, source: "telegram", received, body}。id / received は dashboard route.ts と同型)
- 生テキスト保存 (trim 等の加工をしない)
- allowlist は fail-closed (env 未設定・非数値なら誰も許可しない)
- kind: task-request は付けない (spec 明記の禁じ手)
- run_once() 1 tick の統合経路 (実 SQLite WAL 読み取り → JSON 変換 → 実 HTTP の
  Contents API PUT) も localhost の偽 API サーバで実際に走らせる。ただしこれは
  DoD「実メッセージ 1 通の実測」の代替ではない (cluster + 人間の送信が必要)
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO / "apps" / "openclaw" / "bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("openclaw_bridge_under_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()

# 実物と同じ形の payload (telegram-ingress-spool.ts の SPOOL_VERSION=1)。
# update 部は Telegram getUpdates の生レコードの主要フィールドを抜いた合成 fixture
def make_payload(update_id, received_at_ms, message=None, version=1):
    return {
        "version": version,
        "updateId": update_id,
        "receivedAt": received_at_ms,
        "update": {"update_id": update_id, "message": message},
    }


def dm_message(sender_id_value, text):
    return {"message_id": 1, "from": {"id": sender_id_value, "is_bot": False}, "text": text}


class ExtractMessageTextTest(unittest.TestCase):
    def test_text_to_raw(self):
        # trim しない。生テキスト保存が絶対条件
        text = bridge.extract_message_text({"message": dm_message(7, "  止めてください \n")})
        self.assertEqual(text, "  止めてください \n")

    def test_caption_fallback(self):
        update = {"message": {"from": {"id": 7}, "caption": "写真の説明"}}
        self.assertEqual(bridge.extract_message_text(update), "写真の説明")

    def test_media_without_text_is_none(self):
        update = {"message": {"from": {"id": 7}, "sticker": {"emoji": "🐱"}}}
        self.assertIsNone(bridge.extract_message_text(update))

    def test_whitespace_only_is_none(self):
        update = {"message": {"from": {"id": 7}, "text": "   "}}
        self.assertIsNone(bridge.extract_message_text(update))

    def test_non_message_updates_are_ignored(self):
        self.assertIsNone(bridge.extract_message_text({"edited_message": dm_message(7, "x")}))
        self.assertIsNone(bridge.extract_message_text({"channel_post": {"text": "x"}}))
        self.assertIsNone(bridge.extract_message_text({"callback_query": {"data": "x"}}))
        self.assertIsNone(bridge.extract_message_text("not a dict"))
        self.assertIsNone(bridge.extract_message_text(None))


class AllowlistTest(unittest.TestCase):
    def test_match(self):
        update = {"message": dm_message(123456789, "hello")}
        self.assertTrue(bridge.is_allowed_sender(update, "123456789"))

    def test_mismatch(self):
        update = {"message": dm_message(111, "hello")}
        self.assertFalse(bridge.is_allowed_sender(update, "222"))

    def test_missing_sender_fails_closed(self):
        update = {"message": {"message_id": 1, "text": "hello"}}
        self.assertFalse(bridge.is_allowed_sender(update, "123"))

    def test_unset_env_fails_closed(self):
        update = {"message": dm_message(123, "hello")}
        for bad in (None, "", "   "):
            self.assertFalse(bridge.is_allowed_sender(update, bad), repr(bad))

    def test_non_numeric_env_fails_closed(self):
        update = {"message": dm_message(123, "hello")}
        self.assertFalse(bridge.is_allowed_sender(update, "@user"))
        self.assertFalse(bridge.is_allowed_sender(update, "12o3"))


class NoteFormatTest(unittest.TestCase):
    RECEIVED_MS = 1755888000000  # 2025-08-22T18:40:00Z

    def test_fields_and_source(self):
        note = bridge.build_note(self.RECEIVED_MS, "本文")
        self.assertEqual(
            {"id", "source", "received", "body"}, set(note.keys())
        )
        self.assertEqual(note["source"], "telegram")
        self.assertEqual(note["received"], "2025-08-22T18:40:00Z")
        self.assertEqual(note["body"], "本文")

    def test_no_kind_field(self):
        # kind: task-request の付与は spec 明記の禁じ手 (依頼らしさ判定になる)
        note = bridge.build_note(self.RECEIVED_MS, "P-0101 を進めて")
        self.assertNotIn("kind", note)

    def test_id_format_follows_dashboard_original(self):
        # route.ts newNoteId(): YYYYMMDD-HHMMSS-<3byte hex>
        note = bridge.build_note(self.RECEIVED_MS, "x", hex6="0a1b2c")
        self.assertEqual(note["id"], "20250822-184000-0a1b2c")

    def test_random_hex6_when_omitted(self):
        note = bridge.build_note(self.RECEIVED_MS, "x")
        suffix = note["id"].rsplit("-", 1)[1]
        self.assertEqual(len(suffix), 6)
        int(suffix, 16)

    def test_render_matches_route_ts_shape(self):
        note = bridge.build_note(self.RECEIVED_MS, "本文", hex6="beef01")
        rendered = bridge.render_note_json(note)
        self.assertTrue(rendered.endswith("\n"))
        # route.ts と同じ indent=1。JSON としては等価であることを roundtrip で固定
        self.assertEqual(json.loads(rendered), note)
        self.assertEqual(rendered.splitlines()[1], ' "id": "20250822-184000-beef01",')


class ParseEventRowTest(unittest.TestCase):
    def test_ok(self):
        row = ("0000000000000042", json.dumps(make_payload(42, 1755888000000)))
        event = bridge.parse_event_row(row)
        self.assertEqual(event["update_id"], 42)
        self.assertEqual(event["received_at_ms"], 1755888000000)
        self.assertIn("update", event)

    def test_bad_version_is_skipped(self):
        row = ("0000000000000042", json.dumps(make_payload(42, 1000, version=99)))
        self.assertIsNone(bridge.parse_event_row(row))

    def test_broken_json_is_skipped(self):
        self.assertIsNone(bridge.parse_event_row(("0000000000000042", "{oops")))

    def test_non_numeric_update_id_is_skipped(self):
        payload = make_payload(42, 1000)
        payload["updateId"] = "42"
        self.assertIsNone(bridge.parse_event_row(("0000000000000042", json.dumps(payload))))

    def test_malformed_row_shape_is_skipped(self):
        self.assertIsNone(bridge.parse_event_row((None, None)))


class SelectEventsTest(unittest.TestCase):
    ALLOWED = "42"

    @staticmethod
    def row_for(event_id, update_id, received_at_ms, message, channel_status="completed"):
        # status 列は読まない (墓石込みで走査する設計)。形の覚書として受渡しするだけ
        return (
            f"{event_id:016d}",
            json.dumps(make_payload(update_id, received_at_ms, message)),
            channel_status,
        )

    def test_selects_new_events_in_update_order(self):
        rows = [
            self.row_for(3, 3, 3000, dm_message(42, "三通目")),
            self.row_for(2, 2, 2000, dm_message(42, "二通目")),
            self.row_for(1, 1, 1000, dm_message(42, "一通目")),
        ]
        events = bridge.select_events(rows, 0, self.ALLOWED)
        self.assertEqual([e["update_id"] for e in events], [1, 2, 3])

    def test_cursor_skips_old_and_keeps_new(self):
        rows = [
            self.row_for(4, 4, 4000, dm_message(42, "new")),
            self.row_for(2, 2, 2000, dm_message(42, "old")),
        ]
        events = bridge.select_events(rows, 2, self.ALLOWED)
        self.assertEqual([e["update_id"] for e in events], [4])

    def test_dedupes_duplicate_event_ids(self):
        duplicate = self.row_for(5, 5, 5000, dm_message(42, "once"))
        events = bridge.select_events([duplicate, duplicate], 0, self.ALLOWED)
        self.assertEqual(len(events), 1)

    def test_filters_disallowed_sender(self):
        rows = [self.row_for(6, 6, 6000, dm_message(999, "外部の人間"))]
        self.assertEqual(bridge.select_events(rows, 0, self.ALLOWED), [])

    def test_filters_unset_allowlist(self):
        rows = [self.row_for(6, 6, 6000, dm_message(42, "hello"))]
        self.assertEqual(bridge.select_events(rows, 0, None), [])

    def test_filters_rows_without_text(self):
        sticker_message = {"from": {"id": 42}, "sticker": {"emoji": "🐱"}}
        rows = [self.row_for(7, 7, 7000, sticker_message)]
        self.assertEqual(bridge.select_events(rows, 0, self.ALLOWED), [])

    def test_body_is_verbatim(self):
        raw = "veto P-0090\n\n本題。ここもそのまま残ってほしい  "
        rows = [self.row_for(8, 8, 8000, dm_message(42, raw))]
        events = bridge.select_events(rows, 0, self.ALLOWED)
        self.assertEqual(events[0]["body"], raw)


class QueueNameContractTest(unittest.TestCase):
    def test_default_queue_name_matches_image_measurement(self):
        # registry-D1_pYg_a.js: queue_name = JSON.stringify([pluginId, accountId])。
        # pluginId=telegram / accountId 未指定時の既定値 "default"。image を上げて
        # この前提が崩れたとき、ここが最初に落ちるように固定しておく
        self.assertEqual(bridge.QUEUE_NAME, '["telegram","default"]')

    def test_state_db_points_into_pvc_state_dir(self):
        self.assertEqual(bridge.STATE_DB, "/home/node/.openclaw/state/openclaw.sqlite")


# --- 統合テスト: run_once() を実 SQLite (WAL) × 実 HTTP で通す ---
#
# review 指摘 (P-0107): 「sqlite 読み取り → JSON 変換 → Contents API PUT という統合経路は
# unit テスト以外で一度も走っていない」対策。gateway が開いたままの writer 接続
# (journal_mode=WAL, checkpoint 未実施 = -wal に未反映分が残る状態) への mode=ro 読み取りと、
# localhost の偽 GitHub API への実 HTTP リクエストで、本物の run_once() を動かす。
# 実 Telegram メッセージによる DoD 実測 (cluster + allowlist 内ユーザーの送信) の代替では
# ない。統合経路が壊れていないことの機械的保証と、CI での回帰検知が目的。


class _FakeGitHubHandler(BaseHTTPRequestHandler):
    """bridge.py が使う範囲 (git ref 取得/作成 + contents PUT) だけ模す。

    state 辞書を server に持たせ、テストから保存結果と受付リクエストを検査する。
    """

    def log_message(self, *args):  # テスト出力を汚さない
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        st = self.server.state
        match = re.fullmatch(r"/repos/[^/]+/[^/]+/git/ref/heads/(.+)", self.path)
        if match:
            branch = match.group(1)
            if branch == st["base_branch"] or branch in st["branches"]:
                return self._send(200, {"object": {"sha": st["shas"][branch]}})
            return self._send(404, {"message": "Not Found"})
        return self._send(404, {"message": "Not Found"})

    def do_POST(self):
        st = self.server.state
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == f"/repos/{st['repo']}/git/refs":
            branch = payload["ref"].removeprefix("refs/heads/")
            st["branches"][branch] = payload["sha"]
            st["requests"].append({"method": "POST", "path": self.path, **payload})
            return self._send(201, {"ref": payload["ref"]})
        return self._send(404, {"message": "Not Found"})

    def do_PUT(self):
        st = self.server.state
        match = re.fullmatch(rf"/repos/{re.escape(st['repo'])}/contents/(.+)", self.path)
        if not match:
            return self._send(404, {"message": "Not Found"})
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        st["requests"].append({"method": "PUT", "path": match.group(1), **payload})
        if st["fail_puts"] > 0:
            st["fail_puts"] -= 1
            return self._send(500, {"message": "boom"})
        path = match.group(1)
        if path in st["files"]:
            # 実 API 同型: 同名ファイル既存は 422 (put_note の id 振り直し経路)
            return self._send(422, {"message": "already exists"})
        st["files"][path] = base64.b64decode(payload["content"]).decode()
        return self._send(201, {"commit": {"sha": f"fake-{len(st['files'])}"}})


class EndToEndRunOnceTest(unittest.TestCase):
    ALLOWED_USER_ID = "42"

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="openclaw-bridge-e2e-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.db_path = os.path.join(tmp, "openclaw.sqlite")
        self.cursor_path = os.path.join(tmp, "bridge-cursor.json")

        # gateway 側の writer 接続 (開きっぱなし = WAL checkpoint されない状態を再現)
        self.writer = sqlite3.connect(self.db_path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute(
            "CREATE TABLE channel_ingress_events ("
            "event_id TEXT PRIMARY KEY, queue_name TEXT NOT NULL, status TEXT NOT NULL, "
            "payload_json TEXT NOT NULL)"
        )
        mode = self.writer.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal", "テスト前提: spool DB は WAL で作る")

        state = {
            "repo": bridge.REPO,
            "base_branch": "main",
            "branches": {},
            "shas": {},
            "files": {},
            "requests": [],
            "fail_puts": 0,
        }
        for name in ("main", bridge.BASE_BRANCH):
            state["shas"][name] = f"sha-{name}"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGitHubHandler)
        self.server.state = state
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop_server)
        self.state = state

        # bridge の IO 先を実物から localhost / 一時ディレクトリへ向ける。
        # テストモジュールで共有する module global のため、必ず元に戻す
        self._originals = {n: getattr(bridge, n) for n in ("API", "STATE_DB")}
        self.addCleanup(self._restore_globals)
        bridge.API = f"http://127.0.0.1:{self.server.server_port}"
        bridge.STATE_DB = self.db_path

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def _restore_globals(self):
        for name, value in self._originals.items():
            setattr(bridge, name, value)
        bridge.BRANCH_READY = False
        bridge.HEAD_ATTEMPTS.clear()
        bridge._LAST_LOG_MESSAGE = None

    def spool(self, event_id, update_id, received_at_ms, message, status="pending"):
        """gateway が update を spool した直後の状態を作る (commit 済み、接続は開いたまま)。"""
        self.writer.execute(
            "INSERT INTO channel_ingress_events VALUES (?, ?, ?, ?)",
            (
                f"{event_id:016d}",
                bridge.QUEUE_NAME,
                status,
                json.dumps(make_payload(update_id, received_at_ms, message)),
            ),
        )
        self.writer.commit()

    def saved_paths(self):
        return sorted(self.state["files"])

    def inbox_path(self, note_id):
        return f"{bridge.INBOX_DIR}/{note_id}.json"

    def test_first_run_marks_history_read_without_saving(self):
        self.spool(1, 1, 1755888000000, dm_message(42, "過去ログ。初回起動では保存しない"))
        with contextlib.redirect_stdout(io.StringIO()):
            advanced = bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.assertTrue(advanced)
        self.assertEqual(self.saved_paths(), [])
        cursor = json.load(open(self.cursor_path))
        self.assertEqual(cursor["last_update_id"], 1)

    def test_new_message_flows_to_inbox_file_over_real_http(self):
        received_ms = 1787392800000
        raw_body = "veto P-0107\n\n全停止してる。応答なし  \n"
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.spool(2, 2, received_ms, dm_message(42, raw_body))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            advanced = bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.assertTrue(advanced)

        paths = self.saved_paths()
        self.assertEqual(len(paths), 1, paths)
        note = json.loads(self.state["files"][paths[0]])
        self.assertEqual(set(note.keys()), {"id", "source", "received", "body"})
        self.assertNotIn("kind", note)
        self.assertEqual(note["source"], "telegram")
        self.assertEqual(note["received"], bridge.format_received(received_ms))
        self.assertEqual(note["body"], raw_body)  # trim 等の加工無し (絶対条件)
        self.assertEqual(paths[0], self.inbox_path(note["id"]))
        self.assertRegex(paths[0], rf"^{bridge.INBOX_DIR}/{bridge.id_stamp(received_ms)}-[0-9a-f]{{6}}\.json$")

        puts = [r for r in self.state["requests"] if r["method"] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["branch"], bridge.BRANCH)
        self.assertEqual(puts[0]["message"], f"feedback {note['id']} (telegram)")

        # pod ログで grep する行と同じもの (`logs -c feedback-bridge | grep saved`)
        self.assertIn(f"saved {bridge.BRANCH}:{paths[0]} (update 2, {len(raw_body)} chars)", stdout.getvalue())
        cursor = json.load(open(self.cursor_path))
        self.assertEqual(cursor["last_update_id"], 2)

    def test_rerun_after_save_is_noop(self):
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.spool(3, 3, 1755888000000, dm_message(42, "一回だけ"))
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        with contextlib.redirect_stdout(io.StringIO()):
            advanced = bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.assertFalse(advanced)
        self.assertEqual(len(self.saved_paths()), 1)

    def test_disallowed_sender_never_reaches_github(self):
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.spool(4, 4, 1755888000000, dm_message(999, "allowlist 外の人間"))
        with contextlib.redirect_stdout(io.StringIO()):
            advanced = bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.assertFalse(advanced)
        self.assertEqual(self.saved_paths(), [])
        self.assertFalse([r for r in self.state["requests"] if r["method"] == "PUT"])

    def test_put_failure_keeps_cursor_and_next_tick_retries(self):
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        before = open(self.cursor_path).read()
        self.spool(5, 5, 1755888000000, dm_message(42, "一時障害"))
        self.state["fail_puts"] = 1
        with contextlib.redirect_stdout(io.StringIO()):
            advanced = bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.assertFalse(advanced)
        self.assertEqual(open(self.cursor_path).read(), before)  # cursor は進まない
        self.assertEqual(bridge.HEAD_ATTEMPTS.get(5), 1)
        self.state["fail_puts"] = 0
        with contextlib.redirect_stdout(io.StringIO()):
            advanced = bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.assertTrue(advanced)
        self.assertEqual(len(self.saved_paths()), 1)
        self.assertNotIn(5, bridge.HEAD_ATTEMPTS)

    def test_422_collision_regenerates_id_and_succeeds(self):
        received_ms = 1755888000000
        stamp = bridge.id_stamp(received_ms)
        first_hex, second_hex = "abcdef", "123456"
        seq = iter([int(first_hex, 16), int(second_hex, 16)])
        original_getrandbits = bridge.random.getrandbits
        bridge.random.getrandbits = lambda n: next(seq)
        self.addCleanup(setattr, bridge.random, "getrandbits", original_getrandbits)

        with contextlib.redirect_stdout(io.StringIO()):
            bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        # 先着で同名ファイルがある状態 (hex 衝突)。実 API なら 422 が返る
        self.state["files"][self.inbox_path(f"{stamp}-{first_hex}")] = "先着"
        self.spool(6, 6, received_ms, dm_message(42, "衝突テスト"))
        with contextlib.redirect_stdout(io.StringIO()):
            advanced = bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.assertTrue(advanced)
        puts = [r for r in self.state["requests"] if r["method"] == "PUT"]
        self.assertEqual([r["path"] for r in puts], [self.inbox_path(f"{stamp}-{first_hex}"),
                                                     self.inbox_path(f"{stamp}-{second_hex}")])
        self.assertEqual(json.loads(self.state["files"][self.inbox_path(f"{stamp}-{second_hex}")])["body"],
                         "衝突テスト")
        self.assertEqual(len(self.saved_paths()), 2)  # 先着 + 振り直し後

    def test_missing_state_db_waits_without_error(self):
        bridge.STATE_DB = os.path.join(os.path.dirname(self.db_path), "not-yet.sqlite")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            advanced = bridge.run_once("t", self.ALLOWED_USER_ID, self.cursor_path)
        self.assertFalse(advanced)
        self.assertIn("state DB 未準備", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
