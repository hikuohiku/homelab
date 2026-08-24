"""コア発 command のビート結合テスト (設計 D3/D21)。

reconcile の遷移表 (test_reconcile.TestCoreCommands) は「どう判断するか」しか
固定しない。ここで固定するのは実行側の契約:

- サイドカーが落としたファイルが実際に task-requests.jsonl に載ること
  (キューの id は command_id から決定論的に導かれる)
- 同じ command を持ったまま次のビートを回しても **2 件にならない** こと
  (台帳 commands.jsonl が永続化され、次ビートの facts に載る)
- 停止中は載らず、台帳にも刻まれないこと。再開後のビートで載ること

外部依存 (git / GitHub API / k8s / 通知) はすべてパッチする。
"""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.heart import facts, gitutil, spawn, tasks
from ops.heart.heart import Heart
from ops.heart.notify import Notifier
from ops.heart.statefiles import StateFiles

REPO = Path(__file__).resolve().parents[3]


class CommandBeatTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.command_dir = self.data_dir / "command-bus" / "inbox"
        self.command_dir.mkdir(parents=True)
        env = mock.patch.dict(
            os.environ,
            {"HEART_DATA_DIR": str(self.data_dir), "HEART_MODE": "active"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.h = Heart(REPO)

    def write_command(self, command_id, body="ストリームが太っている", **kw):
        doc = {
            "command_id": command_id,
            "type": "task-request",
            "source": "core",
            "issued_at": "2026-08-23T08:00:00Z",
            "title": "nats の掃除",
            "body": body,
        }
        doc.update(kw)
        (self.command_dir / f"{command_id}.json").write_text(
            json.dumps(doc, ensure_ascii=False)
        )

    def _beat(self, n=1):
        patches = [
            mock.patch.object(gitutil, "sync_main", lambda *a, **k: None),
            mock.patch.object(gitutil, "sync_state_branch", lambda *a, **k: None),
            mock.patch.object(gitutil, "commit_and_push_state", lambda *a, **k: None),
            mock.patch.object(type(self.h.gh), "ensure_branch", lambda *a, **k: None),
            mock.patch.object(Heart, "k8s_client", lambda self: None),
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
            # 依頼の取り込みだけを見る。立案 Job と通知は本題ではない
            mock.patch.object(spawn, "create", lambda *a, **k: "job-dummy"),
            mock.patch.object(Notifier, "send", lambda *a, **k: None),
            mock.patch.object(Notifier, "flush_outbox", lambda *a, **k: None),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.h.beat(n)

    def queue(self):
        return self.h.work.read_jsonl(tasks.QUEUE_FILE)

    def ledger(self):
        return self.h.work.read_jsonl(tasks.COMMAND_LEDGER_FILE)

    def test_command_lands_in_task_queue_once(self):
        self.write_command("core-abc123")
        self._beat(1)

        queue = self.queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["status"], tasks.PENDING)
        self.assertEqual(queue[0]["source"], "core-command/core-abc123")
        self.assertEqual(queue[0]["id"], tasks.make_id("core-command/core-abc123"))
        # 題名も原料として残す (立案役が読む)
        self.assertIn("nats の掃除", queue[0]["body"])
        self.assertEqual(
            [(r["command_id"], r["status"]) for r in self.ledger()],
            [("core-abc123", "accepted")],
        )

        # ファイルは残ったまま (サイドカーが 7 日後に掃除する)。
        # それでも二度目は載らない — 台帳が効いている証拠
        self._beat(2)
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(len(self.ledger()), 1)

    def test_stop_engaged_defers_until_resume(self):
        # 人間が止めている間は実行しない。台帳にも刻まないので依頼は消えない
        sf = StateFiles(self.h.state_dir)
        doc = sf.load_projects()
        doc["stop_engaged"] = True
        sf.save_projects(doc)

        self.write_command("core-def456")
        self._beat(1)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.ledger(), [])

        # 再開 (stop_engaged を落とす) すると次のビートで拾い直す
        doc = StateFiles(self.h.state_dir).load_projects()
        doc["stop_engaged"] = False
        StateFiles(self.h.state_dir).save_projects(doc)
        self._beat(2)
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(len(self.ledger()), 1)

    def test_unsupported_type_is_recorded_without_queueing(self):
        self.write_command("core-xyz789", type="spawn-investigation")
        self._beat(1)

        self.assertEqual(self.queue(), [])
        self.assertEqual(
            [(r["command_id"], r["status"]) for r in self.ledger()],
            [("core-xyz789", "unsupported")],
        )
        # 台帳に載ったので次のビートで通知が発振しない
        self._beat(2)
        self.assertEqual(len(self.ledger()), 1)


if __name__ == "__main__":
    unittest.main()
