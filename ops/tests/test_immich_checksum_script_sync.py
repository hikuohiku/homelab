"""ops/check_immich_checksum_script_sync.py と埋め込みスクリプトの健全性 (P-0361)。

判断ロジックの埋め込みが正 (ops/tools/immich_checksum_check.py) と一致していること、
埋め込みランナーがコンパイルできて API フロー (トリガー → 完了待ち → summary → 書込み)
を代役で通せること、失敗時に error の代役レコードを書くことを固定する。
クラスタにもネットワークにも出ない (API は mock)。
"""
import builtins
import io
import json
import os
import sys
import textwrap
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent

import ops.check_immich_checksum_script_sync as sync  # noqa: E402

MANIFEST = ROOT / "apps/immich/checksum-cronjob.yaml"

SA_FILES = {
    "/var/run/secrets/kubernetes.io/serviceaccount/namespace": "immich",
    "/var/run/secrets/kubernetes.io/serviceaccount/token": "sa-token",
    "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt": "fake-ca",
}


def script_configmap_data():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if (
            isinstance(doc, dict)
            and doc.get("kind") == "ConfigMap"
            and doc.get("metadata", {}).get("name") == "immich-checksum-script"
        ):
            return doc["data"]
    raise AssertionError("ConfigMap immich-checksum-script が見つからない")


def _install_embedded_tool():
    """埋め込み tool を `import immich_checksum_check` で解決できるよう登録する。

    ランナーは `/scripts/immich_checksum_check.py` を import する。テストでは同じ
    埋め込みソースをモジュールにして sys.modules へ入れ、import を成立させる。
    """
    mod = types.ModuleType("immich_checksum_check")
    exec(
        compile(script_configmap_data()["immich_checksum_check.py"], "immich_checksum_check.py", "exec"),
        mod.__dict__,
    )
    sys.modules["immich_checksum_check"] = mod
    return mod


def _exec_runner(env=None):
    """埋め込みランナーを、SA ファイルと ssl を代役にして exec し、globals を返す。

    実行後は呼び出し側が ns["immich_request"] / ns["put_configmap"] を差し替えて
    _call_main() で main() を呼ぶ。time.sleep は POLL_INTERVAL_S を 0 にすると
    実質無視できる (wait_for_checksum_run が呼び出し時に既定値を解決するため)。
    """
    runner = script_configmap_data()["checksum_runner.py"]
    ns = {}
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if isinstance(path, str) and path in SA_FILES:
            return io.StringIO(SA_FILES[path])
        return real_open(path, *args, **kwargs)

    with mock.patch.dict(os.environ, env or {}, clear=False):
        with mock.patch("builtins.open", side_effect=fake_open):
            with mock.patch("ssl.create_default_context", return_value=object()):
                exec(compile(runner, "checksum_runner.py", "exec"), ns)
    return ns


def _call_main(ns, env=None):
    """os.environ を適用した状態で埋め込みランナーの main() を呼ぶ。

    main() は実行時 (呼び出し時) に os.environ を読むため、_exec_runner の
    mock.patch.dict のスコープを抜けた後でも env が効くようにここで再適用する。
    """
    with mock.patch.dict(os.environ, env or {}, clear=False):
        return ns["main"]()


