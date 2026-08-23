"""heart ビートが暦の断片を ops-state へ運ぶことを固定する結合テスト (P-0231)。

publish_reminders() の中身 (文面生成) は ops/tests/test_reminders.py で
純関数として固定済み。ここは配線を見る:

- ビート 1 回で state_dir/briefing/reminders.txt ができ、中身が
  実台帳 × 固定 now のレンダラ出力と一致すること
- 続ビートで内容が変わらないこと (同一断片の再書き込み・二重 commit をしない)
- 台帳が無い環境では黙ってスキップし、ビートが落ちないこと

外部依存 (git / GitHub / k8s / Discord) のパッチ方法は test_budget_alert_beat.py
と同じ。expected をレンダラで作るのは循環ではない — この層で見るのは「実台帳が
state_dir のファイルに降りてくるか」であって、文面の言葉遣いではない。
"""

import contextlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ops.heart import facts, gitutil
from ops.heart.heart import Heart
from ops.life import reminders

REPO = Path(__file__).resolve().parents[3]
# 日付が変わると窓が動くので時刻は固定 (JST 2026-08-23 18:00 相当)
NOW = datetime(2026, 8, 23, 9, 0, 0, tzinfo=timezone.utc)


class RemindersBeatTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        env = mock.patch.dict(
            os.environ,
            {"HEART_DATA_DIR": str(self.data_dir), "HEART_MODE": "shadow"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.h = Heart(REPO)

    def _patch_externals(self):
        return [
            mock.patch.object(gitutil, "sync_main", lambda *a, **k: None),
            mock.patch.object(gitutil, "sync_state_branch", lambda *a, **k: None),
            mock.patch.object(gitutil, "commit_and_push_state", lambda *a, **k: False),
            mock.patch.object(type(self.h.gh), "ensure_branch", lambda *a, **k: None),
            # 引数評価で K8s クライアント (SA token 読み) が走るのでクライアントごと止める
            mock.patch.object(Heart, "k8s_client", lambda self: None),
            mock.patch.object(facts, "load_health", lambda *a, **k: ([], True, None)),
            mock.patch.object(facts, "load_adopted_specs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_jobs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_prs", lambda *a, **k: ({}, {})),
            mock.patch.object(facts, "collect_curriculum", lambda *a, **k: None),
            mock.patch.object(
                facts,
                "collect_feedback",
                lambda gh, rd, cursors, *a, **k: (
                    [], [], False, [], False, [], [], dict(cursors)
                ),
            ),
        ]

    def _beat(self):
        with contextlib.ExitStack() as stack:
            for p in self._patch_externals():
                stack.enter_context(p)
            self.h.beat(1)

    def _fragment(self):
        path = self.h.state_dir / "briefing" / "reminders.txt"
        return path.read_text() if path.exists() else None

    def _expected(self):
        entries = json.loads((REPO / "ops" / "reminders.json").read_text())
        return reminders.render(entries, now=NOW) + "\n"

    def test_publish_writes_fragment_built_from_real_ledger(self):
        # 文面の中身は純関数側 (ops/tests/test_reminders.py) の領分。
        # ここでは「実台帳 × 固定 now」の断片がそのまま降りてくることを見る
        self.assertTrue(self.h.publish_reminders(NOW))
        self.assertEqual(self._fragment(), self._expected())

    def test_beat_transports_the_fragment(self):
        # ビート内部の now は実時刻なので中身は比較しない。state_dir に
        # 断片が降りてくること (commit_and_push_state の add -A に乗る形)
        # だけを見る
        self._beat()
        self.assertIsNotNone(self._fragment())

    def test_second_beat_keeps_fragment_stable(self):
        self.h.publish_reminders(NOW)
        before = self._fragment()
        self.assertFalse(self.h.publish_reminders(NOW))
        self.assertEqual(self._fragment(), before)

    def test_missing_ledger_is_skipped_silently(self):
        with tempfile.TemporaryDirectory() as d:
            self.h.repo_dir = Path(d)
            self.assertFalse(self.h.publish_reminders(NOW))
        self.assertIsNone(self._fragment())


if __name__ == "__main__":
    unittest.main()
