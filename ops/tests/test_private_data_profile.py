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

import importlib.util
import json
import os
import pathlib
import tempfile
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


def load_drill():
    """exfil_drill.py をモジュールとして読む (ディレクトリ名にハイフンが
    あるので通常 import できず、ファイル位置で読む)。"""
    path = REPO / "ops/profiles/private-data/exfil_drill.py"
    spec = importlib.util.spec_from_file_location("exfil_drill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDriftGuard(unittest.TestCase):
    def test_apps_copy_is_identical_to_profile_source(self):
        self.assertEqual(
            PROFILE_NP.read_bytes(), APPS_NP.read_bytes(),
            "正本 (ops/profiles) と本番コピー (apps/autopilot) が乖離している",
        )

    def test_apps_kustomization_carries_the_policy(self):
        text = (REPO / "apps/autopilot/kustomization.yaml").read_text(encoding="utf-8")
        self.assertIn("networkpolicy.yaml", text)


class TestImagePinning(unittest.TestCase):
    """ドリル image の digest pin を固定 (#49 の教訓)。

    drill (exfil_drill.py の IMAGE) と参照テンプレート (job-template.yaml) が
    同じ digest pin を指すこと。浮遊タグへの退行も 2 箇所の乖離もここで落とす。
    上げるときは ops/inventory.json の private-data-drill-image の current も
    同じ値に揃えること (こちらは watcher 側の管轄)。
    """

    def test_drill_image_is_digest_pinned(self):
        drill = load_drill()
        self.assertRegex(
            drill.IMAGE, r"^python@sha256:[0-9a-f]{64}$",
            "image は浮遊タグでなく digest pin であること",
        )

    def test_template_shares_the_same_pin(self):
        drill = load_drill()
        text = (REPO / "ops/profiles/private-data/job-template.yaml").read_text(
            encoding="utf-8")
        self.assertIn(f"image: {drill.IMAGE}", text)


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


class TestReportWriting(unittest.TestCase):
    """--report 書き出しの堅牢さを固定 (P-0243 セッション 3 の実測より)。

    固定パス /tmp 配下で「ディレクトリが root 所有」「残骸が他 uid 所有」という
    環境差に報告だけ沈む事故の再発防止。事前プローブで fail fast、最終書き込みは
    原子的着地、という 2 段構えを機械に覚えさせる。
    """

    @classmethod
    def setUpClass(cls):
        cls.drill = load_drill()

    def test_replaces_stale_readonly_file_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / "report.json"
            target.write_text("stale", encoding="utf-8")
            os.chmod(target, 0o444)  # 前回残骸が読み取り専用でも
            self.drill.write_report(str(target), {"ok": True})
            doc = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(doc, {"ok": True})

    def test_preflight_rejects_unwritable_dir_before_cluster_work(self):
        with tempfile.TemporaryDirectory() as d:
            locked = pathlib.Path(d) / "locked"
            locked.mkdir()
            os.chmod(locked, 0o555)
            try:
                with self.assertRaises(self.drill.ReportDestinationError):
                    self.drill.check_report_destination(str(locked / "r.json"))
            finally:
                os.chmod(locked, 0o755)  # TemporaryDirectory の掃除のため戻す

    def test_preflight_passes_without_leaving_residue(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / "r.json"
            self.drill.check_report_destination(str(target))
            self.assertFalse(target.exists())
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])


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
