"""ops/dashboard/build.py のプロジェクト 3 層表示を固定する (P-0044)。

実データ (ops-state の projects.json) は heart が毎時進めるので、そちらを読ませると
明日には落ちる。ここは辞書を直接組んで split_projects() / render_projects() を呼ぶ。

とくに「人間の回答待ちの stalled は現役層に残す」の分岐は、受入 verify が
一切見張っていない (今の台帳に question 系の stalled が 1 件も無いため、
stalled を全部畳んでも verify は green になる)。その穴をここで塞ぐ。

リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`。
"""

import unittest

from ops.dashboard import build

CLOSED_ID = 'id="heart-projects-closed"'
DELIVERED_FOLD = '<details class="fold"><summary>それ以前の'


def project(pid, state, **kw):
    p = {"id": pid, "state": state, "title": f"{pid} の題", "budget": {}}
    p.update(kw)
    return p


def ids(items):
    return [p["id"] for p in items]


def render(projects, specs=None):
    return build.render_projects({"projects": projects}, specs or {})


class TestSplit(unittest.TestCase):
    def test_question_stall_stays_live(self):
        """人間の回答待ちで止まったものは現役層。終端に畳むと催促が消える。"""
        for reason in ("budget_exhausted", "quota_wait_exhausted", "merge_timeout",
                       "pr_closed", "adopt_gate_reject", "adopt_gate_unmeasurable"):
            with self.subTest(reason=reason):
                p = project("P-0100", "stalled", stalled_reason=reason)
                live, delivered, closed = build.split_projects([p])
                self.assertEqual(ids(live), ["P-0100"])
                self.assertEqual(closed, [])
                self.assertEqual(delivered, [])

    def test_dead_ends_are_closed(self):
        """もう誰も動かさないものは終端層。"""
        ps = [
            project("P-0101", "stalled", stalled_reason="spec_error"),
            project("P-0102", "stalled", stalled_reason="error"),
            project("P-0103", "stalled", stalled_reason="human_stop"),
            project("P-0104", "stalled", stalled_reason="review_rejected"),
            project("P-0105", "stalled"),  # reason 欠落も終端に倒す
            project("P-0106", "vetoed"),
        ]
        live, delivered, closed = build.split_projects(ps)
        self.assertEqual(live, [])
        self.assertEqual(delivered, [])
        self.assertEqual(sorted(ids(closed)), [p["id"] for p in ps])

    def test_working_states_are_live(self):
        ps = [project(f"P-02{i:02d}", s) for i, s in enumerate(
            ("proposed", "announced", "active", "in_review", "merging", "soaking"))]
        live, delivered, closed = build.split_projects(ps)
        self.assertEqual(len(live), 6)
        self.assertEqual((delivered, closed), ([], []))

    def test_unknown_state_stays_visible(self):
        """statefiles.py に状態が増えても、黙って折り畳みへ消えない。"""
        live, _, closed = build.split_projects([project("P-0300", "brand_new")])
        self.assertEqual(ids(live), ["P-0300"])
        self.assertEqual(closed, [])

    def test_delivered_sorted_by_merging_since(self):
        """「直近」は id 順ではなく merging_since 降順。欠けたものは末尾。"""
        ps = [
            project("P-0401", "delivered", merging_since="2026-08-08T00:00:00Z"),
            project("P-0402", "delivered", merging_since="2026-08-10T00:00:00Z"),
            project("P-0403", "delivered"),
            project("P-0404", "delivered", merging_since="2026-08-09T00:00:00Z"),
        ]
        _, delivered, _ = build.split_projects(ps)
        self.assertEqual(ids(delivered), ["P-0402", "P-0404", "P-0401", "P-0403"])


class TestRender(unittest.TestCase):
    def test_none_doc_renders_nothing(self):
        """ops-state を持たない環境 (CI の ops job) では節ごと出さない。"""
        self.assertEqual(build.render_projects(None, {}), "")

    def test_question_stall_outside_the_fold(self):
        h = render([
            project("P-0500", "stalled", stalled_reason="budget_exhausted"),
            project("P-0501", "stalled", stalled_reason="spec_error"),
        ])
        closed_at = h.find(CLOSED_ID)
        self.assertGreaterEqual(closed_at, 0)
        self.assertLess(h.find("P-0500"), closed_at)
        self.assertGreater(h.find("P-0501"), closed_at)
        # 何を待っているかを語で出す (chip は「停止」としか言わない)
        self.assertIn("budget_exhausted", h[:closed_at])

    def test_only_five_delivered_outside_the_fold(self):
        ps = [project(f"P-06{i:02d}", "delivered",
                      merging_since=f"2026-08-{10 - i:02d}T00:00:00Z")
              for i in range(7)]
        h = render(ps)
        fold_at = h.find(DELIVERED_FOLD)
        self.assertGreaterEqual(fold_at, 0)
        for p in ps[:5]:
            self.assertLess(h.find(p["id"]), fold_at, p["id"])
        for p in ps[5:]:
            self.assertGreater(h.find(p["id"]), fold_at, p["id"])
        self.assertIn("それ以前の 2 件", h)

    def test_no_delivered_fold_when_five_or_fewer(self):
        h = render([project(f"P-07{i:02d}", "delivered",
                            merging_since=f"2026-08-0{i + 1}T00:00:00Z")
                    for i in range(5)])
        self.assertNotIn(DELIVERED_FOLD, h)
        self.assertIn("納品済み 5 件", h)

    def test_closed_fold_omitted_when_empty(self):
        h = render([project("P-0800", "active")])
        self.assertNotIn(CLOSED_ID, h)

    def test_counts_come_from_the_layers(self):
        h = render([
            project("P-0900", "active"),
            project("P-0901", "stalled", stalled_reason="merge_timeout"),
            project("P-0902", "stalled", stalled_reason="error"),
            project("P-0903", "delivered", merging_since="2026-08-09T00:00:00Z"),
        ])
        self.assertIn("4 件・進行中 2", h)
        self.assertIn("進行中 2 件", h)
        self.assertIn("納品済み 1 件", h)
        self.assertIn("終わった案 1 件", h)

    def test_empty_ledger(self):
        h = render([])
        self.assertIn("動いているプロジェクトはありません", h)
        self.assertNotIn(CLOSED_ID, h)

    def test_live_layer_can_be_empty(self):
        """終端だけが残っても、節が「進行中 0 件」を語で言う。"""
        h = render([project("P-1000", "vetoed")])
        self.assertIn("いま進行中のプロジェクトはありません", h)
        self.assertIn(CLOSED_ID, h)


if __name__ == "__main__":
    unittest.main()
