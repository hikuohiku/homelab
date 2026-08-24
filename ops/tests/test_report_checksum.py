"""report.py の immich アセット整合性検証の集約 (P-0361) を固定する。

report.py 自身は import 時に ServiceAccount token を読むため cluster 外からは
ロードできない。そこで test_report_dashboard_smoke.py と同じく、副作用を持たない
関数と定数だけを AST で取り出して名前空間に入れ、k8s_get は偽物に差し替えて試す。

固定する契約:
- 産出側 (immich ns の週次 CronJob immich-checksum) が専用 ConfigMap
  immich-checksum-report の report.json キーに書いた結果を読み、checksum 節に載せる
- status は産出側が判定済みの ok / fail / unconfigured / error を**そのまま**載せる
  (集約側は再判定しない。判定ロジックの正は ops/tools/immich_checksum_check.py)
- 産出側未稼働 (ConfigMap 未作成・キー無し)・記録破損 (JSON でない・dict でない・
  status が未知値) は例外にせず no_data で正直に出す。「検出ゼロ」と「帳簿の壊れ」を
  区別可能なまま、他の収集を止めない
- API パスは /api/v1/namespaces/immich/configmaps/immich-checksum-report
"""

import ast
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "report.py"

FUNCTIONS = ("collect_checksum",)
CONSTANTS = ("CHECKSUM_NAMESPACE", "CHECKSUM_CONFIGMAP", "CHECKSUM_STATUSES")


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
    exec(compile(ast.fix_missing_locations(module), "<report_checksum>", "exec"), ns)
    return ns


rep = load_functions()


def report_payload(**overrides):
    """産出側 (immich_checksum_check.build_report) が書く report.json の実測形。"""
    payload = {
        "generated_at": "2026-08-24T12:00:00Z",
        "namespace": "immich",
        "status": "ok",
        "reason": "checksum_mismatch が 0 件 (閾値 1 未満)",
        "ok": True,
        "checksum_mismatch": 0,
        "missing_file": 1,
        "untracked_file": 2,
        "job": {
            "name": "integrity-checksum-mismatch",
            "triggered_at": "2026-08-24T11:00:00Z",
            "run_elapsed_s": 123,
        },
    }
    payload.update(overrides)
    return payload


def collect(payload_or_exception):
    calls = []

    def fake_k8s_get(path):
        calls.append(path)
        if isinstance(payload_or_exception, Exception):
            raise payload_or_exception
        return {
            "data": {"report.json": json.dumps(payload_or_exception, ensure_ascii=False)}
        }

    rep["k8s_get"] = fake_k8s_get
    return rep["collect_checksum"](), calls


class ContractTest(unittest.TestCase):
    def test_namespace_configmap_constants(self):
        self.assertEqual(rep["CHECKSUM_NAMESPACE"], "immich")
        self.assertEqual(rep["CHECKSUM_CONFIGMAP"], "immich-checksum-report")
        self.assertEqual(rep["CHECKSUM_STATUSES"], ("ok", "fail", "unconfigured", "error"))

    def test_api_path_is_namespaced_configmap(self):
        out, calls = collect(report_payload())
        self.assertEqual(out["status"], "ok")
        self.assertEqual(
            calls,
            ["/api/v1/namespaces/immich/configmaps/immich-checksum-report"],
        )

    def test_ok_passes_through_untouched(self):
        out, _ = collect(report_payload())
        self.assertEqual(out, report_payload())

    def test_fail_passes_through_untouched(self):
        payload = report_payload(status="fail", reason="checksum_mismatch が 3 件 (閾値 1 以上)", ok=False, checksum_mismatch=3)
        out, _ = collect(payload)
        self.assertEqual(out, payload)

    def test_unconfigured_passes_through_untouched(self):
        # 閾値未設定 (rules.json の checksum.mismatch_threshold 未導入) の現状実装状態
        payload = report_payload(status="unconfigured", ok=False, reason="閾値未設定")
        out, _ = collect(payload)
        self.assertEqual(out, payload)

    def test_error_stub_record_passes_through(self):
        # 産出側自身の失敗 (代役レコード。dashboard-smoke の rc=2 と同じ思想)
        payload = {
            "generated_at": "2026-08-24T12:00:00Z",
            "namespace": "immich",
            "status": "error",
            "reason": "RuntimeError: queue の取得に失敗: 500 {...}",
            "ok": False,
        }
        out, _ = collect(payload)
        self.assertEqual(out, payload)


class NoDataTest(unittest.TestCase):
    def test_missing_configmap_is_no_data(self):
        out, _ = collect(FileNotFoundError(2, "connect refused"))
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["reason"], "configmap immich-checksum-report を読めない")

    def test_missing_key_is_no_data(self):
        def fake_k8s_get(_path):
            return {"data": {}}

        rep["k8s_get"] = fake_k8s_get
        out = rep["collect_checksum"]()
        self.assertEqual(out["status"], "no_data")
        self.assertIn("report.json", out["error"])

    def test_broken_json_is_no_data(self):
        def fake_k8s_get(_path):
            return {"data": {"report.json": "{not-json"}}

        rep["k8s_get"] = fake_k8s_get
        out = rep["collect_checksum"]()
        self.assertEqual(out["status"], "no_data")

    def test_non_dict_payload_is_no_data(self):
        out, _ = collect(["a", "b"])
        self.assertEqual(out["status"], "no_data")

    def test_unknown_status_is_no_data(self):
        # 未知 status は「検出ゼロ」とも「正常」とも区別できない → no_data
        out, _ = collect(report_payload(status="mystery"))
        self.assertEqual(out["status"], "no_data")
        self.assertIn("mystery", out["error"])


if __name__ == "__main__":
    unittest.main()