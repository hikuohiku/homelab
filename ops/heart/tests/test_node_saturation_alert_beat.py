"""heart ビート全体での CPU 飽和前兆警報の抑制結合テスト (P-9037)。

facts.node_saturation_alert() の単体分岐 (test_node_saturation_alert.py) だけでは
「cursors への書き込みが save_cursors より前にある」という順序契約は守れない —
StateFiles._save_json は即時 json.dump なので、save の後から dict に入れた記録は
cursors.json に反映されず、次ビートの load_cursors() は前回記録を持たずに警報を
積み直す (test_budget_alert_beat.py / test_dashboard_smoke_alert_beat.py 冒頭の
説明と同じ。budget で実際に起きた不具合)。

このテストは外部依存 (git / GitHub API / k8s / Discord) をすべてパッチした上で
実物の Heart.beat() を同日内に連続して走らせ、実ファイル (cursors.json /
briefing-queue.jsonl / metrics.jsonl) で次を固定する:

- warn が鳴ったビートで cursors.json に前回記録が **永続化されている** こと
- 同じ status・同じ日の続ビートでは積み直されないこと
- quiet な状態 (ok/観測失敗) は cursor も queue も触らないこと
- budget / dashboard_smoke 側の cursor キーとは独立していること

shadow モードで実行する (spawn・送信の副作用が出ない)。
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
NOW = datetime(2026, 8, 24, 9, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-08-24"


class _FixedDatetime(datetime):
    """beat() 冒頭の datetime.now(timezone.utc) を NOW に固定する。"""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def health_doc(status, reasons=None):
    """load_health が返す latest.json の生 doc。node_saturation キーのみ関心あり。"""
    return {
        "generated_at": "2026-08-24T09:00:00Z",
        "node_saturation": {
            "status": status,
            "reasons": reasons,
            "requests_m": 3761,
            "allocatable_m": 4000,
            "requests_ratio": 0.9403,
            "load_1m": 25.0,
            "vcpus": 4,
            "node": "node01",
            "load_source": "proc_loadavg",
        },
    }


class NodeSaturationAlertBeatTest(unittest.TestCase):
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
                # cursors (第 2 引数) をそのまま返す passthrough
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

    def _saturation_queue_entries(self):
        sf = self.h.work
        return [
            r
            for r in sf.read_jsonl("briefing-queue.jsonl")
            if str(r.get("source", "")).startswith("node-saturation")
        ]

    def _latest_metric(self):
        return self.h.metrics_store.read_jsonl("metrics.jsonl")[-1]

    def test_warn_persists_cursor_and_fires_once_per_day(self):
        self._beat(([], True, health_doc("warn", ["requests_ratio", "load"])))

        # カーソルが永続化されていること。save_cursors 後の dict 書き込みに
        # 戻すとここで即落ちする
        self.assertEqual(
            self._cursors().get("node_saturation_alert"),
            {"status": "warn", "date": TODAY},
        )
        entries = self._saturation_queue_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "node-saturation (warn)")
        self.assertIn("90% 超 (3761m/4000m)", entries[0]["body"])
        self.assertEqual(self._latest_metric()["node_saturation_status"], "warn")
        # 他方の警報キーを巻き込んでいないこと (独立した流路)
        self.assertNotIn("dashboard_smoke_alert", self._cursors())
        self.assertNotIn("download_budget_alert", self._cursors())

        # 同じ status・同じ日の続ビートは抑制される
        self._beat(([], True, health_doc("warn", ["requests_ratio"])))
        self.assertEqual(len(self._saturation_queue_entries()), 1)
        self.assertEqual(
            self._cursors()["node_saturation_alert"],
            {"status": "warn", "date": TODAY},
        )

    def test_ok_writes_no_cursor_nor_queue_entry(self):
        for _ in range(2):
            self._beat(([], True, health_doc("ok", [])))

        self.assertNotIn("node_saturation_alert", self._cursors())
        self.assertEqual(self._saturation_queue_entries(), [])
        self.assertIsNone(self._latest_metric()["node_saturation_status"])

    def test_missing_health_observation_is_quiet_too(self):
        self._beat((None, False, None))

        self.assertNotIn("node_saturation_alert", self._cursors())
        self.assertEqual(self._saturation_queue_entries(), [])
        self.assertIsNone(self._latest_metric()["node_saturation_status"])


if __name__ == "__main__":
    unittest.main()