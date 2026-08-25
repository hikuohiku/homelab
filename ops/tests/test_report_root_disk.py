"""report.py main() が ConfigMap に書く latest.json が P-9062 の受入検証を満たすことを、
k8s 層を偽物に差し替えてクラスタ無しで固定する (P-9062)。

**2026-08-25 実測で判明した spec レベルの罠**: P-9062 の受入検証 verify[0]
`kubectl get cm -n autopilot ops-health-report -o jsonpath='{.data.latest.json}' ...` は
**実 kubectl では決して green にならない**。実 kubectl の jsonpath は `{.data.latest.json}`
を**入れ子フィールド** (data["latest"]["json"]) と解釈するため、data のキーが
`latest.json` (リテラル) の ConfigMap では空出力になり、後段の json.load が
JSONDecodeError で落ちる (mock apiserver + kubectl v1.35.0 で実測 — wrapper の verify
出力と同一の失敗)。リテラルのドットキーを読むには `{.data.latest\\.json}` と
バックスラッシュでエスケープする (ops/CHARTER.md §5.5 の実測済み読み方)。
**クラスタ到達が解決しても verify[0] は通らない**ため、以前の「reporter が 1 回走れば
green」という見込みは誤りで、spec の verify[0] 自体の修正 (エスケープ) が必要。

report.py の main() を AST 抽出し (report.py は import 時に ServiceAccount token を
読むため cluster 外から import 不可 — test_report_configmap_write.py と同じ流儀)、
k8s_get / k8s_request を偽物に差し替えて 1 周実行させ、書けた ConfigMap の
data[latest.json] に受入検証の python 断片をそのまま流して通ることを CI で固定する。
kubectl 偽物は**実 kubectl の jsonpath 解釈を忠実に模す** (ドット=入れ子・`\\.`=リテラル、
欠落フィールドは空出力) ため、「verify[0] が実 kubectl では空出力になる」事実も CI に
閉じる。main() 本体を実行するので配線 (report の root_disk キー・fill_days・履歴書き戻し)
の変化を検出できる。

root_disk の取得源は kubelet stats/summary が優先 (RBAC 追加済み、nodes/proxy +
nodes/stats) なので、root_disk_usage.k8s_get も summary を返す偽物に差し替えて
source=kubelet_summary の経路をオフラインで通す (内訳 images/PVC まで載る)。

固定する契約:
- main() が書く latest.json に root_disk 節があり、fill_days キーを持つ
  (初回 run は履歴 1 点で fill_days=None、fill_days_note に理由あり — 受入検証は
  キー存在のみ。予報は 1 日分の履歴が溜まってから)
- root_disk.source が kubelet_summary (RBAC で追加した経路が優先される)
- 履歴は同一 ConfigMap の root_disk_history.json キーに latest.json と同じ 1 回の
  PUT で書かれる (別 PUT だと resourceVersion 競合の 409 — report.py コメント参照)
- 他の収集が空応答でも main() は 1 周走りきる (1 収集の失敗が他を止めない collect()
  の思想)
"""

import ast
import datetime
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import textwrap
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ops.tools import node_saturation
from ops.tools import root_disk_usage

REPO = Path(__file__).resolve().parents[2]
REPORT = REPO / "apps" / "ops-health-reporter" / "report.py"
DOWNLOAD_BUDGET = REPO / "apps" / "ops-health-reporter" / "download_budget.py"

# 受入検証の python 断片 (kubectl 部分を除いた残りをそのまま使う)
SPEC_VERIFY_SNIPPET = (
    "import json,sys; d=json.load(sys.stdin); "
    "assert d.get(\"root_disk\") and \"fill_days\" in d[\"root_disk\"]"
)

