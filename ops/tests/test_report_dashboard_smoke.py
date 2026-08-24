"""report.py の dashboard_smoke 収集 (P-0193) の純関数部分を固定する。

report.py 自身は import 時に ServiceAccount token を読むため cluster 外からは
ロードできない。そこで test_report_externalsecrets.py と同じく、副作用を持たない
関数と定数だけを AST で取り出して名前空間に入れ、k8s_get は偽物に差し替えて試す。

固定する契約:
- 産出側 (autopilot ns の CronJob dashboard-smoke) が専用 ConfigMap の report.json
  キーに書いた run_smoke() 戻り値を読み、status ok/fail/stale/no_data に判定する
- 生 checks は載せず失敗した検査だけを出す (history jsonl 膨張止め)。
  detail は 200 文字で切り詰める (collect_externalsecrets の message と同じ上限)
- 鮮度を最優先で判定する: 古い fail より「装置の沈黙」を先に報せる。
  境界は > STALE_AFTER_S でのみ stale (ちょうどは沈黙扱いにしない)
- ランナーの代役レコード (rc=2, 装置故障。ok=False・failed_checks 空・
  tool_error/tool_error_rc 付き) には「描画断言が不合格」の文面を当てはめず、
  reason を tool_error 由来に分岐して切り詰めた tool_error も載せる。
  「ページの嘘」と「装置が回らなかった」を区別可能なまま保つ
- 形が壊れた記録 (ok が真偽値でない等) は例外 → no_data。「ページの嘘」と
  「帳簿の壊れ」を区別可能なまま、他の収集を止めない
"""

import ast
import datetime
import json
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "report.py"

FUNCTIONS = ("collect_dashboard_smoke", "_dashboard_smoke_summary")
CONSTANTS = ("DASHBOARD_SMOKE_NAMESPACE", "DASHBOARD_SMOKE_STALE_AFTER_S")


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
    exec(compile(ast.fix_missing_locations(module), "<report_dashboard_smoke>", "exec"), ns)
    return ns


rep = load_functions()

NOW = datetime.datetime(2026, 8, 23, 3, 0, 0, tzinfo=datetime.timezone.utc)


class _FixedDatetime(datetime.datetime):
    """collect_dashboard_smoke 内の datetime.datetime.now() を NOW に固定する。

    実時計のままだと stamp_ago(600) が実行日に応じて stale 判定へ倒れ、
    テストが日付で壊れる。strptime 等は datetime のサブクラスなのでそのまま効く。"""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


# 抽出した関数が参照する datetime モジュールを固定時計版に差し替える
rep["datetime"] = types.SimpleNamespace(
    datetime=_FixedDatetime, timezone=datetime.timezone
)


