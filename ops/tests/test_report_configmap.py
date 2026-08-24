"""reporter がレポートを ConfigMap に書く部分を固定する (設計 state-out-of-git Phase 5)。

report.py は import 時に ServiceAccount token を開くのでクラスタ外からはロードできない
(check_health_reporter_target.py の冒頭参照)。test_report_externalsecrets.py と同じく、
副作用を持たない関数と定数だけを AST で取り出して k8s_request を偽物に差し替える。

固定する契約:
- 無ければ POST で作り、あれば PUT で置き換える (create を resourceNames で縛れないぶん、
  update 側を 1 つの名前に絞ってある — rbac.yaml)
- GET も POST/PUT も 2xx でなければ例外。「書けなかった」を成功に化けさせない
- 1 MiB を超えるレポートは API に弾かれる前に理由の分かる形で落とす
"""

import ast
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "report.py"

FUNCTIONS = ("put_health_configmap",)
CONSTANTS = ("HEALTH_CONFIGMAP", "HEALTH_CONFIGMAP_KEY", "HEALTH_CONFIGMAP_MAX_BYTES")


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
    ns = {"json": json}
    exec(compile(ast.fix_missing_locations(module), "<report_configmap>", "exec"), ns)
    return ns


rep = load_functions()


class FakeAPI:
    """k8s_request の差し替え。(method, path) 順に応答を返す。"""

    def __init__(self, get_status, write_status=200):
        self.get_status = get_status
        self.write_status = write_status
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self.get_status, {} if self.get_status == 200 else "not found"
        return self.write_status, {}


def put(api, payload="{}"):
    rep["k8s_request"] = api
    return rep["put_health_configmap"](
        "ops-health-reporter", rep["HEALTH_CONFIGMAP"], rep["HEALTH_CONFIGMAP_KEY"], payload
    )


class TestPutHealthConfigMap(unittest.TestCase):
    def test_creates_when_absent(self):
        api = FakeAPI(get_status=404, write_status=201)
        put(api, '{"generated_at":"x"}')
        methods = [c[0] for c in api.calls]
        self.assertEqual(methods, ["GET", "POST"])
        self.assertEqual(
            api.calls[1][2]["data"][rep["HEALTH_CONFIGMAP_KEY"]], '{"generated_at":"x"}'
        )

    def test_updates_when_present(self):
        api = FakeAPI(get_status=200)
        put(api)
        self.assertEqual([c[0] for c in api.calls], ["GET", "PUT"])
        self.assertTrue(api.calls[1][1].endswith("/configmaps/" + rep["HEALTH_CONFIGMAP"]))

    def test_write_failure_raises(self):
        api = FakeAPI(get_status=200, write_status=403)
        with self.assertRaises(RuntimeError):
            put(api)

    def test_unexpected_get_status_raises(self):
        api = FakeAPI(get_status=500)
        with self.assertRaises(RuntimeError):
            put(api)

    def test_oversized_report_raises_before_calling_the_api(self):
        api = FakeAPI(get_status=200)
        with self.assertRaises(RuntimeError):
            put(api, "x" * (rep["HEALTH_CONFIGMAP_MAX_BYTES"] + 1))
        self.assertEqual(api.calls, [], "上限超過は API を叩く前に落とすこと")


class TestReporterStillWritesTheBranch(unittest.TestCase):
    """dual-write。外側の番人 (ops/check_health_freshness.py) がまだブランチを読む。"""

    def test_main_writes_both(self):
        source = MODULE_PATH.read_text()
        self.assertIn("put_health_configmap(", source)
        self.assertIn('"ops/health/latest.json"', source)
        self.assertIn("ensure_branch(", source)


if __name__ == "__main__":
    unittest.main()
