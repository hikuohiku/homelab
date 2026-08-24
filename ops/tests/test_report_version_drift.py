"""report.py の version_drift 収集 (P-0126) の純関数部分を固定する。

report.py は import 時に ServiceAccount token を読むためクラスタ外からロードできない。
test_report_dashboard_smoke.py と同じく、副作用を持たない関数と定数だけを AST で
取り出して試す。

固定する契約:
- 産出側 (version-watcher ns の CronJob) が専用 ConfigMap version-drift の report.json
  キーへ書いた observe() 戻り値を読む。**GitHub のブランチは経由しない**
- 鮮度を最優先で判定する。境界は > STALE_AFTER_S でのみ stale
- 形が壊れた記録は no_data。他の収集を止めない
"""

import ast
import datetime
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "report.py"
WATCH_PATH = REPO / "apps" / "version-watcher" / "watch.py"
WATCH_CRONJOB = REPO / "apps" / "version-watcher" / "cronjob.yaml"

FUNCTIONS = ("_version_drift_summary",)
CONSTANTS = ("VERSION_DRIFT_NAMESPACE", "VERSION_DRIFT_STALE_AFTER_S")


def load_functions():
    tree = ast.parse(MODULE_PATH.read_text())
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            body.append(node)
        elif isinstance(node, ast.Assign):
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in CONSTANTS:
                body.append(node)
    missing = set(FUNCTIONS + CONSTANTS) - {
        n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in body
    }
    assert not missing, f"抽出に失敗: {sorted(missing)}"
    module = ast.Module(body=body, type_ignores=[])
    ns = {"datetime": datetime, "json": json}
    exec(compile(ast.fix_missing_locations(module), "<report_version_drift>", "exec"), ns)
    return ns


rep = load_functions()

NOW = datetime.datetime(2026, 8, 25, 3, 0, 0, tzinfo=datetime.timezone.utc)


def payload(generated_at, drifted=None):
    return {
        "generated_at": generated_at,
        "summary": {"total": 40, "drifted": 1, "errors": 0, "uncomparable": 3},
        "drifted": drifted
        if drifted is not None
        else [
            {
                "id": "vaultwarden",
                "current": "1.36.0",
                "latest": "1.37.0",
                "upstream": "ghcr",
            }
        ],
        "errors": [],
    }


class VersionDriftSummary(unittest.TestCase):
    def test_fresh_is_ok(self):
        out = rep["_version_drift_summary"](payload("2026-08-25T02:37:00Z"), NOW)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["summary"]["drifted"], 1)
        self.assertEqual(out["drifted"][0]["id"], "vaultwarden")
        self.assertLess(out["age_seconds"], 3600)

    def test_stale_when_watcher_silent(self):
        out = rep["_version_drift_summary"](payload("2026-08-23T02:37:00Z"), NOW)
        self.assertEqual(out["status"], "stale")
        self.assertIn("観測", out["reason"])

    def test_boundary_is_not_stale(self):
        at = NOW - datetime.timedelta(seconds=rep["VERSION_DRIFT_STALE_AFTER_S"])
        out = rep["_version_drift_summary"](
            payload(at.strftime("%Y-%m-%dT%H:%M:%SZ")), NOW
        )
        self.assertEqual(out["status"], "ok")

    def test_broken_record_raises(self):
        broken = payload("2026-08-25T02:37:00Z")
        broken["summary"] = "壊れている"
        with self.assertRaises(ValueError):
            rep["_version_drift_summary"](broken, NOW)


class WatcherWritesToConfigMap(unittest.TestCase):
    """watcher が git のブランチへ書き戻す経路を持たないことを固定する。

    state-out-of-git: 機械が git を定期的に叩く経路を 1 本も残さない (原則 3)。
    ここが緩むと ops-health-report ブランチが削除後に再生成される。
    """

    def test_no_branch_write(self):
        # モジュール docstring は「以前はブランチだった」という記録なので対象外にする
        tree = ast.parse(WATCH_PATH.read_text())
        tree.body = [n for n in tree.body if not (
            isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
        )]
        src = ast.unparse(tree)
        for token in (
            "ops-health-report",
            "REPORT_BRANCH",
            "ensure_branch",
            "api.github.com/repos/{}/git/refs",
        ):
            self.assertNotIn(token, src, f"watch.py に {token} が残っている")

    def test_writes_named_configmap(self):
        src = WATCH_PATH.read_text()
        self.assertIn('REPORT_CONFIGMAP = "version-drift"', src)
        self.assertIn("put_configmap(", src)
        self.assertEqual(
            rep["VERSION_DRIFT_NAMESPACE"],
            "version-watcher",
            "reporter の読み先 namespace が watcher の居場所とずれている",
        )

    def test_cronjob_has_serviceaccount(self):
        src = WATCH_CRONJOB.read_text()
        self.assertIn("serviceAccountName: version-watcher", src)
        self.assertIn("automountServiceAccountToken: true", src)
        self.assertNotIn("REPORT_BRANCH", src)


if __name__ == "__main__":
    unittest.main()
