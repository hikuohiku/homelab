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


def telegram_note(body, kind=None, source="telegram"):
    """bridge.build_note (apps/openclaw/bridge.py) が保存するのと同じ形の note。
    trim 等の加工をしないのが bridge の契約なので、ここでも body は生のまま渡す。
    kind は bridge が付けることは無い (P-0107 の禁じ手) が、dashboard 書き置き等の
    上流で付与された場合の collect_feedback 契約を固定するために渡せるようにしてある。
    source は分類への非依存を検証するためだけに差し替えられる (下のクラス参照)。"""
    doc = {
        "id": "20260823-000000-abc123",
        "source": source,
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


class CommentGh:
    """issue コメント 1 件を返すだけの gh スタブ (輸送元比較用)。"""

    def __init__(self, comments):
        self.comments = comments

    def issue_comments_since(self, issue, since):
        return self.comments


def collect_issue_comment(body):
    gh = CommentGh([{"id": "9001", "created_at": "2026-08-23T00:00:00Z", "body": body}])
    with mock.patch.object(facts, "_list_feedback_files", return_value=[]):
        return facts.collect_feedback(
            gh, "/repo", dict(CURSORS), RULES, 56, "ops-feedback"
        )


def verdict_kind(result):
    """collect_feedback の戻り値を分類種別 1 つに潰す (輸送間の突合用)。
    呼び出しごとにメッセージ 1 通だけ流すので、満たされる受け皿は高々 1 つ。"""
    vetoes, _acks, stop_all, review_needed, resume_all, reqs, _ = result
    if stop_all:
        return "stop_all"
    if resume_all:
        return "resume_all"
    if vetoes:
        return "veto"
    if review_needed or reqs:
        return "review_needed"
    return "none"


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


class TestClassificationIgnoresSource(unittest.TestCase):
    """分類は本文のみで決まり、届いた経路 (source) に依存しないことの実検証。

    triage.classify は (text, rules) のみを受け、source を引数に持たない (設計)。
    この契約が壊れると緊急時の「止めて」が経路によって拾われたり漏れたりする —
    たとえば新しい transport 追加時に「分類へ source を渡して分岐する」変更が
    入った場合がそれ。同一本文を telegram note・source 違いの note・issue コメント
    の 3 経路で通し、分類種別が完全一致することに加え、期待種別そのものも同時に
    断言する (片側だけの比較では分類全体が壊れても気づけないため)。"""

    CASES = [
        ("止めて", "stop_all"),
        ("再開", "resume_all"),
        ("veto P-0103", "veto"),
        ("今日はいい天気ですね", "review_needed"),
    ]

    def test_same_body_same_verdict_across_transports(self):
        for body, expected in self.CASES:
            with self.subTest(body=body):
                via_telegram = verdict_kind(
                    collect_telegram_note(telegram_note(body))
                )
                via_dashboard = verdict_kind(
                    collect_telegram_note(telegram_note(body, source="ops-dashboard"))
                )
                via_issue_comment = verdict_kind(collect_issue_comment(body))
                self.assertEqual(
                    (via_telegram, via_dashboard, via_issue_comment),
                    (expected, expected, expected),
                )


if __name__ == "__main__":
    unittest.main()
