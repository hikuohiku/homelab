"""report.py の ExternalSecret 収集 (P-0175) の純関数部分を固定する。

report.py 自身は import 時に ServiceAccount token を読むため cluster 外からは
ロードできない (check_health_reporter_target.py の冒頭参照)。そこで
test_download_ledger_script.py と同じく、副作用を持たない関数と定数だけを
AST で取り出して名前空間に入れ、k8s_get は偽物に差し替えて試す。
時刻は差し替えない (now を見る代わりに refreshTime 側を実現在時刻から逆算し、
±5 秒の許容で比較する)。

固定する契約 (どれも実測に基づく):
- spec.refreshInterval は K8s duration 文字列 ("1h" / "30m") で来る。数値前提で
  パースすると None になる (P-0175 セッション1 の実測)。複合形 ("1h30m")・
  単位の無い数字列も受け、決められない入力は例外ではなく None
- status.refreshTime は秒精度 UTC ("2026-08-23T10:16:07Z"、2026-08-23 実クラスタ
  実測)。この書式以外が来たらその 1 item を error エントリに落として止めない
- SecretSyncedError は件数と対象名に出し、message は history jsonl を膨らませない
  ため切り詰める。target Secret 未作成で refreshTime が無い item
  (syncthing-photo-intake-credentials が実例) は last_sync_age_seconds=None
- 全体ソートは (namespace, name)。error エントリ (name 無し) が混ざっても
  比較で落ちない
"""

import ast
import datetime
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "ops-health-reporter" / "report.py"

FUNCTIONS = ("_duration_seconds", "collect_externalsecrets")
CONSTANTS = ("_DURATION_UNITS",)


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
    exec(compile(ast.fix_missing_locations(module), "<report_externalsecrets>", "exec"), ns)
    return ns


rep = load_functions()

AGE_TOLERANCE = 5


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def refresh_time_ago(seconds):
    return (now() - datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def es_item(namespace, name, *, interval="1h", refresh_time=None,
            ready="True", reason="SecretSynced", message="secret synced"):
    conditions = []
    if ready is not None:
        conditions.append(
            {"type": "Ready", "status": ready, "reason": reason, "message": message,
             "lastTransitionTime": "2026-08-01T00:00:00Z"}
        )
    status = {"conditions": conditions}
    if refresh_time:
        status["refreshTime"] = refresh_time
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"refreshInterval": interval},
        "status": status,
    }


def collect(items):
    """collect_externalsecrets() を k8s_get 差し替えで実行する。

    関数の __globals__ は load_functions() の exec に使った ns 自身なので、
    その dict に直接書き込む (dict(rep) とコピーしても globals は変わらない)。
    """
    calls = []

    def fake_k8s_get(path):
        calls.append(path)
        if not path.startswith("/apis/external-secrets.io/v1/"):
            raise AssertionError(f"想定外の API パス: {path}")
        return {"items": items}

    rep["k8s_get"] = fake_k8s_get
    return rep["collect_externalsecrets"](), calls


class DurationSecondsTest(unittest.TestCase):
    def test_duration_strings_from_live_cluster(self):
        # P-0175 セッション1 の罠: refreshInterval は文字列で来る
        self.assertEqual(rep["_duration_seconds"]("1h"), 3600)
        self.assertEqual(rep["_duration_seconds"]("30m"), 1800)

    def test_compound_and_bare_number(self):
        self.assertEqual(rep["_duration_seconds"]("1h30m"), 5400)
        self.assertEqual(rep["_duration_seconds"]("90s"), 90)
        # 単位の無い数字列は秒とみなす (数値が文字列で来る API 版に備える)
        self.assertEqual(rep["_duration_seconds"]("3600"), 3600)
        self.assertEqual(rep["_duration_seconds"]("0"), 0)

    def test_numeric_accepted_bool_rejected(self):
        self.assertEqual(rep["_duration_seconds"](3600), 3600)
        self.assertEqual(rep["_duration_seconds"](1.5), 1)
        # bool は int の派生なので明示的に弾く (test_download_budget と同じ倒し方)
        self.assertIsNone(rep["_duration_seconds"](True))

    def test_undecidable_is_none_not_zero_not_exception(self):
        # 0 を返すと「経過秒との比較」ですべて即滞留扱いになり、計器が静かに嘘をつく
        for garbage in ("", "abc", None, "h", "1x", "-1h", "1h30x"):
            with self.subTest(garbage=garbage):
                self.assertIsNone(rep["_duration_seconds"](garbage))