# kubectl 偽物 (P-9062)。**実 kubectl v1.35.0 の -o jsonpath を忠実に模す**:
# ドットは入れ子フィールド区切り、`\.` はリテラルのドット、欠落フィールドは空出力。
# `{.data.latest.json}` は data["latest"]["json"] を探すため、data のキーが
# `latest.json` (リテラル) の ConfigMap では空出力になる (mock apiserver で実測)。
# 旧偽物は「jsonpath はリテラルキーを返す」と誤って模しており、spec の verify[0] が
# 実 kubectl では通らない事実を隠していた。
KUBECTL_SHIM = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys

    def resolve(obj, expr):
        inner = expr.strip()
        if inner.startswith("{") and inner.endswith("}"):
            inner = inner[1:-1]
        if inner.startswith("$"):
            inner = inner[1:]
        tokens, cur = [], []
        i = 0
        while i < len(inner):
            c = inner[i]
            if c == "\\\\" and i + 1 < len(inner):
                cur.append(inner[i + 1])
                i += 2
                continue
            if c == ".":
                tokens.append("".join(cur))
                cur = []
            else:
                cur.append(c)
            i += 1
        tokens.append("".join(cur))
        if tokens and tokens[0] == "":
            tokens = tokens[1:]
        for t in tokens:
            if not isinstance(obj, dict) or t not in obj:
                return ""
            obj = obj[t]
        if isinstance(obj, (dict, list)):
            return json.dumps(obj, ensure_ascii=False)
        return str(obj)

    def main(argv):
        if len(argv) != 8 or argv[1:7] != [
            "get", "cm", "-n", "autopilot", "ops-health-report", "-o"
        ]:
            sys.stderr.write("unexpected kubectl args: %s\\n" % (argv[1:],))
            return 2
        arg = argv[7]
        if not arg.startswith("jsonpath="):
            sys.stderr.write("expected -o jsonpath, got: %s\\n" % arg)
            return 2
        cm = json.load(open(os.environ["SHIM_CM_FILE"]))
        out = resolve(cm, arg[len("jsonpath="):])
        if out:
            print(out)
        return 0

    if __name__ == "__main__":
        sys.exit(main(sys.argv))
    """
)

# 実スキーマに即した kubelet stats/summary fixture (test_root_disk_usage.py と同一)
SUMMARY = {
    "node": {
        "nodeName": "node01",
        "fs": {
            "availableBytes": 179000000000,
            "capacityBytes": 270000000000,
            "usedBytes": 74000000000,
        },
        "runtime": {"imageFs": {"usedBytes": 45000000000}},
        "pods": [
            {"podRef": {"name": "p1"}, "volume": [{"name": "data", "fs": {"usedBytes": 1000000000}}]},
            {"podRef": {"name": "p2"}, "volume": [{"name": "home", "fs": {"usedBytes": 250000000}}]},
        ],
    }
}


def _load_download_budget():
    spec = importlib.util.spec_from_file_location("download_budget_under_test", DOWNLOAD_BUDGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_report_namespace():
    """report.py のトップレベル FunctionDef と Assign だけを AST 抽出して実行する。

    副作用のあるモジュール先頭 (SA トークン読みは With 文、SSL_CTX は ssl 呼び出し) は
    抽出から外れるか偽物 ssl で無害化する。k8s 到達関数 (k8s_get / k8s_get_text /
    k8s_request) は偽物に差し替えるため、SA_TOKEN / SSL_CTX は参照されない。
    """
    tree = ast.parse(REPORT.read_text())
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            body.append(node)
        elif isinstance(node, ast.Assign):
            body.append(node)
    module = ast.Module(body=body, type_ignores=[])
    ns = {
        "datetime": datetime,
        "json": json,
        "os": os,
        "re": re,
        "urllib": urllib,
        # SSL_CTX = ssl.create_default_context(...) を無害化する (偽物の k8s 層に
        # 差し替えるため実 ctx は不要)
        "ssl": types.SimpleNamespace(create_default_context=lambda *a, **k: None),
        "root_disk_usage": root_disk_usage,
        "node_saturation": node_saturation,
        "download_budget": _load_download_budget(),
    }
    exec(compile(ast.fix_missing_locations(module), "<report_root_disk>", "exec"), ns)
    return ns


class FakeK8s:
    """k8s_get の偽物。知らない path は {"items": []} (空クラスタ相当) を返す。"""

    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path.startswith("/apis/apps/v1/namespaces/autopilot/deployments/"):
            return {"status": {"replicas": 1, "readyReplicas": 1, "unavailableReplicas": 0}}
        if "configmaps/" in path:
            return {"data": {}}
        return {"items": []}


class FakeWriter:
    """k8s_request の偽物 (put_configmap 専用)。ConfigMap 未作成 → POST で作る。"""

    def __init__(self):
        self.calls = []
        self.written = None

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return 404, None
        if method == "POST":
            self.written = body
            return 201, body
        return 500, None


class ReportRootDiskContractTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k) for k in ("HEALTH_NAMESPACE", "HEALTH_CONFIGMAP")
        }
        os.environ["HEALTH_NAMESPACE"] = "autopilot"
        os.environ["HEALTH_CONFIGMAP"] = "ops-health-report"
        self.rep = load_report_namespace()
        self.k8s = FakeK8s()
        self.writer = FakeWriter()
        self.rep["k8s_get"] = self.k8s
        self.rep["k8s_get_text"] = lambda path: (_ for _ in ()).throw(AssertionError("offline"))
        self.rep["k8s_request"] = self.writer
        # root_disk_usage の取得源は summary 優先。RBAC 経路をオフラインで通すため
        # その k8s_get も summary を返す偽物に差し替え、テスト後に戻す
        self._ru_calls = []
        self._original_ru_k8s_get = root_disk_usage.k8s_get

        def fake_ru_k8s_get(path):
            self._ru_calls.append(path)
            if path.endswith("/proxy/stats/summary"):
                return SUMMARY
            raise OSError("offline: no cluster")

        root_disk_usage.k8s_get = fake_ru_k8s_get
        self.addCleanup(self._restore)

    def _restore(self):
        root_disk_usage.k8s_get = self._original_ru_k8s_get
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run_main(self):
        self.rep["main"]()
        self.assertIsNotNone(self.writer.written, "put_configmap が呼ばれていない")
        raw = self.writer.written["data"][self.rep["HEALTH_KEY"]]
        return json.loads(raw)

    def test_latest_json_has_root_disk_with_fill_days(self):
        latest = self._run_main()
        # 受入検証そのもの (kubectl 以外の部分) を main() の出力に流す
        proc = subprocess.run(
            ["python3", "-c", SPEC_VERIFY_SNIPPET],
            input=json.dumps(latest),
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_root_disk_source_is_kubelet_summary_with_breakdown(self):
        latest = self._run_main()
        rd = latest["root_disk"]
        self.assertEqual(rd["source"], "kubelet_summary")
        self.assertEqual(rd["used_bytes"], 74000000000)
        self.assertEqual(rd["breakdown"]["images_bytes"], 45000000000)
        self.assertEqual(rd["breakdown"]["local_path_pvc_bytes"], 1250000000)
        # 非特権 pod から読めない内訳は None (計測不能) で正直に載る
        self.assertIsNone(rd["breakdown"]["k3s_bytes"])
        self.assertTrue(
            any(p.endswith("/proxy/stats/summary") for p in self._ru_calls),
            "kubelet summary 経路 (RBAC で追加した nodes/proxy) が呼ばれていない",
        )

    def test_history_written_in_same_put_as_latest(self):
        self._run_main()
        data = self.writer.written["data"]
        self.assertIn(self.rep["ROOT_DISK_HISTORY_KEY"], data)
        history = json.loads(data[self.rep["ROOT_DISK_HISTORY_KEY"]])
        self.assertEqual(len(history["samples"]), 1)
        self.assertEqual(history["samples"][0]["used_bytes"], 74000000000)
        # latest.json と履歴は同じ 1 回の PUT (resourceVersion 競合の 409 回避)
        writes = [c for c in self.writer.calls if c[0] in ("POST", "PUT")]
        self.assertEqual(len(writes), 1)

    def test_first_run_fill_days_is_none_with_note(self):
        latest = self._run_main()
        rd = latest["root_disk"]
        # 受入検証はキー存在のみで green になるが、予報は履歴が 1 日分溜まってから
        self.assertIsNone(rd["fill_days"])
        self.assertIsNotNone(rd["fill_days_note"])

    def _run_verify_command(self, jsonpath_flag):
        """kubectl 偽物を PATH に差し込み、受入検証の形 (namespace/name・-o jsonpath・
        2>/dev/null・パイプ・python 断片) でコマンドを実行して返す。

        kubectl 偽物は実 kubectl の jsonpath 解釈を忠実に模すため、jsonpath_flag の
        形 (エスケープの有無) によって出力が変わる。残るのはクラスタ到達と reporter
        の実実行のみ。
        """
        self._run_main()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cm_file = td / "configmap.json"
            cm_file.write_text(json.dumps(self.writer.written))
            shim = td / "kubectl"
            shim.write_text(KUBECTL_SHIM)
            shim.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = str(td) + os.pathsep + env.get("PATH", "")
            env["SHIM_CM_FILE"] = str(cm_file)
            cmd = (
                "kubectl get cm -n autopilot ops-health-report "
                + jsonpath_flag
                + "| python3 -c 'import json,sys; d=json.load(sys.stdin); "
                "assert d.get(\"root_disk\") and \"fill_days\" in d[\"root_disk\"]'"
            )
            return subprocess.run(
                cmd, shell=True, text=True, capture_output=True, env=env
            )

    def test_acceptance_kubectl_command_verbatim_unsatisfiable(self):
        """spec の verify[0] を**リテラル実行**し、実 kubectl の jsonpath では
        空出力 → JSONDecodeError (rc=1) になることを固定する (P-9062)。

        spec の jsonpath `{.data.latest.json}` は実 kubectl では入れ子
        (data["latest"]["json"]) と解釈され、ConfigMap の data キーが `latest.json`
        (リテラル) の場合は空出力になる。クラスタ到達が解決しても verify[0] は
        決して green にならない (spec レベルのバグ。mock apiserver + kubectl
        v1.35.0 で実測)。このテストはその事実を CI に閉じ、旧偽物のように
        「jsonpath がリテラルキーを返す」と誤って模して通ることを防ぐ。
        """
        proc = self._run_verify_command("-o jsonpath='{.data.latest.json}' 2>/dev/null ")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("JSONDecodeError", proc.stderr)

    def test_escaped_jsonpath_verify_command_passes(self):
        """CHARTER.md §5.5 の実測済み読み方 `{.data.latest\\.json}` (エスケープ) なら
        受入検証コマンドは通る。spec の verify[0] がこの形に修正されたときに
        green になることを、同じ偽物で固定する (P-9062)。
        """
        proc = self._run_verify_command(
            "-o jsonpath='{.data.latest\\.json}' 2>/dev/null "
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()