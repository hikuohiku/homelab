"""report.py の autopilot (heart) 観測が複数コンテナ Pod で成立することを固定する。

heart Pod は NATS 導入 (2026-08-23) から heart / bus-sidecar の 2 コンテナ。
複数コンテナ Pod への pods/log は container= を省くと k8s API が 400 を返すため、
2026-08-27 まで latest.json の autopilot.heartbeat は毎回
`HTTPError: HTTP Error 400: Bad Request` で埋まっていた。

report.py 自身は import 時に ServiceAccount token を読むため cluster 外からは
ロードできない。test_report_dashboard_smoke.py と同じく、副作用を持たない関数と
定数だけを AST で取り出し、k8s API 呼び出しを偽物に差し替えて試す。

固定する契約:
- ログ取得は container=heart を名指しする (省けば偽 API が実機同様 400 を返す)
- pods[].restartCount は heart のもの (containerStatuses は名前順で返るため
  [0] は bus-sidecar を指しうる)
- 観測に失敗したら heartbeat.error に正直に出す (鳴らすのは読み手の担当)
"""

import ast
import json
import re
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "report.py"

FUNCTIONS = ("collect_autopilot_health", "parse_heartbeat")
CONSTANTS = (
    "AUTOPILOT_NAMESPACE",
    "AUTOPILOT_DEPLOYMENT",
    "AUTOPILOT_APP_LABEL",
    "AUTOPILOT_HEART_CONTAINER",
    "HEARTBEAT_RE",
)


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
    found = {
        n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in body
    }
    assert not set(FUNCTIONS + CONSTANTS) - found, f"抽出に失敗: {found}"
    module = ast.Module(body=body, type_ignores=[])
    ns = {"json": json, "re": re, "urllib": urllib}
    exec(compile(ast.fix_missing_locations(module), "<report_autopilot>", "exec"), ns)
    return ns


HEART_LOG = "\n".join(
    [
        "[autopilot] 2026-08-27T13:58:00Z iteration #3482 start",
        "[autopilot] 2026-08-27T13:58:02Z iteration #3482 end exit=0 elapsed=2s",
        "[autopilot] 2026-08-27T14:00:00Z iteration #3483 start",
    ]
)


def http_400(path):
    return urllib.error.HTTPError(path, 400, "Bad Request", {}, None)


class FakeCluster:
    """heart / bus-sidecar の 2 コンテナ Pod を持つ k8s API。

    pods/log は実機と同じく container= が無ければ 400 を返す。
    """

    def __init__(self):
        self.log_paths = []

    def get(self, path):
        if "/deployments/" in path:
            return {"status": {"replicas": 1, "readyReplicas": 1}}
        if "/pods?" in path:
            return {
                "items": [
                    {
                        "metadata": {"name": "autopilot-heart-79d4ddc795-klsfw"},
                        "status": {
                            "phase": "Running",
                            # k8s は containerStatuses を名前順で返す
                            "containerStatuses": [
                                {"name": "bus-sidecar", "restartCount": 4},
                                {"name": "heart", "restartCount": 1},
                            ],
                        },
                    }
                ]
            }
        raise AssertionError("想定外の GET: " + path)

    def get_text(self, path):
        self.log_paths.append(path)
        query = urllib.parse.parse_qs(path.split("?", 1)[1])
        container = query.get("container", [None])[0]
        if container is None:
            raise http_400(path)
        if container != "heart":
            raise AssertionError("heart 以外のコンテナを見ている: " + path)
        return HEART_LOG


class AutopilotHealthTest(unittest.TestCase):
    def setUp(self):
        self.ns = load_functions()
        self.cluster = FakeCluster()
        self.ns["k8s_get"] = self.cluster.get
        self.ns["k8s_get_text"] = self.cluster.get_text

    def collect(self):
        return self.ns["collect_autopilot_health"]()

    def test_heartbeat_is_observed_on_multi_container_pod(self):
        result = self.collect()
        self.assertNotIn("error", result["heartbeat"])
        self.assertEqual(result["heartbeat"]["last_start"]["iteration"], 3483)
        self.assertEqual(result["heartbeat"]["last_end"]["iteration"], 3482)
        self.assertEqual(result["heartbeat"]["last_end"]["exit_code"], 0)

    def test_log_request_names_the_heart_container(self):
        self.collect()
        self.assertEqual(len(self.cluster.log_paths), 1)
        query = urllib.parse.parse_qs(self.cluster.log_paths[0].split("?", 1)[1])
        self.assertEqual(query["container"], ["heart"])
        # 窓は時間で取る契約 (tailLines では 1 周が 200 行を超えて取りこぼす)
        self.assertEqual(query["sinceSeconds"], ["7200"])

    def test_restart_count_is_the_heart_container_not_the_sidecar(self):
        pod = self.collect()["pods"][0]
        self.assertEqual(pod["container"], "heart")
        self.assertEqual(pod["restartCount"], 1)

    def test_http_error_is_still_reported_as_heartbeat_error(self):
        # 400 を握りつぶさないこと。観測できないことは正直に error で出し、
        # 鳴らすのは読み手 (facts.heartbeat_observation_alert) の担当
        def always_400(path):
            raise http_400(path)

        self.ns["k8s_get_text"] = always_400
        result = self.collect()
        self.assertIn("400", result["heartbeat"]["error"])


if __name__ == "__main__":
    unittest.main()
