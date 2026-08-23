"""ops/tools/wish_seeds.py (P-0192) の純関数と受信側契約を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_wish_seeds`。

固定する契約:

- 問いかけ本文は spec DoD (1) の固定文言そのもの (挨拶・装飾を足さない)
- 送信は sendMessage 1 通のみ。証跡ファイルが既にあるときは二度と送らない
  (verify が直接見ない「再実行で二重送信しない」の歯止め)
- 証跡は message_id / sent_at を機械可読に持つ (verify 2 の要求形)
- 返答 → seed 変換は PROJECT.md 作り方 4 の 3 系列を固定する:
  「返信あり」「ゼロ件 (沈黙)」「veto 語が本文に混じる通常文」
- 返信の切り分け (replies_after): 募集送信より前の inbox note は返信と数えない
  (2026-08-23 実測で送信前に無関係な telegram note が既に inbox にあるため)。
  沈黙記録の日付は evidence の sent_at 由来のみで、推測では焼かない
- 受信側の取り込み契約 (DoD (2)): telegram-adapter の note は **kind フィールドを
  持たない**。collect_feedback がそれを通常の feedback (review_needed) として
  取り込めることを実物サンプルと同じ形の fixture で実証する
  (対応付けの追加が不要だったことの証跡。ops-feedback ブランチ本体は触らない)
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.heart import facts, triage
from ops.tools import wish_seeds as ws

RULES = ws.load_rules()


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def telegram_note(note_id, received, body):
    """origin/ops-feedback の実物サンプルと同じ欄構成 (kind は持たない)。"""
    return {"id": note_id, "source": "telegram", "received": received, "body": body}


# 実物サンプル origin/ops-feedback:ops/feedback/inbox/20260823-120317-1e88e232.json
REAL_NOTE = telegram_note(
    "20260823-120317-1e88e232", "2026-08-23T12:03:17Z", "こんにちさ"
)


class TestAskAndSend(unittest.TestCase):
    def test_ask_text_is_spec_wording(self):
        self.assertEqual(
            ws.compose_ask(),
            "生活で面倒に感じていることを上位 3 つ教えてください "
            "(homelab で自動化できそうなもの)",
        )

    def test_send_telegram_posts_sendmessage_once_and_returns_payload(self):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return FakeResponse({"ok": True, "result": {"message_id": 123}})

        payload = ws.send_telegram("TOKEN", "42", ws.compose_ask(), urlopen=fake_urlopen)
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertIn("/botTOKEN/sendMessage", request.full_url)
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data.decode())
        self.assertEqual(body, {"chat_id": "42", "text": ws.compose_ask()})
        self.assertEqual(payload["result"]["message_id"], 123)

    def test_send_telegram_raises_when_not_ok(self):
        with self.assertRaises(RuntimeError):
            ws.send_telegram(
                "TOKEN", "42", "x",
                urlopen=lambda *a, **k: FakeResponse({"ok": False, "description": "no"}),
            )

    def test_build_evidence_satisfies_verify_shape(self):
        evidence = ws.build_evidence(message_id=123, chat_id="42",
                                     sent_at="2026-08-23T12:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ask-evidence.json"
            with open(path, "w") as f:
                json.dump(evidence, f)
            # wrapper が回す verify 2 と同じ判定
            with open(path) as f:
                d = json.load(f)
            self.assertTrue(d.get("message_id"))
            self.assertTrue(d.get("sent_at"))


class TestDoubleSendGuard(unittest.TestCase):
    def test_already_sent_follows_file_existence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ask-evidence.json"
            self.assertFalse(ws.already_sent(path))
            path.write_text("{}")
            self.assertTrue(ws.already_sent(path))

    def test_main_refuses_to_send_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ask-evidence.json"
            path.write_text('{"message_id": 1}')
            sent = []
            with mock.patch.object(ws, "send_telegram",
                                   side_effect=lambda *a, **k: sent.append(a)):
                rc = ws.main(["--send", "--evidence", str(path)])
            self.assertEqual(rc, 1)
            self.assertEqual(sent, [])

    def test_main_dry_run_is_default_and_never_sends(self):
        sent = []
        with mock.patch.object(ws, "send_telegram",
                               side_effect=lambda *a, **k: sent.append(a)):
            rc = ws.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [])

    def test_main_send_writes_evidence_from_telegram_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "ask-evidence.json"
            response = {"ok": True, "result": {"message_id": 777}}
            env = {
                "TELEGRAM_BOT_TOKEN": "t",
                "TELEGRAM_ALLOWED_USER_ID": "42",
            }
            with mock.patch.object(ws, "send_telegram", return_value=response) as st,\
                 mock.patch.dict(os.environ, env):
                rc = ws.main(["--send", "--evidence", str(path)])
            self.assertEqual(rc, 0)
            st.assert_called_once()
            with open(path) as f:
                d = json.load(f)
            self.assertEqual(d["message_id"], 777)
            self.assertTrue(d["sent_at"])

    def test_main_send_without_credentials_fails_before_sending(self):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ask-evidence.json"
            clean = {k: "" for k in ("TELEGRAM_BOT_TOKEN",
                                     "TELEGRAM_ALLOWED_USER_ID")}
            with mock.patch.object(ws, "send_telegram",
                                   side_effect=lambda *a, **k: sent.append(a)),\
                 mock.patch.dict(os.environ, clean):
                rc = ws.main(["--send", "--evidence", str(path)])
            self.assertEqual(rc, 1)
            self.assertEqual(sent, [])
            self.assertFalse(path.exists())
class TestRenderSeedsSection(unittest.TestCase):
    def test_replies_become_one_line_each_with_provenance(self):
        notes = [
            REAL_NOTE,
            telegram_note(
                "note-2", "2026-08-24T01:00:00Z",
                "朝の服選びが面倒\n傷の絆創膏の在庫管理",
            ),
        ]
        section = ws.render_seeds_section(notes, rules=RULES)
        self.assertTrue(section.startswith(ws.SECTION_TITLE))
        self.assertIn("人間の要望 (2026-08 募集より)", section)
        bullets = [ln for ln in section.splitlines() if ln.startswith("- ")]
        self.assertEqual(bullets, [
            "- こんにちさ",
            "- 朝の服選びが面倒",
            "- 傷の絆創膏の在庫管理",
        ])

    def test_silence_is_recorded_as_observation(self):
        section = ws.render_seeds_section(
            [], rules=RULES, sent_at="2026-08-24T09:30:00Z")
        self.assertIn("人間の要望", section)
        self.assertIn("2026-08-24 に募集を送ったが", section)
        self.assertIn("返信 0 件", section)
        self.assertIn("聞いたこと・返ってこなかったこと", section)
        self.assertFalse([ln for ln in section.splitlines() if ln.startswith("- ")])

    def test_silence_without_sent_at_refuses_to_guess_the_date(self):
        with self.assertRaises(ValueError):
            ws.render_seeds_section([], rules=RULES)

    def test_veto_word_inside_long_normal_sentence_is_promoted(self):
        body = (
            "冷蔵庫の在庫を覚えておいてほしい。買い物のとき何を止めておくべきか"
            "分からなくてよく同じものを買ってしまうので、在庫の自動記録をしてほしい"
        )
        verdict = triage.classify(body, RULES)
        self.assertEqual(verdict["kind"], "review_needed")
        section = ws.render_seeds_section(
            [telegram_note("n1", "2026-08-24T02:00:00Z", body)], rules=RULES)
        self.assertIn(body[:20], section)
        self.assertNotIn("昇格を見送った返信", section)

    def test_short_stop_reply_is_not_promoted_but_listed(self):
        section = ws.render_seeds_section(
            [telegram_note("n1", "2026-08-24T03:00:00Z", "全部やめて")],
            rules=RULES)
        self.assertIn("昇格を見送った返信", section)
        self.assertIn("stop_all", section)
        self.assertNotIn("- 全部やめて", section)

    def test_veto_pattern_reply_is_flagged(self):
        section = ws.render_seeds_section(
            [telegram_note("n1", "2026-08-24T04:00:00Z", "veto P-0001 とりあえず")],
            rules=RULES)
        self.assertIn("veto", section)
        self.assertNotIn("とりあえず", section.replace(
            "「veto P-0001 とりあえず」", ""))


class TestReplySelection(unittest.TestCase):
    """送信前 note を「返信」として採らない歯止め (replies_after)。

    2026-08-23 実測: 募集送信前に inbox には無関係な telegram note が既にあり
    (挨拶系)、全 notes を素通しで render_seeds_section に渡すと誤昇格する。
    """

    SENT_AT = "2026-08-23T12:30:00Z"

    def test_pre_ask_notes_are_dropped_and_replies_kept(self):
        pre = telegram_note("20260823-120317-1e88e232", "2026-08-23T12:03:17Z",
                            "こんにちさ")
        reply = telegram_note("r1", "2026-08-23T13:00:00Z", "服選びが面倒")
        self.assertEqual(
            ws.replies_after([pre, reply], self.SENT_AT), [reply])

    def test_same_second_as_send_is_not_a_reply(self):
        boundary = telegram_note("b1", "2026-08-23T12:30:00Z", "same second")
        self.assertEqual(ws.replies_after([boundary], self.SENT_AT), [])

    def test_unparseable_or_missing_received_fails_loud(self):
        with self.assertRaises(ValueError):
            ws.replies_after([{"id": "x", "body": "時刻が無い"}], self.SENT_AT)
        with self.assertRaises(ValueError):
            ws.replies_after(
                [telegram_note("y", "not-a-time", "壊れた時刻")], self.SENT_AT)
        with self.assertRaises(ValueError):
            ws.replies_after([], "also-not-a-time")

    def test_documented_recipe_cutoff_then_render(self):
        """PROGRESS 記載の定石 (cutoff → render) を 1 本で固定する。"""
        pre = telegram_note("pre", "2026-08-23T12:03:17Z", "こんにちさ")
        reply = telegram_note("r1", "2026-08-24T01:00:00Z", "ごみ出しリマインダー")
        section = ws.render_seeds_section(
            ws.replies_after([pre, reply], self.SENT_AT),
            rules=RULES, sent_at=self.SENT_AT,
        )
        self.assertNotIn("こんにちさ", section)
        self.assertIn("- ごみ出しリマインダー", section)


NOTE_PATH = "ops/feedback/inbox/note-1.json"


def collect_with_note(raw):
    """test_facts.py と同じ差し替えで collect_feedback だけを見る。"""

    class FakeGh:
        def issue_comments_since(self, issue, since):
            return []

    with (
        mock.patch.object(facts, "_list_feedback_files", return_value=[NOTE_PATH]),
        mock.patch.object(facts.gitutil, "show", return_value=raw),
    ):
        cursors = {
            "initialized": True,
            "issue_comments_since": "2026-08-01T00:00:00Z",
            "seen_feedback_files": [],
        }
        return facts.collect_feedback(
            FakeGh(), "/repo", cursors, RULES, 56, "ops-feedback"
        )


class TestIngestionDryRun(unittest.TestCase):
    """DoD (2) の dry-run: telegram 由来 note (kind 無し) が通常 feedback として
    取り込めること。対応付け追加が不要だったことの実証。"""

    def test_real_shaped_note_lands_in_review_needed(self):
        raw = json.dumps(REAL_NOTE, ensure_ascii=False)
        vetoes, acks, stop_all, review_needed, resume_all, reqs, _ = \
            collect_with_note(raw)
        self.assertEqual(len(review_needed), 1)
        self.assertEqual(review_needed[0]["source"], NOTE_PATH)
        self.assertEqual(review_needed[0]["body"], "こんにちさ")
        self.assertEqual((vetoes, acks), ([], []))
        self.assertFalse(stop_all)
        self.assertFalse(resume_all)
        self.assertEqual(reqs, [])

    def test_wish_list_reply_also_lands_in_review_needed(self):
        body = "1. 朝の服選び\n2. 傷の絆創膏の在庫管理\n3. ごみ出しの曜日リマインダー"
        raw = json.dumps(telegram_note("note-9", "2026-08-24T05:00:00Z", body),
                         ensure_ascii=False)
        _, _, _, review_needed, _, _, _ = collect_with_note(raw)
        self.assertEqual(len(review_needed), 1)


if __name__ == "__main__":
    unittest.main()