class CollectExternalSecretsTest(unittest.TestCase):
    def test_api_path_is_v1(self):
        # ESO は v1beta1 を提供していない (/v1 と /v1alpha1 のみ、実測)
        out, calls = collect([])
        self.assertEqual(out["total"], 0)
        self.assertEqual(calls, ["/apis/external-secrets.io/v1/externalsecrets"])

    def test_synced_item_shape(self):
        rt = refresh_time_ago(600)
        out, _ = collect([es_item("coder", "coder-db-url",
                                  interval="30m", refresh_time=rt)])
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["secret_synced_errors"], 0)
        item = out["items"][0]
        self.assertEqual(item["namespace"], "coder")
        self.assertEqual(item["ready"], "True")
        self.assertEqual(item["sync_reason"], "SecretSynced")
        self.assertEqual(item["refresh_interval_seconds"], 1800)
        self.assertAlmostEqual(item["last_sync_age_seconds"], 600, delta=AGE_TOLERANCE)

    def test_staleness_beyond_interval_is_visible(self):
        # 「静かな鮮度劣化」を見える化するのが目的: Synced のまま古いものも
        # last_sync_age_seconds > refresh_interval_seconds で分かる
        rt = refresh_time_ago(7200)
        out, _ = collect([es_item("immich", "immich-postgres-credentials",
                                  interval="1h", refresh_time=rt)])
        item = out["items"][0]
        self.assertGreater(item["last_sync_age_seconds"],
                           item["refresh_interval_seconds"])
        self.assertEqual(out["secret_synced_errors"], 0)  # まだエラー化していない

    def test_secret_synced_error_counted_with_truncated_message(self):
        long_message = "could not get secret data from provider: " + "x" * 500
        out, _ = collect([
            es_item("vaultwarden", "vaultwarden-admin-token", ready="False",
                    reason="SecretSyncedError", message=long_message),
        ])
        self.assertEqual(out["secret_synced_errors"], 1)
        self.assertEqual(out["errored"], ["vaultwarden: vaultwarden-admin-token"])
        self.assertEqual(len(out["items"][0]["message"]), 200)

    def test_errored_without_refresh_time_has_null_age(self):
        # target Secret 未作成の item (syncthing-photo-intake-credentials、実在の
        # 例) は最終成功同期が無いので経過秒も出せない。例外ではなく None で正直に出す
        out, _ = collect([
            es_item("syncthing", "syncthing-photo-intake-credentials",
                    interval="1h", refresh_time=None, ready="False",
                    reason="SecretSyncedError"),
        ])
        self.assertEqual(out["secret_synced_errors"], 1)
        self.assertIsNone(out["items"][0]["last_sync_age_seconds"])

    def test_malformed_item_does_not_stop_the_rest(self):
        broken = {
            "metadata": {"name": "broken", "namespace": "ns"},
            "spec": {},
            "status": {"refreshTime": "not-a-timestamp", "conditions": []},
        }
        out, _ = collect([es_item("aaa", "fine",
                                  refresh_time=refresh_time_ago(60)), broken])
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["secret_synced_errors"], 0)
        by_name = {i.get("name"): i for i in out["items"]}
        self.assertGreater(by_name["fine"]["last_sync_age_seconds"], 0)
        self.assertIn("error", by_name[None])

    def test_sorted_by_namespace_then_name_even_with_error_entries(self):
        items = [
            es_item("zzz", "b", refresh_time=refresh_time_ago(60)),
            es_item("aaa", "c", refresh_time=refresh_time_ago(60)),
            es_item("aaa", "a", refresh_time=refresh_time_ago(60)),
            {"metadata": {}, "spec": {}, "status": {}},  # error エントリ候補
        ]
        out, _ = collect(items)
        names = [i.get("name") for i in out["items"]]
        # error エントリ (name 無し) は空文字キーで先頭に寄る。契約は
        # 「混在で比較が落ちない」ことと、有効 item 同士の (namespace, name) 順
        valid = [n for n in names if n]
        self.assertEqual(valid, ["a", "c", "b"])


if __name__ == "__main__":
    unittest.main()
