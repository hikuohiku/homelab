"""restic-integrity CronJob 群 (P-0187 セッション 3) の manifest 不変条件を固定する。

verify #3 (`grep -rl 'read-data-subset' apps --include='*.yaml' | wc -l >= 4`) は
文字列の数しか見ないため、このテストが文字列の背後にある契約を検査する:

- 4 ns (coder は 2 repo 分) に integrity CronJob が存在し、schedule が分散方針
  (毎月固定日・JST 午後帯・5 本で実行日が重複しない) を守っていること
- 成功記録の書き込み先 ConfigMap が CronJob の env と Role の resourceNames と
  manifest 上の事前作成 ConfigMap の 3 箇所で一致すること (1 箇所だけ直して他を
  直し忘れる drift を落とす)
- download-ledger の LEDGER_RULES に integrity CronJob 名が登録されていること
  (未登録だと unknown_jobs として記録される。帳簿が黙って 0 扱いにしない設計)
- kustomization への配線忘れがないこと

YAML のパースは PyYAML を使う (CI の unittest と同じ環境で利用可。既存テストが
AST 抽出で避けていたのは「import 副作用を持つスクリプト」の話であり manifest の
構造検査には不要)。
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (ns, ファイル, [(CronJob 名, repo, records ConfigMap, schedule)])
EXPECTED = [
    ("vaultwarden", "apps/vaultwarden/restic-integrity-cronjob.yaml",
     [("vaultwarden-restic-integrity", "vaultwarden",
       "vaultwarden-integrity-records", "20 14 2 * *")]),
    ("immich", "apps/immich/restic-integrity-cronjob.yaml",
     [("immich-restic-integrity", "immich",
       "immich-integrity-records", "40 13 6 * *")]),
    ("syncthing", "apps/syncthing/restic-integrity-cronjob.yaml",
     [("syncthing-restic-integrity", "syncthing",
       "syncthing-integrity-records", "50 14 18 * *")]),
    ("coder", "apps/coder/restic-integrity-cronjob.yaml",
     [("coder-postgres-restic-integrity", "coder-postgres",
       "coder-postgres-integrity-records", "10 15 10 * *"),
      ("coder-workspace-homes-restic-integrity", "coder-workspace-homes",
       "coder-workspace-homes-integrity-records", "30 13 14 * *")]),
]

# LEDGER_RULES への登録値 (bytes)。「推定 = index/config 読み 32 MiB + 総量 ÷ 3」。
# 数値の根拠は各 download-ledger-cronjob.yaml のコメントと PROGRESS を参照
LEDGER_BYTES = {
    "vaultwarden-restic-integrity": 35651584,
    "immich-restic-integrity": 153092096,
    "coder-postgres-restic-integrity": 138412032,
    "coder-workspace-homes-restic-integrity": 1396703232,
    "syncthing-restic-integrity": 34603008,
}


def load_yaml(path):
    import yaml
    return list(yaml.safe_load_all((ROOT / path).read_text()))


def ledger_rules_of(path):
    """download-ledger-cronjob.yaml の LEDGER_RULES env 値を取り出す。"""
    text = (ROOT / path).read_text()
    m = re.search(r'name: LEDGER_RULES\s*\n\s*value: "([^"]*)"', text)
    if not m:
        raise AssertionError(f"{path}: LEDGER_RULES env が見つからない")
    rules = {}
    for item in m.group(1).split(","):
        name, _, raw = item.partition(":")
        rules[name] = int(raw)
    return rules


class IntegrityCronJobManifestTest(unittest.TestCase):
    def test_expected_files_exist_with_expected_cronjobs(self):
        for ns, path, cronjobs in EXPECTED:
            docs = load_yaml(path)
            by_kind = {}
            for d in docs:
                by_kind.setdefault(d["kind"], []).append(d)
            names = {c["metadata"]["name"] for c in by_kind.get("CronJob", [])}
            self.assertEqual(
                names, {c[0] for c in cronjobs},
                f"{path}: CronJob 名の一致")
            for c in by_kind["CronJob"]:
                self.assertEqual(c["spec"]["concurrencyPolicy"], "Forbid")
                pod_spec = c["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                self.assertEqual(pod_spec["serviceAccountName"], "restic-integrity")

    def test_schedules_are_monthly_on_distinct_days_in_afternoon_jst(self):
        days = []
        for ns, path, cronjobs in EXPECTED:
            docs = load_yaml(path)
            schedules = {
                d["metadata"]["name"]: d["spec"]["schedule"]
                for d in docs if d["kind"] == "CronJob"
            }
            for name, _, _, schedule in cronjobs:
                self.assertEqual(schedules[name], schedule, f"{name}")
                minute, hour, day, month, dow = schedule.split()
                # 月次 (毎月固定日)。日付は全月に存在する 28 日まで
                self.assertEqual((month, dow), ("*", "*"), name)
                self.assertTrue(1 <= int(day) <= 28, name)
                # JST 午後帯 (13-15 時台 = UTC 04-06 時台)。00:00 UTC リセット直後の
                # 予算を使い、retention 帯 (日曜 JST 03:45-04:50) と時刻帯が離れる
                self.assertIn(int(hour), (13, 14, 15), name)
                days.append(int(day))
        # apps 間で実行日が重複しない (同一 UTC 日に integrity が重なる構造を作らない)
        self.assertEqual(len(days), len(set(days)),
                         f"integrity の実行日が重複: {sorted(days)}")

    def test_records_configmap_wiring_is_consistent(self):
        # CronJob env ↔ Role resourceNames ↔ 事前作成 ConfigMap の 3 箇所一致
        for ns, path, cronjobs in EXPECTED:
            docs = load_yaml(path)
            cms = {d["metadata"]["name"] for d in docs if d["kind"] == "ConfigMap"
                   and d["metadata"]["name"].endswith("-integrity-records")}
            self.assertEqual(cms, {c[2] for c in cronjobs}, path)
            role = next(d for d in docs if d["kind"] == "Role")
            rnames = set()
            for rule in role["rules"]:
                rnames.update(rule.get("resourceNames") or [])
            self.assertEqual(rnames, cms, f"{path}: Role resourceNames")
            self.assertEqual(role["rules"][0]["verbs"], ["get", "update"])
            for d in docs:
                if d["kind"] != "CronJob":
                    continue
                spec = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                env = {e["name"]: e.get("value") for e in
                       spec["containers"][0]["env"]}
                cm_name = env["INTEGRITY_RECORDS_CONFIGMAP"]
                self.assertIn(cm_name, cms, d["metadata"]["name"])
                repo = env["INTEGRITY_REPO"]
                self.assertEqual(cm_name, f"{repo}-integrity-records")

    def test_cronjob_uses_restic_check_read_data_subset_via_staged_binary(self):
        # verify #3 の実体: N/T 形式の --read-data-subset をドライバ経由で走らせる
        for ns, path, _ in EXPECTED:
            docs = load_yaml(path)
            script_cm = next(d for d in docs if d["kind"] == "ConfigMap"
                             and d["metadata"]["name"] == "restic-integrity-script")
            driver = script_cm["data"]["run_integrity.py"]
            module = script_cm["data"]["restic_integrity.py"]
            self.assertIn("--read-data-subset=", driver, path)
            self.assertIn("coverage_from_records(", driver, path)
            self.assertIn("raise SystemExit(\n            \"restic check failed:", driver)
            # 埋め込みモジュールは正本と同一であることを CI の sync check が担保する
            canonical = (ROOT / "ops/tools/restic_integrity.py").read_text()
            self.assertEqual(module, canonical, path)
            spec = next(d for d in docs if d["kind"] == "CronJob")[
                "spec"]["jobTemplate"]["spec"]["template"]["spec"]
            init_names = [c["name"] for c in spec["initContainers"]]
            self.assertEqual(init_names, ["stage-restic"])
            init_cmd = "\n".join(spec["initContainers"][0]["command"])
            self.assertIn("set -eu", init_cmd)

    def test_ledger_rules_register_every_integrity_cronjob(self):
        registered = set()
        for ns, _, cronjobs in EXPECTED:
            rules = ledger_rules_of(f"apps/{ns}/download-ledger-cronjob.yaml")
            for name, _, _, _ in cronjobs:
                self.assertIn(name, rules,
                              f"{ns}: LEDGER_RULES に {name} が無い (unknown_jobs 化する)")
                self.assertEqual(rules[name], LEDGER_BYTES[name], name)
                registered.add(name)
        self.assertEqual(registered, set(LEDGER_BYTES))

    def test_kustomizations_wire_the_new_manifests(self):
        for ns, path, _ in EXPECTED:
            rel = Path(path).name
            kust = (ROOT / f"apps/{ns}/kustomization.yaml").read_text()
            self.assertIn(f"- {rel}", kust, f"apps/{ns}/kustomization.yaml")


# --- 埋め込みドライバ run_integrity.py の純関数 ---
# (import 副作用を持つスクリプトを AST で取り出して試す。
#  test_download_ledger_script.py 流儀)

import ast  # noqa: E402
import textwrap  # noqa: E402

sys.path.insert(0, str(ROOT / "ops"))
from check_download_ledger_script_sync import extract_block_scalar as _extract  # noqa: E402

DRIVER_FUNCTIONS = (
    "parse_packs_read", "build_record", "parse_existing_payload",
    "extract_records", "merge_records", "trim_records", "build_payload",
)
DRIVER_CONSTANTS = ("MAX_RECORDS",)


def load_driver():
    source = textwrap.dedent(
        _extract(EXPECTED[0][1], "run_integrity.py")).lstrip("\n")
    tree = ast.parse(source)
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in DRIVER_FUNCTIONS:
            body.append(node)
        elif isinstance(node, ast.Assign) and getattr(
                node.targets[0], "id", None) in DRIVER_CONSTANTS:
            body.append(node)
    missing = set(DRIVER_FUNCTIONS + DRIVER_CONSTANTS) - {
        n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in body}
    assert not missing, f"抽出に失敗: {sorted(missing)}"
    module = ast.Module(body=body, type_ignores=[])
    ns = {"re": re, "json": json}
    exec(compile(ast.fix_missing_locations(module), "<run_integrity>", "exec"), ns)
    return ns


driver = load_driver()


class DriverPureFunctionsTest(unittest.TestCase):
    def test_parse_packs_read_takes_last_match(self):
        out = "check all packs\n[0:12] 100%  37 pack files read\nno errors found"
        self.assertEqual(driver["parse_packs_read"](out), 37)
        self.assertEqual(
            driver["parse_packs_read"]("read group 1/3\n12 packs in group"), 12)

    def test_parse_packs_read_is_tolerant(self):
        self.assertIsNone(driver["parse_packs_read"]("no errors found"))
        self.assertIsNone(driver["parse_packs_read"](None))
        self.assertIsNone(driver["parse_packs_read"](42))

    def test_build_record_keeps_plan_contract_keys(self):
        record = driver["build_record"](
            {"date": "2026-08-23", "slot": 2, "subset": "2/3", "cycle": 3}, 37)
        self.assertEqual(record, {
            "date": "2026-08-23", "slot": 2, "subset": "2/3",
            "cycle": 3, "packs_read": 37})

    def test_parse_existing_payload_never_raises(self):
        self.assertEqual(driver["parse_existing_payload"]("{broken"), {})
        self.assertEqual(driver["parse_existing_payload"]("[1, 2]"), {})
        self.assertEqual(driver["parse_existing_payload"](""), {})
        self.assertEqual(driver["parse_existing_payload"](None), {})

    def test_extract_records_drops_non_dicts(self):
        payload = {"records": [1, "x", {"date": "2026-07-01", "slot": 3}]}
        self.assertEqual(driver["extract_records"](payload),
                         [{"date": "2026-07-01", "slot": 3}])
        self.assertEqual(driver["extract_records"]({}), [])
        self.assertEqual(driver["extract_records"]({"records": "no"}), [])

    def test_merge_records_replaces_same_date_and_sorts(self):
        existing = [{"date": "2026-06-01", "slot": 1},
                    {"date": "2026-08-01", "slot": 9}]
        incoming = {"date": "2026-08-01", "slot": 2, "subset": "2/3"}
        out = driver["merge_records"](existing, incoming)
        self.assertEqual([r["date"] for r in out],
                         ["2026-06-01", "2026-08-01"])
        self.assertEqual(out[-1]["slot"], 2)

    def test_merge_records_drops_unusable_existing(self):
        existing = ["garbage", {"slot": 1}, {"date": 7},
                    {"date": "2026-07-01", "slot": 3}]
        incoming = {"date": "2026-08-01", "slot": 1}
        out = driver["merge_records"](existing, incoming)
        self.assertEqual([r["date"] for r in out], ["2026-07-01", "2026-08-01"])

    def test_trim_records_keeps_recent_tail(self):
        records = [{"date": "2026-%02d-01" % m} for m in range(1, 13)]
        self.assertEqual(len(driver["trim_records"](records)), 12)
        trimmed = driver["trim_records"](records, 3)
        self.assertEqual(trimmed[0]["date"], "2026-10-01")


if __name__ == "__main__":
    unittest.main()
