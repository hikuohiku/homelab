"""feedback 取り込みの分岐テスト (P-0091 の task-request 分流を含む)。

collect_feedback() は gh と git に依存するので、両方を差し替えて
「note 1 件がどの受け皿に落ちるか」だけを見る。
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from ops.heart import facts

RULES_PATH = Path(__file__).resolve().parents[2] / "rules.json"
with open(RULES_PATH) as f:
    RULES = json.load(f)

CURSORS = {
    "initialized": True,
    "issue_comments_since": "2026-08-01T00:00:00Z",
    "seen_feedback_files": [],
}

NOTE = "ops/feedback/inbox/note-1.json"


class FakeGh:
    def issue_comments_since(self, issue, since):
        return []


def note_raw(kind=None, body="vaultwarden を最新化して"):
    doc = {"body": body}
    if kind is not None:
        doc["kind"] = kind
    return json.dumps(doc, ensure_ascii=False)


def collect_with_note(raw):
    with (
        mock.patch.object(facts, "_list_feedback_files", return_value=[NOTE]),
        mock.patch.object(facts.gitutil, "show", return_value=raw),
    ):
        return facts.collect_feedback(
            FakeGh(), "/repo", dict(CURSORS), RULES, 56, "ops-feedback"
        )


class TestCollectFeedbackTaskRequest(unittest.TestCase):
    def test_task_request_is_diverted_not_review_needed(self):
        """kind: task-request の note は briefing (review_needed) に落とさず
        未処理キュー行きとして返す (P-0091)。"""
        vetoes, _acks, stop_all, review_needed, resume_all, reqs, _approves, _ = collect_with_note(
            note_raw(kind="task-request")
        )
        self.assertEqual(reqs, [{"source": NOTE, "body": "vaultwarden を最新化して"}])
        self.assertEqual(review_needed, [])
        self.assertFalse(stop_all)

    def test_plain_note_still_goes_to_review(self):
        """kind 無しの従来型書き置きは今までどおり review_needed。"""
        _, _, _, review_needed, _, reqs, _, _ = collect_with_note(
            note_raw(body="ダッシュボードの配色が見づらい")
        )
        self.assertEqual(reqs, [])
        self.assertEqual(len(review_needed), 1)
        self.assertEqual(review_needed[0]["source"], NOTE)

    def test_other_kind_values_are_not_diverted(self):
        """未知の kind は分流せず通常経路 (将来の kind 追加を壊さない)。"""
        _, _, _, review_needed, _, reqs, _, _ = collect_with_note(
            note_raw(kind="something-else")
        )
        self.assertEqual(reqs, [])
        self.assertEqual(len(review_needed), 1)

    def test_stop_keyword_in_task_request_wins(self):
        """停止系キーワードは task-request より先 (P-0090 の決定論パススルー)。
        「止めて」を依頼文に混ぜられても全停止は潰されない。"""
        raw = note_raw(kind="task-request", body="全部やめて、それと掃除して")
        vetoes, _acks, stop_all, review_needed, resume_all, reqs, _approves, _ = collect_with_note(raw)
        self.assertTrue(stop_all)
        self.assertEqual(reqs, [])
        self.assertEqual(review_needed, [])

    def test_veto_keyword_in_task_request_wins(self):
        raw = note_raw(kind="task-request", body="veto P-0012。あと日報を作って")
        vetoes, _acks, stop_all, _, _, reqs, _approves, _ = collect_with_note(raw)
        self.assertEqual(vetoes, ["P-0012"])
        self.assertEqual(reqs, [])

    def test_malformed_json_is_treated_as_free_text(self):
        """JSON として壊れた note は生テキスト扱い (従来どおり)。"""
        _, _, _, review_needed, _, reqs, _, _ = collect_with_note("ただのテキスト")
        self.assertEqual(reqs, [])
        self.assertEqual(len(review_needed), 1)


if __name__ == "__main__":
    unittest.main()
