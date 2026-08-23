"""private-data 分離プロファイル (P-0243) の意味論と drift を固定する。

- 正本 (ops/profiles/private-data/networkpolicy.yaml) と本番コピー
  (apps/autopilot/networkpolicy.yaml) が一致すること (drift guard)
- ポリシーが「private-data=true のみ選択 / ingress 全拒否 / egress は DNS のみ」
  という fail-closed 形であること。P-0203 census 由来の穴を開けるときは
  このテストの更新もセットで行う (開けた穴を実測と無関係に増やさせない)
- spawn.py が capability "private-data" 宣言時のみラベルを付けること
- 台帳基準 1 の証拠 demo.json の判定キーが全て true であること
  (existence 検査は ops/tests/test_stage3_readiness.py 側)

リポジトリルートから `python3 -m unittest ops.tests.test_private_data_profile`。
"""

import json
import pathlib
import unittest

import yaml

from ops.heart.spawn import build_job

REPO = pathlib.Path(__file__).resolve().parents[2]
PROFILE_NP = REPO / "ops/profiles/private-data/networkpolicy.yaml"
APPS_NP = REPO / "apps/autopilot/networkpolicy.yaml"
DEMO = REPO / "ops/profiles/private-data/demo.json"


class FakeCfg:
    namespace = "autopilot"
    image = "example:1"
    repo = "hikuohiku/homelab"

    def model_for(self, _role):
        return "test-model"


def load_np():
    return yaml.safe_load(PROFILE_NP.read_text(encoding="utf-8"))


class TestDriftGuard(unittest.TestCase):
    def test_apps_copy_is_identical_to_profile_source(self):
        self.assertEqual(
            PROFILE_NP.read_bytes(), APPS_NP.read_bytes(),
            "正本 (ops/profiles) と本番コピー (apps/autopilot) が乖離している",
        )

    def test_apps_kustomization_carries_the_policy(self):
        text = (REPO / "apps/autopilot/kustomization.yaml").read_text(encoding="utf-8")
        self.assertIn("networkpolicy.yaml", text)


class TestPolicySemantics(unittest.TestCase):
    """fail-closed 形を機械固定。census 由来の穴を開ける時はここを conscious に変える。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = load_np()

    def test_selects_only_private_data_pods(self):
        sel = self.doc["spec"]["podSelector"]["matchLabels"]
        self.assertEqual(sel, {"private-data": "true"})
        self.assertEqual(self.doc["metadata"]["namespace"], "autopilot")

    def test_isolates_both_directions(self):
        self.assertEqual(set(self.doc["spec"]["policyTypes"]), {"Ingress", "Egress"})

    def test_ingress_is_fully_denied(self):
        self.assertEqual(self.doc["spec"]["ingress"], [])

    def test_egress_allows_dns_and_nothing_else_yet(self):
        egress = self.doc["spec"]["egress"]
        self.assertEqual(len(egress), 1, "egress 規則の追加は P-0203 census 実測由来で行う")
        rule = egress[0]
        peer = rule["to"][0]
        ns = peer["namespaceSelector"]["matchLabels"][
            "kubernetes.io/metadata.name"
        ]
        pod = peer["podSelector"]["matchLabels"]
        self.assertEqual((ns, pod), ("kube-system", {"k8s-app": "kube-dns"}))
        ports = {(p["protocol"], p["port"]) for p in rule["ports"]}
        self.assertEqual(ports, {("UDP", 53), ("TCP", 53)})


class TestSpawnWiring(unittest.TestCase):
    def _labels(self, capabilities):
        project = {
            "id": "P-TEST",
            "branch": "project/p-test",
            "capabilities": capabilities,
        }
        job = build_job(FakeCfg(), "runner", project=project)
        return job["metadata"]["labels"], job["spec"]["template"]["metadata"]["labels"]

    def test_capability_adds_label_to_both_metadata_levels(self):
        job_labels, tpl_labels = self._labels(["kubectl-write", "private-data"])
        self.assertEqual(job_labels.get("private-data"), "true")
        self.assertEqual(tpl_labels.get("private-data"), "true")
        # kubectl-write の従来挙動を壊していないこと
        sa = build_job(FakeCfg(), "runner", project={
            "id": "P-TEST", "capabilities": ["kubectl-write", "private-data"],
        })["spec"]["template"]["spec"]["serviceAccountName"]
        self.assertEqual(sa, "autopilot-writer")

    def test_without_capability_no_label(self):
        for caps in ([], ["kubectl-write"]):
            with self.subTest(caps=caps):
                job_labels, tpl_labels = self._labels(caps)
                self.assertNotIn("private-data", job_labels)
                self.assertNotIn("private-data", tpl_labels)


class TestLedgerEvidence(unittest.TestCase):
    """台帳基準 1 の証拠ファイルが判定キー全 true であること。

    existence 検査自体は test_stage3_readiness.py 側。ここでは「証拠の中身が
    主張どおり」まで見る — ダミーファイルでの existence 潜りを構造的に落とす。
    """

    def test_demo_json_verdicts_are_true(self):
        doc = json.loads(DEMO.read_text(encoding="utf-8"))
        keys = (
            "labeled_blocked", "unlabeled_allowed",
            "dns_ok_labeled", "dns_ok_control", "cleaned_up", "all_passed",
        )
        for k in keys:
            with self.subTest(key=k):
                self.assertIs(doc[k], True)


if __name__ == "__main__":
    unittest.main()
