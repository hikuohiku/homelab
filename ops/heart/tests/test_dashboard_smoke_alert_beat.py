"""heart ビート全体での dashboard_smoke 警報抑制の結合テスト (P-0193)。

facts.dashboard_smoke_alert() の単体分岐 (test_dashboard_smoke_alert.py) だけでは
「cursors への書き込みが save_cursors より前にある」という順序契約は守れない —
StateFiles._save_json は即時 json.dump なので、save の後から dict に入れた記録は
cursors.json に反映されず、次ビートの load_cursors() は前回記録を持たずに警報を
積み直す (test_budget_alert_beat.py 冒頭の説明と同じ。budget で実際に起きた不具合)。

このテストは外部依存 (git / GitHub API / k8s / Discord) をすべてパッチした上で
実物の Heart.beat() を同日内に連続して走らせ、実ファイル (cursors.json /
briefing-queue.jsonl / metrics.jsonl) で次を固定する:

- fail が鳴ったビートで cursors.json に前回記録が **永続化されている** こと
- 同じ status・同じ日の続ビートでは積み直されないこと
- status 変化 (fail→stale) は同日でも再度積まれること
- quiet な状態 (ok/no_data/観測失敗) は cursor も queue も触らないこと。
  成功日は通知予算を消費しない (DoD 3 の本体)
- budget 側の cursor キーとは独立していること

shadow モードで実行する (spawn・送信の副作用が出ない)。incident の実送信は
smoke_queued と同じ 1 本のフラグから出るため、queue 側の固定で抑制は担保される。
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
from ops.heart.statefiles import StateFiles

REPO = Path(__file__).resolve().parents[3]
# 同日内の連続ビートを見るため時刻は固定 (日付が変わると抑制は解けるのが正)
NOW = datetime(2026, 8, 23, 9, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-08-23"


class _FixedDatetime(datetime):
    """beat() 冒頭の datetime.now(timezone.utc) を NOW に固定する。

    heart.py は `from datetime import datetime` なので、モジュール属性
    ops.heart.heart.datetime をこのサブクラスに差し替えれば効く。"""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def health_doc(status, reason="r", failed_checks=None):
    """load_health が返す latest.json の生 doc。dashboard_smoke キーのみ関心あり。

    reporter (_dashboard_smoke_summary) の summary 形を模す。"""
    doc = {
        "generated_at": "2026-08-23T09:00:00Z",
        "dashboard_smoke": {
            "status": status,
            "reason": reason,
            "ok": status == "ok",
            "generated_at": "2026-08-23T08:50:00Z",
            "age_seconds": 600,
            "checks_total": 12,
        },
    }
    if failed_checks is not None:
        doc["dashboard_smoke"]["failed_checks"] = failed_checks
    return doc


class DashboardSmokeAlertBeatTest(unittest.TestCase):
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
        # doc の置き場は PVC (設計 state-out-of-git 4b-2b)。空の doc を先に
        # 置く — 無いと load_doc が Project CR からの復元に落ちる
        self.h.docs.save_projects({"version": 1, "projects": [], "chores": []})

    def _patch_externals(self, health_returns):
        """git / GitHub / k8s / フィードバック経路をパッチする。

        load_health だけはテストごとに返す値を変える (latest.json の生 doc、
        または観測失敗の (None, False, None))。load_adopted_specs を空にするのは、
        実 repo の archive.jsonl を読むと実 spec が proposed として登録され、
        shadow でも adopt gate が clone を走らせるため。
        """
        return [
            # beat() 内の datetime.now(timezone.utc) を NOW に固定 (実行日に依存させない)
            mock.patch.object(heart_module, "datetime", _FixedDatetime),
            mock.patch.object(gitutil, "sync_main", lambda *a, **k: None),
            # 引数評価で K8s クライアント (SA token 読み) が走るのでクライアントごと止める
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

    def _smoke_queue_entries(self):
        sf = self.h.work
        return [
            r
            for r in sf.read_jsonl("briefing-queue.jsonl")
            if str(r.get("source", "")).startswith("dashboard-smoke")
        ]

    def _latest_metric(self):
        return self.h.metrics_store.read_jsonl("metrics.jsonl")[-1]

    def test_fail_persists_cursor_and_fires_once_per_day_then_stale_refires(self):
        self._beat(([], True, health_doc(
            "fail",
            reason="描画断言が不合格: no-lie-coexistence",
            failed_checks=[{"name": "no-lie-coexistence", "detail": "共存"}],
        )))

        # カーソルが永続化されていること。save_cursors 後の dict 書き込みに
        # 戻すとここで即落ちする (ファイルに反映されないため)
        self.assertEqual(
            self._cursors().get("dashboard_smoke_alert"),
            {"status": "fail", "date": TODAY},
        )
        entries = self._smoke_queue_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "dashboard-smoke (fail)")
        self.assertEqual(entries[0]["body"], "描画断言が不合格: no-lie-coexistence")
        self.assertEqual(self._latest_metric()["dashboard_smoke_status"], "fail")
        # budget 側のキーを巻き込んでいないこと (独立した流路)
        self.assertNotIn("download_budget_alert", self._cursors())

        # 同じ status・同じ日の続ビートは抑制される
        self._beat(([], True, health_doc("fail", reason="r")))
        self.assertEqual(len(self._smoke_queue_entries()), 1)
        self.assertEqual(
            self._cursors()["dashboard_smoke_alert"],
            {"status": "fail", "date": TODAY},
        )

        # fail→stale への変化は同日でも再度積む (カーソルも差し替わる)
        self._beat(([], True, health_doc("stale", reason="記録が古い")))
        entries = self._smoke_queue_entries()
        self.assertEqual(
            [e["source"] for e in entries],
            ["dashboard-smoke (fail)", "dashboard-smoke (stale)"],
        )
        self.assertEqual(
            self._cursors()["dashboard_smoke_alert"],
            {"status": "stale", "date": TODAY},
        )
        self.assertEqual(self._latest_metric()["dashboard_smoke_status"], "stale")

    def test_ok_writes_no_cursor_nor_queue_entry(self):
        # 合格日は記録のみ。続けても cursor を置かないので、後日 fail に
        # 変わっても「前回記録がある」と誤抑制しない
        for _ in range(2):
            self._beat(([], True, health_doc("ok")))

        self.assertNotIn("dashboard_smoke_alert", self._cursors())
        self.assertEqual(self._smoke_queue_entries(), [])
        self.assertIsNone(self._latest_metric()["dashboard_smoke_status"])

    def test_no_data_is_quiet_too(self):
        # 産出側未稼働・記録破損 (no_data) は鳴らさない。budget の
        # unconfigured/no_data と同じ判断 — 鳴らせる状態になってから乗る
        for _ in range(2):
            self._beat(([], True, health_doc("no_data")))

        self.assertNotIn("dashboard_smoke_alert", self._cursors())
        self.assertEqual(self._smoke_queue_entries(), [])
        self.assertIsNone(self._latest_metric()["dashboard_smoke_status"])

    def test_missing_health_observation_is_quiet_too(self):
        # latest.json 無し等で load_health が (None, False, None) を返すビート。
        # 「観測できない」は「警報」ではない
        self._beat((None, False, None))

        self.assertNotIn("dashboard_smoke_alert", self._cursors())
        self.assertEqual(self._smoke_queue_entries(), [])
        self.assertIsNone(self._latest_metric()["dashboard_smoke_status"])


if __name__ == "__main__":
    unittest.main()