def stamp_ago(seconds):
    """NOW から seconds 秒前の generated_at (run_smoke の書式)。"""
    return (NOW - datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def smoke_result(**overrides):
    """run_smoke() 戻り値の実測形 (ops/projects/logs/P-0193/smoke-result.json)。"""
    payload = {
        "schema": 1,
        "tool": "dashboard_smoke",
        "project": "P-0193",
        "generated_at": stamp_ago(600),
        "url": "http://ops-dashboard.autopilot.svc",
        "http_status": 200,
        "checks": [
            {"name": "http-status", "status": "pass", "detail": "HTTP 200"},
            {"name": "render-complete-flow", "status": "pass", "detail": ".loading が消えた"},
            {"name": "rendered-masthead", "status": "pass", "detail": "masthead の MISSION CONTROL 表示"},
            {"name": "non-blank", "status": "pass", "detail": "可視テキスト 1368 文字 (>= 200)"},
            {"name": "section-heartbeat", "status": "pass", "detail": "鼓動チップ x1"},
            {"name": "no-lie-coexistence", "status": "pass", "detail": "正常チップ単独"},
            {"name": "heartbeat-fresh", "status": "pass", "detail": "LAST HEART 08/23 11:59:08 JST (52 秒前)"},
        ],
        "failed_checks": [],
        "ok": True,
        "dom_bytes": 10418,
        "elapsed_s": 5.43,
        "screenshot": {
            "path": "/tmp/smoke-result.png",
            "bytes": 66089,
            "sha256": "6f60dc65",
            "view": "live",
        },
    }
    payload.update(overrides)
    return payload


def fallback_result(rc=2, stderr_tail="chromium を起動できない: [Errno 2]", **overrides):
    """ランナー (dashboard-smoke-cronjob.yaml) の代役レコードの実測形。"""
    payload = {
        "schema": 1,
        "tool": "dashboard_smoke",
        "project": "P-0193",
        "generated_at": stamp_ago(600),
        "ok": False,
        "tool_error_rc": rc,
        "tool_error": stderr_tail,
        "checks": [],
        "failed_checks": [],
    }
    payload.update(overrides)
    return payload


def summarize(payload, now=NOW):
    return rep["_dashboard_smoke_summary"](payload, now)


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
    return rep["collect_dashboard_smoke"](), calls


class SummaryOkTest(unittest.TestCase):
    def test_fresh_ok_is_ok_with_totals(self):
        out = summarize(smoke_result())
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["ok"])
        self.assertEqual(out["checks_total"], 7)
        self.assertEqual(out["failed_checks"], [])
        self.assertEqual(out["age_seconds"], 600)
        self.assertEqual(out["url"], "http://ops-dashboard.autopilot.svc")
        self.assertEqual(out["http_status"], 200)
        self.assertAlmostEqual(out["elapsed_s"], 5.43)

    def test_screenshot_and_raw_checks_are_not_carried(self):
        # Pod 内一時ファイルへの path と生 checks は載せない (契約)
        out = summarize(smoke_result())
        self.assertNotIn("screenshot", out)
        self.assertNotIn("checks", out)
        self.assertNotIn("dom_bytes", out)

    def test_stale_boundary_exact_age_stays_ok(self):
        # ちょうど上限では鳴らさない (> でのみ stale。heartbeat-fresh と同じ倒し方)
        exact = summarize(smoke_result(generated_at=stamp_ago(rep["DASHBOARD_SMOKE_STALE_AFTER_S"])))
        self.assertEqual(exact["status"], "ok")

    def test_one_second_past_boundary_is_stale(self):
        over = summarize(
            smoke_result(generated_at=stamp_ago(rep["DASHBOARD_SMOKE_STALE_AFTER_S"] + 1))
        )
        self.assertEqual(over["status"], "stale")
        self.assertIn("沈黙", over["reason"])

    def test_negative_age_clock_skew_does_not_break_judgement(self):
        # 未来時刻 (node 間 clock skew) も stale 扱いにはしない。ok 判定を優先し
        # 経過秒は負のまま正直に出す
        out = summarize(smoke_result(generated_at=stamp_ago(-30)))
        self.assertEqual(out["status"], "ok")
        self.assertLessEqual(out["age_seconds"], -30)


class SummaryFailTest(unittest.TestCase):
    def test_failed_checks_are_named_with_truncated_detail(self):
        long_detail = "正常チップと異常表示が共存: global-warning: " + "x" * 500
        payload = smoke_result(
            ok=False,
            failed_checks=["no-lie-coexistence"],
            checks=[
                {"name": "no-lie-coexistence", "status": "fail", "detail": long_detail},
                {"name": "http-status", "status": "pass", "detail": "HTTP 200"},
            ],
        )
        out = summarize(payload)
        self.assertEqual(out["status"], "fail")
        self.assertIn("no-lie-coexistence", out["reason"])
        self.assertEqual(len(out["failed_checks"]), 1)
        self.assertEqual(len(out["failed_checks"][0]["detail"]), 200)

    def test_non_dict_entries_in_checks_are_skipped_not_fatal(self):
        payload = smoke_result(checks=["garbage", None])
        out = summarize(payload)
        self.assertEqual(out["status"], "ok")  # fail 判定できる検査が無いため沈黙しない
        self.assertEqual(out["failed_checks"], [])
        self.assertEqual(out["checks_total"], 2)


