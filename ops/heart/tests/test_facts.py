"""feedback 取り込みの分岐テスト (P-0091 の task-request 分流を含む)。

書き置きの経路は issue #56 のコメントとバスのファイルの 2 本
(ops-feedback ブランチは Phase 6 で落とした)。gh を差し替えて
「note 1 件がどの受け皿に落ちるか」だけを見る。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

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

# 「引数を渡さなかった」と「None を渡した」を区別するための番人
_DEFAULT = object()


class FakeGh:
    comments: list = []

    def issue_comments_since(self, issue, since):
        return self.comments


def note_raw(kind=None, body="vaultwarden を最新化して"):
    doc = {"body": body}
    if kind is not None:
        doc["kind"] = kind
    return json.dumps(doc, ensure_ascii=False)


def collect_with_note(raw):
    """バスの inbox に note 1 件だけを置いて collect_feedback を回す。"""
    with tempfile.TemporaryDirectory() as bus:
        (Path(bus) / "note-1.json").write_text(raw)
        return facts.collect_feedback(FakeGh(), dict(CURSORS), RULES, 56, Path(bus))


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


class TestCollectFeedbackFromBus(unittest.TestCase):
    """イベントバス経由の書き置き (移行の段階 3)。

    所有者の「止めて」が GitHub の可用性に依存しないよう、同じ Pod のサイドカーが
    NATS から落としたファイルも読む。ここが守るのは 3 つ:
      - バスから届いた停止指示が同じ決定論で stop_all になる
      - 同じ id が両経路から来ても 1 回しか処理されない
      - バスが死んでいても (ディレクトリが無くても) 既存経路が動く
    """

    def setUp(self):
        self.bus = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bus, True)

    def write_bus_note(self, note_id, body, kind=None):
        doc = {"id": note_id, "source": "telegram", "body": body}
        if kind is not None:
            doc["kind"] = kind
        (self.bus / f"{note_id}.json").write_text(json.dumps(doc, ensure_ascii=False))

    def collect(self, cursors=None, bus_dir=_DEFAULT):
        return facts.collect_feedback(
            FakeGh(), dict(cursors or CURSORS), RULES, 56,
            self.bus if bus_dir is _DEFAULT else bus_dir,
        )

    def test_stop_from_bus_stops_everything(self):
        """バス経由の「止めて」が GitHub を一切経由せずに stop_all になる。
        この 1 本が、緊急停止を第三者の可用性から切り離した証拠。"""
        self.write_bus_note("note-9", "止めて")
        _, _, stop_all, review_needed, _, _, _, cursors = self.collect()
        self.assertTrue(stop_all)
        self.assertEqual(review_needed, [])
        # 既読の鍵は GitHub 経路と同じ形 (ここがずれると重複排除が効かない)
        self.assertIn("ops/feedback/inbox/note-9.json", cursors["seen_feedback_files"])

    def test_seen_key_keeps_the_branch_era_shape(self):
        """既読の鍵は ops-feedback を読んでいた頃と同じ形のまま。
        接頭辞を変えると cursors.json の既読が全部未読に戻り、過去の
        「止めて」を一斉に triage し直す。"""
        self.write_bus_note("note-1", "止めて")
        _, _, stop_all, _, _, _, _, cursors = self.collect()
        self.assertTrue(stop_all)
        self.assertEqual(cursors["seen_feedback_files"], [NOTE])

    def test_already_seen_bus_note_is_not_reprocessed(self):
        """既読のバス note は毎ビート再分類しない (ファイルは掃除まで残る)。"""
        self.write_bus_note("note-1", "止めて")
        cursors = dict(CURSORS, seen_feedback_files=[NOTE])
        _, _, stop_all, review_needed, _, _, _, _ = self.collect(cursors=cursors)
        self.assertFalse(stop_all)
        self.assertEqual(review_needed, [])

    def test_task_request_kind_is_diverted_from_bus_too(self):
        """kind の扱いは経路で変わらない (P-0091 の分流はバス経由でも効く)。"""
        self.write_bus_note("note-2", "vaultwarden を最新化して", kind="task-request")
        _, _, _, review_needed, _, reqs, _, _ = self.collect()
        self.assertEqual(review_needed, [])
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["source"], "ops/feedback/inbox/note-2.json")

    def test_unreadable_bus_dir_does_not_stop_the_issue_route(self):
        """inbox が読めなくても issue #56 の経路は生きる。
        バスの不調で「所有者が GitHub から話しかける」手段まで止めない。"""
        gh = FakeGh()
        gh.comments = [{"id": 1, "created_at": "2026-08-02T00:00:00Z", "body": "止めて"}]
        _, _, stop_all, _, _, _, _, _ = facts.collect_feedback(
            gh, dict(CURSORS), RULES, 56, self.bus / "存在しない",
        )
        self.assertTrue(stop_all)

    def test_unreadable_bus_dir_does_not_advance_the_cursor(self):
        """読めなかったビートは既読を進めない。進めると、復旧後に届いていた
        書き置きを「もう読んだ」ことにして落とす。"""
        _, _, _, _, _, _, _, cursors = self.collect(bus_dir=self.bus / "存在しない")
        self.assertEqual(cursors["seen_feedback_files"], [])

    def test_unreadable_bus_dir_is_reported_not_silently_empty(self):
        """読めないことが警報になる。ここが黙ると「書き置きは無かった」と
        区別がつかないまま自律走行が続く (fail-closed)。"""
        alert = facts.feedback_bus_alert(self.bus / "存在しない")
        self.assertIsNotNone(alert)
        self.assertEqual(alert["status"], "unreadable")
        self.assertIn("存在しない", alert["reason"])

    def test_readable_bus_dir_raises_no_alert(self):
        self.assertIsNone(facts.feedback_bus_alert(self.bus))
        self.write_bus_note("note-7", "こんにちは")
        self.assertIsNone(facts.feedback_bus_alert(self.bus))
        self.assertIsNone(facts.feedback_bus_alert(None))

    def test_bus_dir_none_reads_nothing(self):
        """bus_dir 未設定 (バスを使わない切り戻し構成) はファイルを読まない。"""
        self.write_bus_note("note-3", "止めて")
        _, _, stop_all, _, _, _, _, _ = self.collect(bus_dir=None)
        self.assertFalse(stop_all)

    def test_partial_file_is_not_read(self):
        """サイドカーの一時ファイル (. 始まり) と非 .json は読まない。
        書きかけの JSON を拾うと本文が欠けたまま分類される。"""
        (self.bus / ".tmp-note-4.json").write_text('{"body": "止め')
        (self.bus / "note-5.txt").write_text("止めて")
        _, _, stop_all, review_needed, _, _, _, cursors = self.collect()
        self.assertFalse(stop_all)
        self.assertEqual(review_needed, [])
        self.assertEqual(cursors["seen_feedback_files"], [])

    def test_first_boot_marks_bus_notes_as_read(self):
        """初回起動 (cursor 未初期化) は手元に残っているバス note を triage しない。
        過去の「止めて」が今の停止として効いてしまうため (レビュー指摘 [7] と同じ理由)。"""
        self.write_bus_note("note-6", "止めて")
        _, _, stop_all, _, _, _, _, cursors = self.collect(
            cursors={"initialized": False},
        )
        self.assertFalse(stop_all)
        self.assertEqual(cursors["seen_feedback_files"], ["ops/feedback/inbox/note-6.json"])


if __name__ == "__main__":
    unittest.main()


class TestCollectCommands(unittest.TestCase):
    """コア発の command (events.heart.>) の収集。

    サイドカーが落とすのは書き置きとは別ディレクトリ。混ぜると triage が
    「人間の発話」として誤分類するので、ここは triage を通さない別経路にする。
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, doc):
        (self.dir / name).write_text(
            doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)
        )

    def test_reads_commands_in_name_order(self):
        self.write("core-b.json", {"command_id": "core-b", "type": "task-request",
                                   "body": "後"})
        self.write("core-a.json", {"command_id": "core-a", "type": "task-request",
                                   "body": "先", "title": "題"})
        got = facts.collect_commands(self.dir)
        self.assertEqual([c["command_id"] for c in got], ["core-a", "core-b"])
        self.assertEqual(got[0]["title"], "題")

    def test_missing_dir_is_empty_not_an_error(self):
        """バス経路が無くても heart は止まらない。"""
        self.assertEqual(facts.collect_commands(self.dir / "無い"), [])
        self.assertEqual(facts.collect_commands(None), [])

    def test_partial_and_broken_files_are_skipped(self):
        # サイドカーは rename で置くので書きかけは見えないはずだが、
        # 壊れたものを読んで例外でビートを落とさない
        self.write(".tmp-書きかけ.json", "{")
        self.write("core-broken.json", "{壊れた")
        self.write("core-ok.json", {"command_id": "core-ok", "type": "task-request",
                                    "body": "本文"})
        got = facts.collect_commands(self.dir)
        self.assertEqual([c["command_id"] for c in got], ["core-ok"])

    def test_entries_without_id_or_body_are_skipped(self):
        self.write("a.json", {"type": "task-request", "body": "id が無い"})
        self.write("b.json", {"command_id": "core-b", "type": "task-request"})
        self.write("c.json", {"command_id": "core-c", "body": "type が無い"})
        self.assertEqual(facts.collect_commands(self.dir), [])


