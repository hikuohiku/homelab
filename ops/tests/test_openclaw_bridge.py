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
"""

import importlib.util
import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
