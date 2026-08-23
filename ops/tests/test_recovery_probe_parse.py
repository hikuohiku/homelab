"""report.py / recovery_probe.py の recovery_probe 収集 (P-0258) を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_recovery_probe_parse -v`。

2 種類のロードを併用する (確立済みの流儀どおり):
- apps/ops-health-reporter/recovery_probe.py — import 副作用を持たない単一ファイル
  モジュールなので importlib で実ファイルを直接ロードする (test_download_budget.py
  と同じ形)。パース/要約の契約はこちらで固定する
- report.py の collect_recovery_probe — report.py 自身は import 時に ServiceAccount
  token を読むため cluster 外からはロードできない。test_report_dashboard_smoke.py
  と同じく、副作用を持たない関数だけを AST で取り出し、k8s_get は偽物に差し替える

固定する契約:
- 産出側 (recovery-canary ns の CronJob) が専用 ConfigMap の report.json キーに
  書いた記録を読み、status ok/fail/stale/no_data に判定する
- ok=true の成功レコードだけが last_recovery_seconds (int) を載せる。
  bool・非整数・負値・欠損は帳簿の壊れとして no_data (verify 3 の assert 条件と整合)
- 失敗レコード (ok=false, phase/error 付き) から秒数は載せない。産出側が誤って
  秒数を持たせていても無視する — 「失敗した夜」に秒数が並ぶと履歴が嘘をつく
- 鮮度を最優先で判定する: 古い記録より「装置の沈黙」を先に報せる。
  境界は > STALE_AFTER_S でのみ stale。stale には秒数/phase も載せない
- 形が壊れた記録は例外 → no_data。「装置の故障」と「帳簿の壊れ」を区別可能なまま、
  他の収集を止めない
"""

import ast
import datetime
import importlib.util
import json
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PURE_MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "recovery_probe.py"
REPORT_PATH = REPO / "apps" / "ops-health-reporter" / "report.py"


def _load_pure_module():
    spec = importlib.util.spec_from_file_location("recovery_probe_under_test", PURE_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rp = _load_pure_module()


def load_collect():
    """report.py から collect_recovery_probe だけを AST 抽出して exec する。

    関数本体が参照する recovery_probe モジュールは実物 (純関数のみ) を注入する。
    """
    tree = ast.parse(REPORT_PATH.read_text())
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "collect_recovery_probe"]
    assert len(body) == 1, "report.py から collect_recovery_probe を抽出できない"
    module = ast.Module(body=body, type_ignores=[])
    ns = {"datetime": datetime, "json": json, "recovery_probe": rp}
    exec(compile(ast.fix_missing_locations(module), "<report_recovery_probe>", "exec"), ns)
    return ns


rep = load_collect()

NOW = datetime.datetime(2026, 8, 25, 18, 43, 12, tzinfo=datetime.timezone.utc)