class TestCollectCurriculum(unittest.TestCase):
    """立案結果の観測 (設計 rev3 D32 / state-out-of-git 4b-2b)。

    採択も棄却も curriculum Job の result.json に載っている。**GitHub は一切
    見ない** — 台帳 PR は 4b-2b で無くなった。
    """

    SPEC = {"id": "P-0009", "title": "t", "verify": ["false"], "adopted": True}
    REJECTED = {"id": "P-0010", "title": "r", "adopted": False,
                "reject_reason": "同型の再提案"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        (self.data / "projects" / "system").mkdir(parents=True)

    def result(self, **kw):
        doc = {"state": "curriculum_done", "at": "2026-08-25T00:00:00Z",
               "adopted": ["P-0009"]}
        doc.update(kw)
        (self.data / "projects" / "system" / "result.json").write_text(
            json.dumps(doc, ensure_ascii=False)
        )

    def test_adopted_and_rejected_both_come_from_the_result(self):
        self.result(adopted_specs=[self.SPEC], records=[self.SPEC, self.REJECTED])
        out = facts.collect_curriculum(self.data)
        self.assertEqual(out["adopted_specs"], [self.SPEC])
        self.assertEqual([r["id"] for r in out["records"]], ["P-0009", "P-0010"])
        # 取り込み済みの判定は書き込み時刻。PR 番号はもう無い
        self.assertEqual(out["at"], "2026-08-25T00:00:00Z")

    def test_records_missing_is_empty_not_an_error(self):
        """古い result.json (records を持たない) でも採択の登録は進む。"""
        self.result(adopted_specs=[self.SPEC])
        out = facts.collect_curriculum(self.data)
        self.assertEqual(out["records"], [])
        self.assertEqual(out["adopted_specs"], [self.SPEC])

    def test_error_result_carries_no_specs(self):
        self.result(state="error", error="落ちた")
        out = facts.collect_curriculum(self.data)
        self.assertEqual(out["state"], "error")
        self.assertNotIn("adopted_specs", out)
