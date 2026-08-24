"""reporter の書き込みと heart の読み取りが噛み合うことを、両側のコードで確かめる。

report.py は import できない (モジュール top で SA トークンを開く) ので、
test_report_dashboard_smoke.py と同じく AST で純関数だけ取り出し、k8s API 呼び出しを
偽物に差し替える。書けた ConfigMap をそのまま facts.load_health() に渡して、
「reporter が書いたものを heart が読める」を 1 本のテストで固定する。
"""

import ast
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ops.heart import facts

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "apps" / "ops-health-reporter" / "report.py"

FUNCTIONS = ("put_configmap",)
CONSTANTS = ("HEALTH_NAMESPACE", "HEALTH_CONFIGMAP", "HEALTH_KEY")


def load_report_namespace():
    tree = ast.parse(REPORT.read_text())
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            body.append(node)
        elif isinstance(node, ast.Assign):
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in CONSTANTS:
                body.append(node)
    found = {n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in body}
    assert not set(FUNCTIONS + CONSTANTS) - found, f"抽出に失敗: {found}"
    module = ast.Module(body=body, type_ignores=[])
    ns = {"json": json}
    exec(compile(ast.fix_missing_locations(module), "<report_configmap>", "exec"), ns)
    return ns


rep = load_report_namespace()


class FakeAPI:
    """ConfigMap 1 個だけを持つ k8s API。POST/PUT の作法も見る。"""

    def __init__(self, existing=None):
        self.stored = existing
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            if self.stored is None:
                return 404, {"kind": "Status"}
            return 200, self.stored
        if method == "POST":
            self.stored = body
            self.stored["metadata"]["resourceVersion"] = "1"
            return 201, self.stored
        self.stored = body
        return 200, self.stored


def sample_report(generated_at):
    return json.dumps(
        {
            "generated_at": generated_at,
            "applications": [{"name": "immich", "sync": "Synced", "health": "Degraded"}],
        },
        ensure_ascii=False,
        indent=2,
    )


class FakeK8s:
    def __init__(self, cm):
        self.cm = cm

    def get_configmap(self, namespace, name):
        return self.cm


class TestReporterWritesWhatHeartReads(unittest.TestCase):
    def now(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_round_trip(self):
        api = FakeAPI()
        rep["put_configmap"](
            rep["HEALTH_NAMESPACE"],
            rep["HEALTH_CONFIGMAP"],
            {rep["HEALTH_KEY"]: sample_report(self.now())},
            request=api,
        )
        # 無ければ作る
        self.assertEqual([c[0] for c in api.calls], ["GET", "POST"])

        unhealthy, fresh, doc = facts.load_health(
            FakeK8s(api.stored), rep["HEALTH_NAMESPACE"], rep["HEALTH_CONFIGMAP"]
        )
        self.assertEqual(unhealthy, ["immich"])
        self.assertTrue(fresh)
        self.assertEqual(doc["applications"][0]["health"], "Degraded")

    def test_update_carries_resource_version(self):
        # 既存があれば resourceVersion 付き PUT で上書きする (取り違え・競合の検出)
        api = FakeAPI(
            {
                "metadata": {"name": rep["HEALTH_CONFIGMAP"], "resourceVersion": "42"},
                "data": {rep["HEALTH_KEY"]: sample_report(self.now())},
            }
        )
        rep["put_configmap"](
            rep["HEALTH_NAMESPACE"],
            rep["HEALTH_CONFIGMAP"],
            {rep["HEALTH_KEY"]: sample_report(self.now())},
            request=api,
        )
        methods = [c[0] for c in api.calls]
        self.assertEqual(methods, ["GET", "PUT"])
        self.assertEqual(api.calls[1][2]["metadata"]["resourceVersion"], "42")

    def test_write_failure_raises(self):
        # 書けなかったことを握り潰すと、heart は古いレポートを新しいと信じ続ける
        def refuse(method, path, body=None):
            return (404, None) if method == "GET" else (403, {"message": "forbidden"})

        with self.assertRaises(RuntimeError):
            rep["put_configmap"]("autopilot", "x", {"a": "b"}, request=refuse)

    def test_stale_report_is_not_fresh(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        api = FakeAPI()
        rep["put_configmap"](
            rep["HEALTH_NAMESPACE"],
            rep["HEALTH_CONFIGMAP"],
            {rep["HEALTH_KEY"]: sample_report(old)},
            request=api,
        )
        _, fresh, _ = facts.load_health(
            FakeK8s(api.stored), rep["HEALTH_NAMESPACE"], rep["HEALTH_CONFIGMAP"]
        )
        self.assertFalse(fresh, "reporter が止まれば古い = 信じない (fail-closed)")


if __name__ == "__main__":
    unittest.main()