def stamp_ago(seconds):
    """NOW から seconds 秒前の時刻 (産出側ランナーの iso() と同じ書式)。"""
    return (NOW - datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def success_record(**overrides):
    """産出側ランナー build_report() の成功レコードの実測形 (PROGRESS.md 契約節)。"""
    payload = {
        "schema": 1,
        "tool": "recovery_canary",
        "project": "P-0258",
        "generated_at": stamp_ago(600),
        "ok": True,
        "deleted_at": stamp_ago(787),
        "namespace": "recovery-canary",
        "deployment": "recovery-canary",
        "last_recovery_seconds": 187,
        "ready_at": stamp_ago(600),
    }
    payload.update(overrides)
    return payload


def failure_record(**overrides):
    """産出側ランナー build_report() の失敗レコードの実測形。"""
    payload = {
        "schema": 1,
        "tool": "recovery_canary",
        "project": "P-0258",
        "generated_at": stamp_ago(600),
        "ok": False,
        "deleted_at": stamp_ago(1800),
        "namespace": "recovery-canary",
        "deployment": "recovery-canary",
        "phase": "wait-recreate",
        "error": "1200 秒以内に selfHeal による再作成が起こらない (ArgoCD が止まっている疑い)",
    }
    payload.update(overrides)
    return payload


def summarize(payload, now=NOW):
    return rp.build_summary(payload, now)


def collect(payload_or_exception, now=NOW):
    calls = []

    def fake_k8s_get(path):
        calls.append(path)
        if isinstance(payload_or_exception, Exception):
            raise payload_or_exception
        return {"data": {"report.json": json.dumps(payload_or_exception, ensure_ascii=False)}}

    rep["k8s_get"] = fake_k8s_get
    freeze_clock(now)
    try:
        return rep["collect_recovery_probe"](), calls
    finally:
        unfreeze_clock()


def freeze_clock(now=NOW):
    """collect_recovery_probe 内の datetime.datetime.now を固定する。

    壁時計に依存させると stale 判定が「いつテストを実行したか」で変わる。
    test_report_dashboard_smoke.py の collect 系 stale テストは fixture 時刻
    (NOW 定数) と実壁時の偶一致で成り立っている潜在フラワなので、ここでは
    AST 抽出の名前空間ごと凍結して決定論的にする。
    """
    # 引数式 datetime.timezone.utc はグローバル名 datetime (外側) から、
    # .now() はその .datetime (内側) から解決される。両方に生やす
    rep["datetime"] = types.SimpleNamespace(
        timezone=datetime.timezone,
        datetime=types.SimpleNamespace(
            now=lambda tz=None: now, timezone=datetime.timezone
        ),
    )


def unfreeze_clock():
    rep["datetime"] = datetime


class ParseUtcTest(unittest.TestCase):
    def test_accepts_only_producer_format(self):
        parsed = rp.parse_utc("2026-08-25T18:43:12Z")
        self.assertEqual(parsed, NOW)
        self.assertIsNone(rp.parse_utc("2026/08/25 18:43:12"))
        self.assertIsNone(rp.parse_utc("2026-08-25T18:43:12"))
        self.assertIsNone(rp.parse_utc(None))
        self.assertIsNone(rp.parse_utc(123))


class CoerceSecondsTest(unittest.TestCase):
    def test_int_passes_and_bool_is_rejected(self):
        self.assertEqual(rp.coerce_seconds(187), 187)
        self.assertEqual(rp.coerce_seconds(0), 0)  # 即復旧も正当な計測値
        # bool は int の派生なので明示的に弾く (download_budget.coerce_bytes と同じ倒し方)
        self.assertIsNone(rp.coerce_seconds(True))
        self.assertIsNone(rp.coerce_seconds(False))
        self.assertIsNone(rp.coerce_seconds("187"))
        self.assertIsNone(rp.coerce_seconds(-1))
        self.assertIsNone(rp.coerce_seconds(None))


class SummaryOkTest(unittest.TestCase):
    def test_fresh_ok_carries_int_seconds(self):
        out = summarize(success_record())
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["ok"])
        # verify 3 が要求する形: last_recovery_seconds は int (bool でない)
        self.assertIsInstance(out["last_recovery_seconds"], int)
        self.assertNotIsInstance(out["last_recovery_seconds"], bool)
        self.assertEqual(out["last_recovery_seconds"], 187)
        self.assertEqual(out["age_seconds"], 600)
        self.assertEqual(out["deleted_at"], stamp_ago(787))
        self.assertEqual(out["ready_at"], stamp_ago(600))

    def test_zero_seconds_is_a_valid_measurement(self):
        # reconciliation が即時でも 0 秒は正当。壊れ扱いしない
        out = summarize(success_record(last_recovery_seconds=0))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["last_recovery_seconds"], 0)

    def test_negative_age_clock_skew_does_not_break_judgement(self):
        # 未来時刻 (node 間 clock skew) も stale 扱いにはしない。ok 判定を優先し
        # 経過秒は負のまま正直に出す (_dashboard_smoke_summary と同じ)
        record = success_record(generated_at=stamp_ago(-30))
        record["ready_at"] = stamp_ago(-10)
        out = summarize(record)
        self.assertEqual(out["status"], "ok")
        self.assertLessEqual(out["age_seconds"], -30)

    def test_extra_fields_are_not_carried(self):
        # 生レコードの tool/project/schema/namespace は定数なので載せない
        # (history jsonl の 1 行膨張止め。キー名自体が文脈を持つ)
        out = summarize(success_record())
        for absent in ("schema", "tool", "project", "namespace", "deployment"):
            self.assertNotIn(absent, out)


