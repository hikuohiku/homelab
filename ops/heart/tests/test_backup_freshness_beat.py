"""heart ビート全体での backup 鮮度警報抑制の結合テスト (P-0157)。

facts.backup_freshness_alert_due() の単体分岐 (test_backup_freshness_alert.py)
だけでは「cursors への書き込みが save_cursors より前にある」という順序契約は
守れない — StateFiles._save_json は即時 json.dump なので、save の後から dict に
入れた記録は cursors.json に反映されず、次ビートの load_cursors() は前回記録を
持たずに警報を積み直す (P-0128 で実際に起きた不具合と同じ構造)。

このテストは外部依存 (git / GitHub API / k8s / Discord) をすべてパッチした上で
実物の Heart.beat() を同日内に連続して走らせ、実ファイル (cursors.json /
briefing-queue.jsonl / metrics.jsonl) で次を固定する:

- warn が鳴ったビートで cursors.json に前回記録が **永続化されている** こと
  (save 後書きに戻すとここで即落ちする)
- 同じ stale_repos 集合・同じ日の続ビートでは積み直されないこと
- warn 経路が増えた (集合が変わった) ら同日でも再度積まれること
- 全経路 ok・観測失敗は cursor も queue も触らないこと

shadow モードで実行する (spawn・送信の副作用が出ない)。incident の実送信は
bf_queued と同じ 1 本のフラグから出るため、queue 側の固定で抑制は担保される。
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
from ops.heart.statefiles import StateFiles

REPO = Path(__file__).resolve().parents[3]
# 同日内の連続ビートを見るため時刻は固定 (日付が変わると抑制は解けるのが正)
NOW = datetime(2026, 8, 23, 9, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-08-23"


def health_doc(*rows):
    """load_health が返す latest.json の生 doc。warn 以外は
    backup_freshness_alert が None を返す形。"""
    return {
        "generated_at": "2026-08-23T09:00:00Z",
        "backup_freshness": list(rows),
    }


def row(repo="coder-postgres", status="warn", hours=80.5,
        cronjob="coder-restic-backup"):
    return {
        "repo": repo,
        "namespace": "coder",
        "cronjob": cronjob,
        "last_success_at": "2026-08-19T03:10:00Z",
        "hours_since_success": hours,
        "status": status,
    }


class BackupFreshnessBeatTest(unittest.TestCase):
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

    def _patch_externals(self, health_returns):
        """git / GitHub / k8s / フィードバック経路をパッチする。

        load_health だけはテストごとに返す値を変える (latest.json の生 doc、
        または観測失敗の (None, False, None))。load_adopted_specs を空にするのは、
        実 repo の archive.jsonl を読むと実 spec が proposed として登録され、
        shadow でも adopt gate が clone を走らせるため。
        """
        return [
            mock.patch.object(gitutil, "sync_main", lambda *a, **k: None),
            mock.patch.object(gitutil, "sync_state_branch", lambda *a, **k: None),
            mock.patch.object(gitutil, "commit_and_push_state", lambda *a, **k: None),
            mock.patch.object(type(self.h.gh), "ensure_branch", lambda *a, **k: None),
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
                # cursors (第 3 引数) をそのまま返す passthrough
                lambda gh, rd, cursors, *a, **k: (
                    [], [], False, [], False, [], dict(cursors)
                ),
            ),
        ]

    def _beat(self, health_returns):
        with contextlib.ExitStack() as stack:
            for p in self._patch_externals(health_returns):
                stack.enter_context(p)
            self.h.beat(1)

    def _cursors(self):
        return json.loads((self.h.state_dir / "cursors.json").read_text())

    def _freshness_queue_entries(self):
        sf = StateFiles(self.h.state_dir)
        return [
            r
            for r in sf.read_jsonl("briefing-queue.jsonl")
            if str(r.get("source", "")).startswith("backup-freshness")
        ]

    def _latest_metric(self):
        return StateFiles(self.h.state_dir).read_jsonl("metrics.jsonl")[-1]

    def test_warn_persists_cursor_then_growth_refires_same_day(self):
        self._beat(
            (
                [],
                True,
                health_doc(row(repo="coder-postgres", hours=80.5)),
            )
        )

        # カーソルが永続化されていること。save_cursors 後の dict 書き込みに
        # 戻すとここで即落ちする (ファイルに反映されないため)
        self.assertEqual(
            self._cursors().get("backup_freshness_alert"),
            {"stale_repos": ["coder-postgres"], "date": TODAY},
        )
        entries = self._freshness_queue_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "backup-freshness (warn)")
        self.assertEqual(entries[0]["body"], "coder-restic-backup (80.5h)")
        self.assertEqual(self._latest_metric()["backup_fresh_warn_count"], 1)

        # 同じ集合・同じ日の続ビートは抑制される
        self._beat(
            (
                [],
                True,
                health_doc(row(repo="coder-postgres", hours=81.0)),
            )
        )
        self.assertEqual(len(self._freshness_queue_entries()), 1)
        self.assertEqual(
            self._cursors()["backup_freshness_alert"],
            {"stale_repos": ["coder-postgres"], "date": TODAY},
        )

        # warn 経路が増えた (集合が変わった) ら同日でも再度積む (カーソルも差し替わる)
        self._beat(
            (
                [],
                True,
                health_doc(
                    row(repo="coder-postgres", hours=82.0),
                    row(
                        repo="syncthing",
                        cronjob="syncthing-restic-backup",
                        hours=73.0,
                    ),
                ),
            )
        )
        entries = self._freshness_queue_entries()
        self.assertEqual(len(entries), 2)
        self.assertIn("syncthing-restic-backup", entries[-1]["body"])
        self.assertEqual(
            self._cursors()["backup_freshness_alert"],
            {"stale_repos": ["coder-postgres", "syncthing"], "date": TODAY},
        )
        self.assertEqual(self._latest_metric()["backup_fresh_warn_count"], 2)

    def test_all_ok_writes_no_cursor_nor_queue_entry(self):
        # 全経路 ok は鳴らない。続けても cursor を置かないので、後日 warn に
        # 変わっても「前回記録がある」と誤抑制しない
        ok_doc = health_doc(
            row(repo="vaultwarden", status="ok", hours=3.4,
                cronjob="vaultwarden-restic-backup"),
            row(status="no_data", hours=None),
        )
        for _ in range(2):
            self._beat(([], True, ok_doc))

        self.assertNotIn("backup_freshness_alert", self._cursors())
        self.assertEqual(self._freshness_queue_entries(), [])
        self.assertIsNone(self._latest_metric()["backup_fresh_warn_count"])

    def test_missing_health_observation_is_quiet_too(self):
        # latest.json 無し等で load_health が (None, False, None) を返すビート。
        # 「観測できない」は「警報」ではない
        self._beat((None, False, None))

        self.assertNotIn("backup_freshness_alert", self._cursors())
        self.assertEqual(self._freshness_queue_entries(), [])
        self.assertIsNone(self._latest_metric()["backup_fresh_warn_count"])


if __name__ == "__main__":
    unittest.main()
