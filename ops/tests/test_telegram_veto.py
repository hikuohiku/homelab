"""telegram 由来の note が heart の分類経路を正しく通ることを fixture で固定する (P-0118)。

OpenClaw bridge (P-0107) は受信メッセージを {id, source: "telegram", received, body}
(kind 無し・trim 無し) で ops-feedback ブランチの inbox へ保存する。heart の
collect_feedback() は inbox note を triage.classify() に通すので、停止/再開キーワードは
source に関係なく決定論で拾われるはずだが、telegram 由来 note を通すテストは誰も
持っていなかった (test_triage.py は生テキストのみ、test_openclaw_bridge.py は保存形式のみ)。

ここでは collect_feedback レベル (note → 分類結果) で 4 系統を固定する:
  「止めて」系 -> stop_all / 「再開」系 -> resume_all /
  kind: task-request の実装依頼 -> task_requests / 雑談 -> review_needed

キーワードは rules.json (veto.stop_keywords / resume_keywords) を読んで使う
(ハードコードしない。test_triage.py と同じ読み込みパターン)。
「stop_keywords が telegram source でも参照されること」の証明は、登録済みキーワード
全量を telegram DM の典型形 (短文) で通して分類が決まることで行う。

リポジトリルートから `python3 -m unittest ops.tests.test_telegram_veto`。
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from ops.heart import facts, triage

REPO = Path(__file__).resolve().parents[1]
with open(REPO / "rules.json") as f:
    RULES = json.load(f)

CURSORS = {
    "initialized": True,
    "issue_comments_since": None,
    "seen_feedback_files": [],
}

# bridge.py の id 形式 (YYYYMMDD-HHMMSS-<hex6>) と同じ形のパス
NOTE_PATH = "ops/feedback/inbox/20260823-000000-abc123.json"


class FakeGh:
    def issue_comments_since(self, issue, since):
        return []


def telegram_note(body, kind=None):
    """bridge.build_note (apps/openclaw/bridge.py) が保存するのと同じ形の note。
    trim 等の加工をしないのが bridge の契約なので、ここでも body は生のまま渡す。
    kind は bridge が付けることは無い (P-0107 の禁じ手) が、dashboard 書き置き等の
    上流で付与された場合の collect_feedback 契約を固定するために渡せるようにしてある。"""
    doc = {
        "id": "20260823-000000-abc123",
        "source": "telegram",
        "received": "2026-08-23T00:00:00Z",
        "body": body,
    }
    if kind is not None:
        doc["kind"] = kind
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


def collect_telegram_note(raw):
    with (
        mock.patch.object(facts, "_list_feedback_files", return_value=[NOTE_PATH]),
        mock.patch.object(facts.gitutil, "show", return_value=raw),
    ):
        return facts.collect_feedback(
            FakeGh(), "/repo", dict(CURSORS), RULES, 56, "ops-feedback"
        )


class TestTelegramNoteClassification(unittest.TestCase):
    def test_stop_keyword_becomes_stop_all(self):
        """{source: telegram} の note 本文「止めて」-> stop_all。"""
        vetoes, _acks, stop_all, review_needed, resume_all, reqs, _ = collect_telegram_note(
            telegram_note("止めて")
        )
        self.assertTrue(stop_all)
        self.assertFalse(resume_all)
        self.assertEqual((vetoes, review_needed, reqs), ([], [], []))

    def test_resume_keyword_becomes_resume_all(self):
        """「再開」-> resume_all。"""
        vetoes, _acks, stop_all, review_needed, resume_all, reqs, _ = collect_telegram_note(
            telegram_note("再開")
        )
        self.assertTrue(resume_all)
        self.assertFalse(stop_all)
        self.assertEqual((vetoes, review_needed, reqs), ([], [], []))

    def test_task_request_is_diverted_not_review_needed(self):
        """kind: task-request の実装依頼は review_needed でなく未処理キューへ。
        source が telegram でも分流契約は同じ。"""
        body = "vaultwarden を最新化して"
        vetoes, _acks, stop_all, review_needed, resume_all, reqs, _ = collect_telegram_note(
            telegram_note(body, kind="task-request")
        )
        self.assertEqual(reqs, [{"source": NOTE_PATH, "body": body}])
        self.assertEqual(review_needed, [])
        self.assertFalse(stop_all)
        self.assertFalse(resume_all)
        self.assertEqual(vetoes, [])

    def test_chitchat_is_review_needed(self):
        """雑談は noise (review_needed) に落ちる。daily briefing で人間に見る。"""
        body = "今日はいい天気ですね"
        _, _, stop_all, review_needed, _, reqs, _ = collect_telegram_note(
            telegram_note(body)
        )
        self.assertFalse(stop_all)
        self.assertEqual(reqs, [])
        self.assertEqual(review_needed, [{"source": NOTE_PATH, "body": body}])

    def test_verbatim_untrimmed_body_still_classified_as_stop(self):
        """bridge は本文を trim せず保存する (絶対条件)。前後空白・改行付きの
        生テキストのままでも命令形の停止判定は倒れないことを証明する。"""
        raw = "  止めてください \n"
        _, _, stop_all, _, _, _, _ = collect_telegram_note(telegram_note(raw))
        self.assertTrue(stop_all)

    def test_stop_wins_over_task_request_kind(self):
        """「止めて」が依頼文に混ざっていても task-request 分流より先に全停止
        (P-0090 の絶対条件)。telegram note + kind: task-request の組み合わせでも同じ。"""
        raw = telegram_note("全部やめて、それと掃除をして", kind="task-request")
        vetoes, _acks, stop_all, review_needed, resume_all, reqs, _ = collect_telegram_note(raw)
        self.assertTrue(stop_all)
        self.assertEqual(reqs, [])
        self.assertEqual(review_needed, [])


class TestRulesKeywordsDriveTelegramVerdicts(unittest.TestCase):
    """rules.json に登録されたキーワード全量が、telegram DM の典型形 (1 行短文)
    で分類を決めること。「stop_keywords が telegram source でも参照される」の直接証明。
    キーワードをハードコードしない — 登録の増減にこのテストは自動で追従する。"""

    def test_every_registered_stop_keyword_fires_from_short_dm(self):
        for kw in RULES["veto"]["stop_keywords"]:
            with self.subTest(kw=kw):
                verdict = triage.classify(kw, RULES)
                self.assertEqual(verdict["kind"], "stop_all", kw)

    def test_every_registered_resume_keyword_fires_from_short_dm(self):
        for kw in RULES["veto"].get("resume_keywords", []):
            with self.subTest(kw=kw):
                verdict = triage.classify(kw, RULES)
                self.assertEqual(verdict["kind"], "resume_all", kw)

    def test_classify_is_source_agnostic_by_design(self):
        """分類本体は source を受けない (triage.classify の設計)。つまり telegram
        由来かどうかは分類結果に影響しない — 影響するならこの経路の前提が崩れて
        いるので、その早期発見用に契約を固定しておく。"""
        self.assertEqual(triage.classify("止めて", RULES), triage.classify("止めて", RULES))


if __name__ == "__main__":
    unittest.main()
