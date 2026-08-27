"""heart ビート全体での心拍観測警報の結合テスト。

test_dashboard_smoke_alert_beat.py と同型。単体分岐だけでは
「cursors への書き込みが save_cursors より前にある」順序契約は守れないため、
実物の Heart.beat() を同日内に連続して走らせ、実ファイルで次を固定する:

- heartbeat.error のビートで cursors.json に前回記録が永続化されること
- 同日の続ビートでは積み直されないこと (計器の故障を毎ビート鳴らすと
  通知が壊れた側になる)
- 観測できているビートは cursor も queue も触らないこと
- 他の警報 (dashboard_smoke / download_budget) の cursor を巻き込まないこと
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
from ops.heart import heart as heart_module
from ops.heart.heart import Heart

REPO = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-08-27"


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def health_doc(heartbeat):
    return {
        "generated_at": "2026-08-27T14:00:00Z",
        "autopilot": {
            "deployment": {"replicas": 1, "readyReplicas": 1},
            "pods": [{"name": "autopilot-heart-x", "phase": "Running"}],
            "heartbeat": heartbeat,
        },
    }


class HeartbeatObservationAlertBeatTest(unittest.TestCase):
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
        self.h.docs.save_projects({"version": 1, "projects": [], "chores": []})

    def _patch_externals(self, health_returns):
        return [
            mock.patch.object(heart_module, "datetime", _FixedDatetime),
            mock.patch.object(gitutil, "sync_main", lambda *a, **k: None),
            mock.patch.object(Heart, "k8s_client", lambda self: None),
            mock.patch.object(facts, "load_health", lambda *a, **k: health_returns),
            mock.patch.object(facts, "load_adopted_specs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_jobs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_prs", lambda *a, **k: ({}, {})),
            mock.patch.object(facts, "collect_curriculum", lambda *a, **k: None),
            mock.patch.object(
                facts,
                "collect_feedback",
                lambda gh, cursors, *a, **k: (
                    [], [], False, [], False, [], [], dict(cursors)
                ),
            ),
        ]

    def _beat(self, health_returns):
        with contextlib.ExitStack() as stack:
            for p in self._patch_externals(health_returns):
                stack.enter_context(p)
            self.h.beat(1)

    def _cursors(self):
        return json.loads((self.h.work_dir / "cursors.json").read_text())

    def _entries(self):
        return [
            r
            for r in self.h.work.read_jsonl("briefing-queue.jsonl")
            if str(r.get("source", "")).startswith("heartbeat-observation")
        ]

    def _latest_metric(self):
        return self.h.metrics_store.read_jsonl("metrics.jsonl")[-1]

    def test_error_fires_once_per_day_and_persists_cursor(self):
        doc = health_doc({"error": "HTTPError: HTTP Error 400: Bad Request"})
        self._beat(([], True, doc))

        self.assertEqual(
            self._cursors().get("heartbeat_observation_alert"),
            {"status": "error", "date": TODAY},
        )
        entries = self._entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "heartbeat-observation (error)")
        self.assertIn("400", entries[0]["body"])
        self.assertEqual(self._latest_metric()["heartbeat_observation_status"], "error")
        # 他の警報の流路を巻き込んでいないこと
        self.assertNotIn("dashboard_smoke_alert", self._cursors())
        self.assertNotIn("download_budget_alert", self._cursors())

        # 同日の続ビートは抑制される
        self._beat(([], True, doc))
        self.assertEqual(len(self._entries()), 1)

    def test_observed_heartbeat_writes_nothing(self):
        for _ in range(2):
            self._beat(([], True, health_doc({
                "last_start": {"timestamp": "2026-08-27T13:59:58Z", "iteration": 3483},
                "last_end": {
                    "timestamp": "2026-08-27T13:59:59Z",
                    "iteration": 3482,
                    "exit_code": 0,
                    "elapsed_seconds": 1,
                },
            })))

        self.assertNotIn("heartbeat_observation_alert", self._cursors())
        self.assertEqual(self._entries(), [])
        self.assertIsNone(self._latest_metric()["heartbeat_observation_status"])

    def test_missing_health_observation_is_quiet(self):
        # latest.json ごと読めないビート。それはレポート側の fresh=False の担当で、
        # ここで二重に鳴らさない
        self._beat((None, False, None))

        self.assertNotIn("heartbeat_observation_alert", self._cursors())
        self.assertEqual(self._entries(), [])
        self.assertIsNone(self._latest_metric()["heartbeat_observation_status"])


if __name__ == "__main__":
    unittest.main()