class TestDrift(unittest.TestCase):
    def test_embedded_matches_canonical(self):
        embedded = textwrap.dedent(sync.extract_block_scalar(sync.MANIFEST, sync.KEY))
        canonical = ROOT.joinpath(sync.CANONICAL).read_text()
        self.assertEqual(embedded.rstrip("\n"), canonical.rstrip("\n"))

    def test_main_ok(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = sync.main()
        self.assertEqual(rc, 0, out.getvalue())


class TestEmbeddedScripts(unittest.TestCase):
    def setUp(self):
        self.data = script_configmap_data()
        self.assertIn("immich_checksum_check.py", self.data)
        self.assertIn("checksum_runner.py", self.data)

    def test_tool_compiles(self):
        compile(self.data["immich_checksum_check.py"], "immich_checksum_check.py", "exec")

    def test_runner_compiles(self):
        compile(self.data["checksum_runner.py"], "checksum_runner.py", "exec")

    def test_embedded_tool_selftest(self):
        ns = {}
        exec(compile(self.data["immich_checksum_check.py"], "immich_checksum_check.py", "exec"), ns)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ns["run_selftest"]()
        self.assertEqual(rc, 0, out.getvalue())

    def test_runner_imports_tool(self):
        # ランナーが `/scripts/immich_checksum_check.py` を import する形になっていること
        self.assertIn("import immich_checksum_check as icc", self.data["checksum_runner.py"])

    def test_manifest_yaml_parses(self):
        kinds = [d.get("kind") for d in yaml.safe_load_all(MANIFEST.read_text())]
        self.assertEqual(
            kinds, ["ServiceAccount", "Role", "RoleBinding", "ConfigMap", "CronJob"]
        )


class TestRunnerFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = _install_embedded_tool()

    def _queue(self, active=0, completed=5, waiting=0):
        return {
            "name": "integrityCheck",
            "isPaused": False,
            "statistics": {"active": active, "completed": completed, "failed": 0, "delayed": 0, "waiting": waiting, "paused": 0},
        }

    def test_success_path_writes_ok_report(self):
        ns = _exec_runner({"IMMICH_API_KEY": "test-key"})
        ns["POLL_INTERVAL_S"] = 0
        queue_gets = {"n": 0}

        def fake_immich(method, path, body=None):
            if method == "POST" and path == "/api/jobs":
                self.assertEqual(body, {"name": "integrity-checksum-mismatch"})
                return 204, None
            if method == "GET" and path == "/api/queues/integrityCheck":
                queue_gets["n"] += 1
                if queue_gets["n"] == 1:  # トリガー前の基準読み
                    return 200, self._queue(active=0, completed=5)
                if queue_gets["n"] == 2:  # 実行中を観測
                    return 200, self._queue(active=1, completed=5)
                return 200, self._queue(active=0, completed=6)  # 完了
            if method == "GET" and path == "/api/admin/integrity/summary":
                return 200, {"checksum_mismatch": 0, "missing_file": 1, "untracked_file": 2}
            raise AssertionError("unexpected call: {} {}".format(method, path))

        written = {}

        def fake_put(name, data):
            written[name] = json.loads(data["report.json"])

        ns["immich_request"] = fake_immich
        ns["put_configmap"] = fake_put
        rc = _call_main(ns, {"IMMICH_API_KEY": "test-key", "MISMATCH_THRESHOLD": "1"})
        self.assertEqual(rc, 0)
        report = written["immich-checksum-report"]
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["ok"])
        self.assertEqual(report["checksum_mismatch"], 0)
        self.assertEqual(report["missing_file"], 1)
        self.assertEqual(report["namespace"], "immich")
        self.assertEqual(report["job"]["name"], "integrity-checksum-mismatch")
        self.assertIn("triggered_at", report["job"])
        self.assertIn("run_elapsed_s", report["job"])

    def test_unconfigured_without_threshold(self):
        # 閾値未設定 (rules.json の checksum.mismatch_threshold 未導入 = 現状の実装状態) では
        # status=unconfigured を正直に出す。黙って 0 を決め打ちしない
        ns = _exec_runner({"IMMICH_API_KEY": "test-key"})
        ns["POLL_INTERVAL_S"] = 0

        def fake_immich(method, path, body=None):
            if method == "POST" and path == "/api/jobs":
                return 204, None
            if method == "GET" and path == "/api/queues/integrityCheck":
                return 200, self._queue(active=0, completed=5)
            if method == "GET" and path == "/api/admin/integrity/summary":
                return 200, {"checksum_mismatch": 0, "missing_file": 0, "untracked_file": 0}
            raise AssertionError("unexpected call: {} {}".format(method, path))

        written = {}

        def fake_put(name, data):
            written[name] = json.loads(data["report.json"])

        ns["immich_request"] = fake_immich
        ns["put_configmap"] = fake_put
        rc = _call_main(ns, {"IMMICH_API_KEY": "test-key"})
        self.assertEqual(rc, 0)
        report = written["immich-checksum-report"]
        self.assertEqual(report["status"], "unconfigured")
        self.assertFalse(report["ok"])
        self.assertEqual(report["checksum_mismatch"], 0)

    def test_small_library_completes_without_observing_active(self):
        # ポーリング間に完了して active を一度も観測できない小規模ライブラリ。
        # 完了カウンタの増加 (baseline 5 → 6) で完了を検知して summary を読む
        ns = _exec_runner({"IMMICH_API_KEY": "test-key"})
        ns["POLL_INTERVAL_S"] = 0.01
        queue_gets = {"n": 0}

        def fake_immich(method, path, body=None):
            if method == "POST" and path == "/api/jobs":
                return 204, None
            if method == "GET" and path == "/api/queues/integrityCheck":
                queue_gets["n"] += 1
                if queue_gets["n"] == 1:  # 基準読み
                    return 200, self._queue(active=0, completed=5)
                return 200, self._queue(active=0, completed=6)  # 完了
            if method == "GET" and path == "/api/admin/integrity/summary":
                return 200, {"checksum_mismatch": 0, "missing_file": 1, "untracked_file": 2}
            raise AssertionError("unexpected call: {} {}".format(method, path))

        written = {}

        def fake_put(name, data):
            written[name] = json.loads(data["report.json"])

        ns["immich_request"] = fake_immich
        ns["put_configmap"] = fake_put
        rc = _call_main(ns, {"IMMICH_API_KEY": "test-key", "MISMATCH_THRESHOLD": "1"})
        self.assertEqual(rc, 0)
        self.assertEqual(written["immich-checksum-report"]["status"], "ok")

    def test_threshold_fail(self):
        ns = _exec_runner({"IMMICH_API_KEY": "test-key"})
        ns["POLL_INTERVAL_S"] = 0

        def fake_immich(method, path, body=None):
            if method == "POST" and path == "/api/jobs":
                return 204, None
            if method == "GET" and path == "/api/queues/integrityCheck":
                return 200, self._queue(active=0, completed=5)
            if method == "GET" and path == "/api/admin/integrity/summary":
                return 200, {"checksum_mismatch": 1, "missing_file": 0, "untracked_file": 0}
            raise AssertionError("unexpected call: {} {}".format(method, path))

        written = {}

        def fake_put(name, data):
            written[name] = json.loads(data["report.json"])

        ns["immich_request"] = fake_immich
        ns["put_configmap"] = fake_put
        rc = _call_main(ns, {"IMMICH_API_KEY": "test-key", "MISMATCH_THRESHOLD": "1"})
        self.assertEqual(rc, 0)
        report = written["immich-checksum-report"]
        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["ok"])

    def test_trigger_failure_writes_error_report(self):
        ns = _exec_runner({"IMMICH_API_KEY": "test-key"})

        def fake_immich(method, path, body=None):
            if method == "GET" and path == "/api/queues/integrityCheck":
                return 500, {"error": "boom"}
            raise AssertionError("unexpected call: {} {}".format(method, path))

        written = {}

        def fake_put(name, data):
            written[name] = json.loads(data["report.json"])

        ns["immich_request"] = fake_immich
        ns["put_configmap"] = fake_put
        rc = _call_main(ns, {"IMMICH_API_KEY": "test-key"})
        self.assertEqual(rc, 1)
        report = written["immich-checksum-report"]
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["ok"])
        self.assertIn("queue の取得に失敗", report["reason"])


if __name__ == "__main__":
    unittest.main()