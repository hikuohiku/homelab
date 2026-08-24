"""作業ファイルを PVC へ出したこと (設計 state-out-of-git Phase 3) の固定。

ここで守るのは 2 つ:

- **移行でキューを失わないこと。** ops-state に載ったまま止まった heart を
  新しいイメージで起こしたとき、未送信の通知・受理済みの依頼・フィードバックの
  取り込み位置が PVC 側へ移り、ops-state からは消えること。移行が抜けると
  cursors.json を失って過去のフィードバックを全部取り込み直す
- **audit.jsonl が無限に増えないこと。** git に出さなくなった代わりに、
  保持窓は自分で持つ

外部依存 (git / GitHub API / k8s / 通知) はすべてパッチする。
"""

import contextlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ops.heart import facts, gitutil, metrics, spawn
from ops.heart.heart import Heart
from ops.heart.notify import Notifier
from ops.heart.tests.fakek8s import FakeK8s
from ops.heart.statefiles import WORK_FILES, StateFiles, migrate_plan

REPO = Path(__file__).resolve().parents[3]

# ops-state に残ってよいもの。metrics.jsonl は旧ダッシュボード向けの経過措置で、
# 最新 1 行だけが載る (設計 Phase 1)
# 4b-2b で projects.json への書き込みが止まった。git に残るのは心拍と
# 旧ダッシュボード用の 1 行だけ (どちらも Phase 7 で消える)
KEPT_IN_GIT = ("heartbeat.json", "metrics.jsonl")


class MigratePlanTest(unittest.TestCase):
    def test_copies_only_what_pvc_lacks_but_removes_all(self):
        copy, remove = migrate_plan(
            ["outbox.jsonl", "cursors.json", "projects.json"], ["cursors.json"]
        )
        # PVC の cursors.json は移行後に heart が書いた方が正なので上書きしない。
        # それでも ops-state 側は消す (古い写しを正と取り違えさせない)
        self.assertEqual(copy, ["outbox.jsonl"])
        self.assertEqual(sorted(remove), ["cursors.json", "outbox.jsonl"])

    def test_ignores_files_outside_the_work_set(self):
        self.assertEqual(migrate_plan(["projects.json", "heartbeat.json"], []), ([], []))

    def test_done_when_nothing_left_in_git(self):
        self.assertEqual(migrate_plan([], list(WORK_FILES)), ([], []))


class MigrateBeatTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        env = mock.patch.dict(
            os.environ,
            {"HEART_DATA_DIR": str(self.data_dir), "HEART_MODE": "active"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.h = Heart(REPO)
        # プロジェクトの正は Project CR (4b-2b)。記憶だけの k8s を差す
        self.h._fake_k8s = FakeK8s()
        self.h.state_dir.mkdir(parents=True, exist_ok=True)

    def _beat(self, n=1):
        patches = [
            mock.patch.object(gitutil, "sync_main", lambda *a, **k: None),
            mock.patch.object(gitutil, "sync_state_branch", lambda *a, **k: None),
            mock.patch.object(gitutil, "commit_and_push_state", lambda *a, **k: None),
            mock.patch.object(type(self.h.gh), "ensure_branch", lambda *a, **k: None),
            mock.patch.object(Heart, "k8s_client", lambda self: self._fake_k8s),
            mock.patch.object(facts, "load_health", lambda *a, **k: ([], True, None)),
            mock.patch.object(facts, "load_adopted_specs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_jobs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_prs", lambda *a, **k: ({}, {})),
            mock.patch.object(facts, "collect_curriculum", lambda *a, **k: None),
            mock.patch.object(facts, "collect_critic", lambda *a, **k: None),
            mock.patch.object(
                facts, "collect_feedback",
                lambda gh, rd, cursors, *a, **k: (
                    [], [], False, [], False, [], [], dict(cursors)
                ),
            ),
            mock.patch.object(spawn, "create", lambda *a, **k: "job-dummy"),
            mock.patch.object(Notifier, "send", lambda *a, **k: None),
            mock.patch.object(Notifier, "flush_outbox", lambda *a, **k: None),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.h.beat(n)

    def seed_state_branch(self):
        """移行前の ops-state。heart が今まで書いてきたものを一通り置く。"""
        sf = StateFiles(self.h.state_dir)
        sf.save_cursors(
            {
                "issue_comments_since": "2026-08-20T00:00:00Z",
                "seen_feedback_files": ["a.json"],
            }
        )
        sf.append_jsonl(
            "outbox.jsonl",
            {"at": "2026-08-20T00:00:00Z", "type": "deliver", "text": "未送信"},
        )
        sf.append_jsonl(
            "task-requests.jsonl",
            {"id": "abc", "status": "pending", "source": "core-command/x", "body": "b"},
        )
        sf.append_jsonl("audit.jsonl", {"at": "2026-08-20T00:00:00Z", "action": "announce"})
        return sf

    def test_first_beat_moves_work_files_and_keeps_their_content(self):
        self.seed_state_branch()
        self._beat(1)

        self.assertEqual(
            self.h.work.load_cursors()["issue_comments_since"], "2026-08-20T00:00:00Z"
        )
        self.assertEqual(len(self.h.work.read_jsonl("outbox.jsonl")), 1)
        self.assertEqual(
            [r["id"] for r in self.h.work.read_jsonl("task-requests.jsonl")], ["abc"]
        )
        # ops-state に残るのは外から見えるものだけ
        left = sorted(p.name for p in self.h.state_dir.iterdir() if p.is_file())
        self.assertEqual(left, sorted(set(left) & set(KEPT_IN_GIT)))
        self.assertNotIn("projects.json", left, "正は Project CR (4b-2b)")

    def test_migration_is_idempotent_and_does_not_clobber_pvc(self):
        sf = self.seed_state_branch()
        self._beat(1)

        # 移行後は PVC 側が正。ops-state に古い写しが復活しても上書きさせない
        self.h.work.save_cursors(
            {"issue_comments_since": "2026-08-24T00:00:00Z", "seen_feedback_files": []}
        )
        sf.save_cursors(
            {
                "issue_comments_since": "2026-08-20T00:00:00Z",
                "seen_feedback_files": ["a.json"],
            }
        )
        self._beat(2)

        self.assertEqual(
            self.h.work.load_cursors()["issue_comments_since"], "2026-08-24T00:00:00Z"
        )
        self.assertFalse((self.h.state_dir / "cursors.json").exists())

    def test_beat_writes_work_files_to_pvc_not_to_git(self):
        self._beat(1)
        for name in WORK_FILES:
            self.assertFalse(
                (self.h.state_dir / name).exists(), f"{name} が ops-state に出ている"
            )
        # 監査行はビートが必ず書く経路なので、PVC 側に出ていることを見ておく
        self.h.work.append_jsonl("audit.jsonl", {"at": "2026-08-24T00:00:00Z"})
        self.assertTrue((self.h.work_dir / "audit.jsonl").exists())


class PruneAuditTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 25, tzinfo=timezone.utc)

    def line(self, days_ago):
        at = self.now - timedelta(days=days_ago)
        return {"at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "action": "announce"}

    def test_drops_only_lines_older_than_the_window(self):
        records = [self.line(40), self.line(31), self.line(29), self.line(0)]
        self.assertEqual(metrics.prune_audit(records, self.now), records[2:])

    def test_keeps_lines_without_a_readable_timestamp(self):
        # 書式を知らない行を黙って消すと、壊れた書き手に気づく手段が無くなる
        broken = {"action": "announce"}
        self.assertEqual(
            metrics.prune_audit([broken, self.line(40)], self.now), [broken]
        )


class PruneAuditBeatTest(unittest.TestCase):
    def test_prune_rewrites_the_file_on_pvc(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"HEART_DATA_DIR": d}):
                h = Heart(REPO)
            now = datetime(2026, 8, 25, tzinfo=timezone.utc)
            old = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
            h.work.append_jsonl("audit.jsonl", {"at": old, "action": "announce"})
            h.work.append_jsonl(
                "audit.jsonl", {"at": "2026-08-25T00:00:00Z", "action": "deliver"}
            )
            h.prune_audit(now)
            self.assertEqual(
                [r["action"] for r in h.work.read_jsonl("audit.jsonl")], ["deliver"]
            )


if __name__ == "__main__":
    unittest.main()
