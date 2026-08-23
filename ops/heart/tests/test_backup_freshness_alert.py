"""backup_freshness_alert / backup_freshness_alert_due の分岐テスト (P-0157)。

latest.json (raw doc) の backup_freshness リストから warn だけを拾うこと、そして
同じ stale_repos 集合の同一日内の再通知を落とすことを固定する。heart は 120s
ビートで回るため抑制が壊れると briefing-queue.jsonl と Discord incident を
1 日で使い潰す (P-0128 の budget 警報と同じ構造・同じ倒し方)。
"""

import unittest

from ops.heart import facts


def report_doc(*rows):
    return {"backup_freshness": list(rows)}


def row(repo="coder-postgres", status="warn", hours=80.5,
        cronjob="coder-restic-backup"):
    """reporter の build_entry() が作る形。"""
    return {
        "repo": repo,
        "namespace": "coder",
        "cronjob": cronjob,
        "last_success_at": "2026-08-19T03:10:00Z",
        "hours_since_success": hours,
        "status": status,
    }


class BackupFreshnessAlertTest(unittest.TestCase):
    def test_warn_rows_are_extracted_with_reason(self):
        a = facts.backup_freshness_alert(
            report_doc(
                row(repo="coder-postgres", hours=80.5),
                row(
                    repo="immich",
                    cronjob="immich-restic-backup",
                    hours=75.25,
                ),
                row(repo="vaultwarden", status="ok", hours=3.4),
            )
        )
        self.assertEqual(
            a,
            {
                "status": "warn",
                # stale_repos は集合として比較されるのでソートして固定する
                "stale_repos": ["coder-postgres", "immich"],
                "reason": "coder-restic-backup (80.5h)、immich-restic-backup (75.2h)",
            },
        )

    def test_single_warn_row_reason_uses_repo_when_cronjob_missing(self):
        a = facts.backup_freshness_alert(report_doc(row(cronjob=None)))
        self.assertEqual(a["reason"], "coder-postgres (80.5h)")
        self.assertEqual(a["stale_repos"], ["coder-postgres"])

    def test_non_numeric_hours_are_carried_as_unknown(self):
        for bad in ("80.5h", True, None):
            a = facts.backup_freshness_alert(
                report_doc(row(hours=bad))
            )
            self.assertEqual(a["reason"], "coder-restic-backup (経過時間不明)")

    def test_quiet_statuses_return_none(self):
        """超過 (warn) 以外は鳴らさない。no_data/error は「測定できていない」であって
        「閾値の超過」ではない (DoD(2)。CronJob 再作成直後の no_data 誤報も避ける)。"""
        for status in ("ok", "no_data", "unconfigured", "error"):
            self.assertIsNone(facts.backup_freshness_alert(report_doc(row(status=status))))

    def test_broken_or_missing_shapes_return_none(self):
        for doc in (
            None,
            {},
            {"backup_freshness": None},
            {"backup_freshness": {}},
            {"backup_freshness": []},
            {"backup_freshness": [None, "x", 1]},
            ["not", "a", "dict"],
        ):
            self.assertIsNone(facts.backup_freshness_alert(doc))


class BackupFreshnessAlertDueTest(unittest.TestCase):
    ALERT = {"status": "warn", "stale_repos": ["immich"], "reason": "r"}

    def test_first_alert_fires(self):
        self.assertTrue(
            facts.backup_freshness_alert_due(self.ALERT, None, "2026-08-23")
        )

    def test_same_set_same_day_is_suppressed(self):
        prev = {"stale_repos": ["immich"], "date": "2026-08-23"}
        self.assertFalse(
            facts.backup_freshness_alert_due(self.ALERT, prev, "2026-08-23")
        )

    def test_next_day_fires_again(self):
        prev = {"stale_repos": ["immich"], "date": "2026-08-22"}
        self.assertTrue(
            facts.backup_freshness_alert_due(self.ALERT, prev, "2026-08-23")
        )

    def test_set_growth_fires_even_same_day(self):
        """新たな経路の超過は新しい情報なので同日でも再通知する。"""
        prev = {"stale_repos": ["immich"], "date": "2026-08-23"}
        grown = {
            "status": "warn",
            "stale_repos": ["coder-postgres", "immich"],
            "reason": "r",
        }
        self.assertTrue(facts.backup_freshness_alert_due(grown, prev, "2026-08-23"))

    def test_set_shrink_refires_same_day(self):
        """回復での集合縮小も再通知する (集合が変わったら鳴る側に倒す)。"""
        prev = {"stale_repos": ["coder-postgres", "immich"], "date": "2026-08-23"}
        shrunk = {"status": "warn", "stale_repos": ["immich"], "reason": "r"}
        self.assertTrue(facts.backup_freshness_alert_due(shrunk, prev, "2026-08-23"))

    def test_none_alert_never_fires(self):
        for prev in (
            None,
            {"stale_repos": ["immich"], "date": "2026-08-23"},
            "garbage",
        ):
            self.assertIs(
                facts.backup_freshness_alert_due(None, prev, "2026-08-23"), False
            )

    def test_garbage_prev_fires(self):
        """前回記録が壊れている場合は鳴る側に倒す (沈黙より過剰通知)。"""
        self.assertTrue(
            facts.backup_freshness_alert_due(self.ALERT, "garbage", "2026-08-23")
        )


if __name__ == "__main__":
    unittest.main()
