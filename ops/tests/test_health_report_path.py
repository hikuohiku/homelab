"""健全性レポートの経路が 3 者で揃っていることを機械で縛る (設計 Phase 5)。

書き手 (apps/ops-health-reporter/report.py)、読み手 2 つ (ops/heart, Go のコア)、
そして RBAC が、同じ namespace / ConfigMap 名 / キーを指していること。ここがずれても
誰も落ちない — heart は「観測できなかった」と静かに言い続け、コアは変化に気づかなく
なるだけなので、**沈黙する形で壊れる**。だから検査で縛る。

あわせて、GitHub を経由する旧経路が戻っていないことも見る (Phase 5 の目的)。
"""

import ast
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "apps" / "ops-health-reporter" / "report.py"
REPORTER_RBAC = ROOT / "apps" / "ops-health-reporter" / "rbac.yaml"
REPORTER_CRONJOB = ROOT / "apps" / "ops-health-reporter" / "cronjob.yaml"
HEART_RBAC = ROOT / "apps" / "autopilot" / "rbac.yaml"
CORE_K8S_GO = ROOT / "apps" / "autopilot-core" / "app" / "k8s.go"


def report_constant(name):
    """report.py の定数を取り出す。モジュール top で SA トークンを開くので import は
    できない (check_health_reporter_target.py と同じ理由)。"""
    for node in ast.parse(REPORT.read_text()).body:
        if isinstance(node, ast.Assign):
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"report.py に定数 {name} が無い")


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]


def rules_of(path, kind, name):
    for doc in docs(path):
        if doc.get("kind") == kind and (doc.get("metadata") or {}).get("name") == name:
            return doc
    raise AssertionError(f"{path.name} に {kind}/{name} が無い")


NAMESPACE = report_constant("HEALTH_NAMESPACE")
CONFIGMAP = report_constant("HEALTH_CONFIGMAP")
KEY = report_constant("HEALTH_KEY")


class TestReadersMatchWriter(unittest.TestCase):
    def test_heart_reads_where_reporter_writes(self):
        from ops.heart import facts
        from ops.heart.config import Config

        cfg = Config(ROOT, {}, {}, {})
        self.assertEqual(cfg.namespace, NAMESPACE)
        self.assertEqual(cfg.health_configmap, CONFIGMAP)
        self.assertEqual(facts.HEALTH_KEY, KEY)

    def test_core_reads_where_reporter_writes(self):
        # Go 側の既定値 (healthReportTarget)。envOr の第 2 引数を素直に読む
        source = CORE_K8S_GO.read_text()
        body = re.search(
            r"func healthReportTarget\(\).*?\n}", source, re.DOTALL
        )
        self.assertIsNotNone(body, "k8s.go に healthReportTarget が無い")
        defaults = re.findall(r'envOr\("CORE_HEALTH_[A-Z]+", "([^"]+)"\)', body.group(0))
        self.assertEqual(defaults, [NAMESPACE, CONFIGMAP, KEY])


class TestRbac(unittest.TestCase):
    def test_reporter_can_write_only_that_configmap(self):
        role = rules_of(REPORTER_RBAC, "Role", "ops-health-reporter-health-writer")
        self.assertEqual(role["metadata"]["namespace"], NAMESPACE)
        named = [r for r in role["rules"] if r.get("resourceNames")]
        self.assertEqual([r["resourceNames"] for r in named], [[CONFIGMAP]])
        self.assertEqual(sorted(named[0]["verbs"]), ["get", "update"])
        # create は RBAC で名前を絞れない。その代わり他の動詞は入れない
        for rule in role["rules"]:
            self.assertEqual(rule["resources"], ["configmaps"])
            self.assertFalse(set(rule["verbs"]) - {"create", "get", "update"})

    def test_readers_get_only(self):
        role = rules_of(HEART_RBAC, "Role", "ops-health-report-reader")
        self.assertEqual(role["metadata"]["namespace"], NAMESPACE)
        self.assertEqual(len(role["rules"]), 1)
        rule = role["rules"][0]
        self.assertEqual(rule["resourceNames"], [CONFIGMAP])
        # コアに書き込み動詞を渡さない (設計 D29)
        self.assertEqual(rule["verbs"], ["get"])

        binding = rules_of(HEART_RBAC, "RoleBinding", "ops-health-report-reader")
        self.assertEqual(
            sorted(s["name"] for s in binding["subjects"]),
            ["autopilot-core", "autopilot-heart"],
        )

    def test_kubelet_summary_proxy_resource_names_match_node(self):
        # P-9062。nodes/proxy の resourceNames は **node 名** と照合される (proxy
        # サブパスではない)。"stats/summary" を入れると「stats/summary という名前の
        # node」を指し、node01 への要求が 403 で拒否される罠 (2026-08-25 実測)。
        # node01 に絞ること、read-only の get のみであることを機械で縛る。
        role = rules_of(REPORTER_RBAC, "ClusterRole", "ops-health-reporter-reader")
        proxy = next(r for r in role["rules"] if r["resources"] == ["nodes/proxy"])
        stats = next(r for r in role["rules"] if r["resources"] == ["nodes/stats"])
        self.assertEqual(proxy["resourceNames"], ["node01"])
        self.assertEqual(proxy["verbs"], ["get"])
        self.assertEqual(stats["resourceNames"], ["node01"])
        self.assertEqual(stats["verbs"], ["get"])


class TestNoGitHubRoundTrip(unittest.TestCase):
    def test_reporter_does_not_touch_github(self):
        source = REPORT.read_text()
        for needle in ("api.github.com", "GITHUB_TOKEN", "REPORT_BRANCH"):
            self.assertNotIn(
                needle,
                source,
                f"report.py に {needle} が戻っている"
                " (クラスタ内 → GitHub → クラスタ内 の往復は Phase 5 で切った)",
            )

    def test_cronjob_has_no_github_credential(self):
        for doc in docs(REPORTER_CRONJOB):
            if doc.get("kind") != "CronJob":
                continue
            spec = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            for container in spec["containers"]:
                for env in container.get("env") or []:
                    self.assertNotIn("GITHUB", env["name"])


if __name__ == "__main__":
    unittest.main()