class SummarySecondsValidationTest(unittest.TestCase):
    def test_broken_seconds_raise_for_no_data(self):
        # ok=true なのに秒数が壊れているのは帳簿の壊れ。黙って fail 扱いにせず
        # ValueError → 呼び出し側が no_data へ落とす
        for bad in (None, "187", 187.0, True, -5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    summarize(success_record(last_recovery_seconds=bad))

    def test_missing_seconds_raise(self):
        record = success_record()
        del record["last_recovery_seconds"]
        with self.assertRaises(ValueError):
            summarize(record)


class SummaryFailTest(unittest.TestCase):
    def test_failure_keeps_phase_and_truncated_error(self):
        long_error = "削除後 1200 秒経っても対象 Deployment の消失を確認できない" + "x" * 300
        out = summarize(failure_record(error=long_error))
        self.assertEqual(out["status"], "fail")
        self.assertFalse(out["ok"])
        self.assertEqual(out["phase"], "wait-recreate")
        self.assertEqual(len(out["error"]), 200)
        self.assertIn("phase=wait-recreate", out["reason"])

    def test_failure_never_carries_seconds(self):
        # 産出側の契約では失敗レコードに秒数は無いが、仮に誤って持たせても
        # 集約側は絶対に載せない (秒数の捏造をしないため)
        forged = failure_record(last_recovery_seconds=42)
        out = summarize(forged)
        self.assertEqual(out["status"], "fail")
        self.assertNotIn("last_recovery_seconds", out)

    def test_garbage_phase_or_error_do_not_crash(self):
        out = summarize(failure_record(phase="", error=None))
        self.assertEqual(out["status"], "fail")
        self.assertNotIn("phase", out)
        self.assertNotIn("error", out)
        self.assertIn("unknown", out["reason"])
        out = summarize(failure_record(phase=["delete"], error={"msg": 1}))
        self.assertEqual(out["status"], "fail")
        self.assertIn("unknown", out["reason"])

    def test_all_producer_phases_pass_through(self):
        for phase in ("delete", "wait-deletion", "wait-recreate", "wait-ready"):
            out = summarize(failure_record(phase=phase))
            self.assertEqual(out["status"], "fail")
            self.assertEqual(out["phase"], phase)


class SummaryStaleTest(unittest.TestCase):
    def test_exact_boundary_stays_not_stale(self):
        exact = summarize(
            success_record(generated_at=stamp_ago(rp.STALE_AFTER_S))
        )
        self.assertEqual(exact["status"], "ok")

    def test_one_second_past_boundary_is_stale(self):
        over = summarize(
            success_record(generated_at=stamp_ago(rp.STALE_AFTER_S + 1))
        )
        self.assertEqual(over["status"], "stale")
        self.assertIn("沈黙", over["reason"])

    def test_stale_beats_fail(self):
        # 古い失敗記録の原因よりも「装置が沈黙していること」を先に報せる
        out = summarize(failure_record(generated_at=stamp_ago(27 * 3600)))
        self.assertEqual(out["status"], "stale")

    def test_stale_does_not_carry_seconds_nor_phase(self):
        # 昨晩の数字を今日の状態のように見せない (鮮度最優先の契約)
        out = summarize(success_record(generated_at=stamp_ago(rp.STALE_AFTER_S + 3600)))
        self.assertEqual(out["status"], "stale")
        self.assertNotIn("last_recovery_seconds", out)
        out = summarize(failure_record(generated_at=stamp_ago(rp.STALE_AFTER_S + 3600)))
        self.assertNotIn("phase", out)


class SummaryMalformedTest(unittest.TestCase):
    def test_non_dict_payload_raises(self):
        with self.assertRaises(ValueError):
            summarize(["a", "b"])
        with self.assertRaises(ValueError):
            summarize(None)

    def test_non_bool_ok_raises(self):
        for bad in (None, "true", 1, [True]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    summarize(success_record(ok=bad))

    def test_missing_or_unparsable_generated_at_raises(self):
        with self.assertRaises(ValueError):
            summarize(success_record(generated_at=None))
        with self.assertRaises(ValueError):
            summarize(success_record(generated_at="2026/08/25 18:43:12"))


class CollectRecoveryProbeTest(unittest.TestCase):
    def test_api_path_is_namespaced_configmap(self):
        out, calls = collect(success_record())
        self.assertEqual(out["status"], "ok")
        self.assertEqual(calls, ["/api/v1/namespaces/recovery-canary/configmaps/recovery-probe"])

    def test_missing_key_is_no_data(self):
        rep["k8s_get"] = lambda _path: {"data": {}}
        out = rep["collect_recovery_probe"]()
        self.assertEqual(out["status"], "no_data")
        self.assertIn("report.json", out["error"])

    def test_api_failure_is_no_data_not_fatal(self):
        out, _ = collect(FileNotFoundError(2, "connect refused"))
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["reason"], "configmap recovery-probe を読めない")

    def test_broken_json_is_no_data(self):
        rep["k8s_get"] = lambda _path: {"data": {"report.json": "{not-json"}}
        out = rep["collect_recovery_probe"]()
        self.assertEqual(out["status"], "no_data")

    def test_non_dict_payload_is_no_data(self):
        out, _ = collect([1, 2])
        self.assertEqual(out["status"], "no_data")

    def test_malformed_payload_reports_error_string(self):
        out, _ = collect(success_record(ok="yes"))
        self.assertEqual(out["status"], "no_data")
        self.assertIn("真偽値", out["error"])

    def test_fail_passes_through_with_phase(self):
        # RBAC 漏れの切り分け手がかり (phase=delete + HTTP 403 文面) が
        # latest.json まで届くこと
        payload = failure_record(phase="delete", error="対象 Deployment の削除に失敗: HTTP 403")
        out, _ = collect(payload)
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["phase"], "delete")
        self.assertIn("403", out["error"])

    def test_stale_overrides_ok_for_old_records(self):
        out, _ = collect(success_record(generated_at=stamp_ago(rp.STALE_AFTER_S + 3600)))
        self.assertEqual(out["status"], "stale")


if __name__ == "__main__":
    unittest.main()