class SummaryToolErrorTest(unittest.TestCase):
    """rc=2 の代役レコード (装置故障) と「ページの嘘」の区別を固定する。"""

    def test_fallback_record_reason_branches_on_tool_error(self):
        out = summarize(fallback_result())
        self.assertEqual(out["status"], "fail")
        self.assertIn("異常終了", out["reason"])
        self.assertIn("装置が回らなかった", out["reason"])
        self.assertNotIn("描画断言が不合格", out["reason"])
        self.assertEqual(out["tool_error"], "chromium を起動できない: [Errno 2]")
        self.assertEqual(out["tool_error_rc"], 2)
        self.assertEqual(out["checks_total"], 0)
        self.assertEqual(out["failed_checks"], [])

    def test_tool_error_is_truncated(self):
        out = summarize(fallback_result(stderr_tail="e" * 500))
        self.assertEqual(len(out["tool_error"]), 200)

    def test_non_string_tool_error_is_ignored(self):
        # tool_error が文字列でない記録は代役とは断定できない。
        # 通常の fail 経路に落とし、内訳が空であることは正直に出す
        out = summarize(smoke_result(ok=False, failed_checks=[], checks=[]))
        self.assertEqual(out["status"], "fail")
        self.assertNotIn("tool_error", out)
        self.assertNotIn("tool_error_rc", out)
        self.assertIn("内訳が記録されていない", out["reason"])

    def test_blank_tool_error_is_ignored(self):
        out = summarize(fallback_result(tool_error="   ", tool_error_rc=None))
        self.assertNotIn("tool_error", out)
        self.assertNotIn("tool_error_rc", out)

    def test_stale_still_beats_tool_error(self):
        # 鮮度最優先は代役レコードでも変わらない: 古い記録の原因よりも
        # 「装置が沈黙していること」を先に報せる
        out = summarize(fallback_result(generated_at=stamp_ago(27 * 3600)))
        self.assertEqual(out["status"], "stale")
        self.assertIn("沈黙", out["reason"])

    def test_collect_passes_fields_through(self):
        out, _ = collect(fallback_result())
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["tool_error_rc"], 2)


class SummaryMalformedTest(unittest.TestCase):
    def test_non_bool_ok_raises(self):
        for bad in (None, "true", 1, [True]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    summarize(smoke_result(ok=bad))

    def test_missing_or_unparsable_generated_at_raises(self):
        with self.assertRaises(ValueError):
            summarize(smoke_result(generated_at=None))
        with self.assertRaises(ValueError):
            summarize(smoke_result(generated_at="2026/08/23 12:00:00"))

    def test_non_list_checks_raise(self):
        with self.assertRaises(ValueError):
            summarize(smoke_result(checks="all-pass"))


class CollectDashboardSmokeTest(unittest.TestCase):
    def test_api_path_is_namespaced_configmap(self):
        out, calls = collect(smoke_result())
        self.assertEqual(out["status"], "ok")
        self.assertEqual(calls, ["/api/v1/namespaces/autopilot/configmaps/dashboard-smoke"])

    def test_missing_key_is_no_data(self):
        def fake_k8s_get(_path):
            return {"data": {}}

        rep["k8s_get"] = fake_k8s_get
        out = rep["collect_dashboard_smoke"]()
        self.assertEqual(out["status"], "no_data")
        self.assertIn("report.json", out["error"])

    def test_api_failure_is_no_data_not_fatal(self):
        out, _ = collect(FileNotFoundError(2, "connect refused"))
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["reason"], "configmap dashboard-smoke を読めない")

    def test_broken_json_is_no_data(self):
        def fake_k8s_get(_path):
            return {"data": {"report.json": "{not-json"}}

        rep["k8s_get"] = fake_k8s_get
        out = rep["collect_dashboard_smoke"]()
        self.assertEqual(out["status"], "no_data")

    def test_non_dict_payload_is_no_data(self):
        out, _ = collect(["a", "b"])
        self.assertEqual(out["status"], "no_data")

    def test_malformed_payload_reports_error_string(self):
        out, _ = collect(smoke_result(ok="yes"))
        self.assertEqual(out["status"], "no_data")
        self.assertIn("真偽値", out["error"])

    def test_fail_passes_through_with_reason(self):
        payload = smoke_result(
            ok=False,
            failed_checks=["render-complete"],
            checks=[{"name": "render-complete", "status": "fail", "detail": "スピナ残置"}],
        )
        out, _ = collect(payload)
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["failed_checks"][0]["name"], "render-complete")

    def test_stale_overrides_fail_for_old_records(self):
        payload = smoke_result(
            ok=False,
            generated_at=stamp_ago(rep["DASHBOARD_SMOKE_STALE_AFTER_S"] + 3600),
        )
        out, _ = collect(payload)
        self.assertEqual(out["status"], "stale")


if __name__ == "__main__":
    unittest.main()
