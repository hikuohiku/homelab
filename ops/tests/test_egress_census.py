"""P-0203: egress_census の抽出ロジックと台帳構築の固定テスト。

静的台帳の根幹 (YAML/コードからの endpoint 抽出、attribution、fail-closed) を
合成 fixture で両方向固定する。実リポジトリへの依存は ops/rules.json の読み
(注入で回避可) だけで、**実クラスタへの通信は一切しない**。

リポジトリルートから `python3 -m unittest ops.tests.test_egress_census`。
"""

import json
import tempfile
import unittest
from pathlib import Path

from ops.security import egress_census as ec

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "egress_census"


def read_fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


class ParseDocMetaTest(unittest.TestCase):
    def test_multidoc_kind_name_namespace(self):
        docs = ec.split_docs(read_fixture("backup_cronjob.yaml"))
        self.assertEqual(len(docs), 2)
        first = ec.parse_doc_meta(docs[0][1])
        self.assertEqual(
            first, {"kind": "CronJob", "name": "app-restic-backup", "namespace": "app-ns"}
        )
        second = ec.parse_doc_meta(docs[1][1])
        self.assertEqual(second["name"], "app-restic-retention")

    def test_non_workload_doc(self):
        docs = ec.split_docs(read_fixture("app_values.yaml"))
        cm = ec.parse_doc_meta(docs[1][1])
        self.assertEqual(cm["kind"], "ConfigMap")
        self.assertEqual(cm["namespace"], "app-ns")

    def test_broken_yaml_does_not_crash(self):
        # 簡易パーサは「読めない」ことをエラーにしない (meta は補助情報)。
        meta = ec.parse_doc_meta(["metadata:", "  name: [unclosed", "\tbad indent"])
        self.assertIsInstance(meta, dict)


class ExtractFindingsTest(unittest.TestCase):
    def test_comment_urls_are_ignored(self):
        got = ec.extract_findings_from_lines(
            read_fixture("backup_cronjob.yaml"), ".yaml"
        )
        hosts = {h for _, kind, h in got}
        self.assertNotIn("github.com", " ".join(hosts))
        # コメント行以外に URL は無い (b2 だけ)
        self.assertEqual({k for _, k, _ in got}, {"b2"})

    def test_b2_variants_normalized(self):
        got = ec.extract_findings_from_lines(
            read_fixture("backup_cronjob.yaml"), ".yaml"
        )
        b2 = [(ln, k, h) for ln, k, h in got if k == "b2"]
        self.assertEqual(len(b2), 2)  # $(VAR) 形と literal bucket 形
        self.assertTrue(all(h == ec.B2_ENDPOINT for _, _, h in b2))
        # 行番号は実在する b2: 行を指す
        lines = read_fixture("backup_cronjob.yaml")
        for ln, _, _ in b2:
            self.assertIn("b2:", lines[ln - 1])

    def test_url_and_oci_and_port(self):
        got = ec.extract_findings_from_lines(
            read_fixture("kustomization_helm.yaml"), ".yaml"
        )
        found = {(k, h) for _, k, h in got}
        self.assertIn(("url", "charts.example.org"), found)
        self.assertIn(("oci", "ghcr.io"), found)

    def test_self_public_and_schema_and_loopback(self):
        got = ec.extract_findings_from_lines(read_fixture("app_values.yaml"), ".yaml")
        found = {(k, h) for _, k, h in got}
        self.assertIn(("url", "accounts.google.com"), found)
        self.assertIn(("url", "myapp.tailabc123.ts.net"), found)
        self.assertIn(("schema", "opencode.ai"), found)
        self.assertIn(("url", "127.0.0.1:4096"), found)

    def test_go_defaults_and_inline_comments(self):
        got = ec.extract_findings_from_lines(read_fixture("code_defaults.go"), ".go")
        found = {h for _, k, h in got if k == "url"}
        self.assertIn("api.example.org", found)
        self.assertIn("127.0.0.1:8080", found)
        self.assertNotIn("example.com", " ".join(found))  # // コメントは無視


class ClassifyHostTest(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(ec.classify_host("api.doppler.com")[0], "external")
        self.assertEqual(ec.classify_host("github.com")[0], "external")
        self.assertEqual(
            ec.classify_host("ops-dashboard.autopilot.svc")[0], "cluster_local"
        )
        self.assertEqual(
            ec.classify_host("kubernetes.default.svc")[0], "cluster_local"
        )
        self.assertEqual(ec.classify_host("127.0.0.1")[0], "cluster_local")
        self.assertEqual(ec.classify_host("dex.tailae6c2.ts.net")[0], "self_public_url")


class EndpointOfUrlTest(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(ec.endpoint_of_url("https://api.github.com/repos/x"), "api.github.com")
        self.assertEqual(ec.endpoint_of_url("http://h.test:8443/p"), "h.test:8443")
        with self.assertRaises(ec.CensusError):
            ec.endpoint_of_url("not a url")


class ScanPathsTest(unittest.TestCase):
    def test_yaml_findings_carry_doc_meta(self):
        findings = ec.scan_paths([FIXTURES / "backup_cronjob.yaml"])
        backup = next(f for f in findings if f.doc_name == "app-restic-backup")
        retention = next(f for f in findings if f.doc_name == "app-restic-retention")
        self.assertEqual(backup.doc_namespace, "app-ns")
        self.assertEqual(backup.kind, "b2")
        # doc 境界をまたいで meta が混ざらない (retention の行が backup にならない)
        self.assertLess(backup.line_no, retention.line_no)

    def test_unreadable_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "broken.yaml"
            bad.write_bytes(b"\xff\xfe\x00")  # UTF-8 として不正
            with self.assertRaises(ec.CensusError):
                ec.scan_paths([bad], base=Path(td))


class BuildRecordsTest(unittest.TestCase):
    def _rule_for(self, glob: str, host: str) -> dict:
        return {
            "glob": glob,
            "host": host,
            "workload": "",
            "namespace": "",
            "reason": "test reason",
            "breakage": "test breakage",
            "open": True,
            "open_note": "",
            "use_doc_workload": True,
            "exception_note": "",
            "doc_workload_override": {"workspace-home-backup-script": ("override-job", "coder")},
        }

    def test_doc_workload_attribution(self):
        findings = ec.scan_paths([FIXTURES / "backup_cronjob.yaml"])
        rows, _ = ec.build_records(
            findings, rules=[self._rule_for("*", ec.B2_ENDPOINT)], include_provider_rows=False
        )
        names = {r.workload for r in rows}
        self.assertEqual(names, {"app-restic-backup", "app-restic-retention"})
        for r in rows:
            self.assertTrue(r.source_evidence[0].startswith("ops/tests/fixtures/"))
            self.assertIn("(doc: CronJob", r.source_evidence[1])

    def test_doc_workload_override(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            appdir = tmp / "apps" / "x"
            appdir.mkdir(parents=True)
            (appdir / "cm.yaml").write_text(
                "kind: ConfigMap\n"
                "metadata:\n"
                "  name: workspace-home-backup-script\n"
                "  namespace: coder\n"
                'data:\n  script: |\n    "value": "b2:$(RESTIC_B2_BUCKET):homes"\n',
                encoding="utf-8",
            )
            findings = ec.scan_paths([appdir], base=tmp)
            rule = self._rule_for("apps/*/*", ec.B2_ENDPOINT)
            rows, _ = ec.build_records(findings, rules=[rule], include_provider_rows=False)
            self.assertEqual([r.workload for r in rows], ["override-job"])

    def test_unattributed_external_fails_closed(self):
        findings = [
            ec.Finding(path="somewhere.yaml", line_no=1, kind="url", host="unknown.example.net"),
        ]
        with self.assertRaises(ec.CensusError) as ctx:
            ec.build_records(
                findings, rules=[], include_provider_rows=False
            )
        self.assertIn("unknown.example.net", str(ctx.exception))

    def test_cluster_local_and_schema_excluded_not_attributed(self):
        findings = [
            ec.Finding(path="a.yaml", line_no=1, kind="url", host="db.svc"),
            ec.Finding(path="b.yaml", line_no=2, kind="schema", host="opencode.ai"),
        ]
        rows, excluded = ec.build_records(findings, rules=[], include_provider_rows=False)
        categories = {e["category"] for e in excluded}
        self.assertIn("cluster_local", categories)
        self.assertIn("schema_reference", categories)
        self.assertEqual(rows, [])

    def test_empty_ledger_fails_dod_validation(self):
        # provider 行だけでも下限 (8) に届かない = 台帳としては落とす
        rows = ec.build_provider_rows(allowlisted_keys=set())
        with self.assertRaises(ec.CensusError):
            ec.validate_census(rows)

    def test_duplicate_rows_fail(self):
        dup_rule = {
            "glob": "*",
            "host": "dup.test",
            "workload": "w",
            "namespace": "ns",
            "reason": "r",
            "breakage": "b",
            "open": True,
            "open_note": "",
            "use_doc_workload": False,
            "exception_note": "",
            "doc_workload_override": {},
        }
        # 同一 (ns, workload, endpoint) は merge されるので重複 row にはならない。
        # 代わりに evidence が集約されることを確かめる
        findings = [
            ec.Finding(path="f1.yaml", line_no=1, kind="url", host="dup.test"),
            ec.Finding(path="f2.yaml", line_no=5, kind="url", host="dup.test"),
        ]
        rows, _ = ec.build_records(
            findings, rules=[dup_rule], include_provider_rows=False
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0].source_evidence), 2)


class ProviderRowsTest(unittest.TestCase):
    def test_allowlist_drives_rows(self):
        keys = {"DISCORD_WEBHOOK_URL", "TELEGRAM_ALLOWED_USER_ID"}
        rows = ec.build_provider_rows(allowlisted_keys=keys)
        endpoints = {r.endpoint for r in rows}
        self.assertIn("discord.com:443", endpoints)
        # 対応表に無い鍵は行を作らない
        self.assertNotIn("api.telegram.org:443", endpoints)

    def test_real_rules_json_readable(self):
        keys = ec._allowlisted_keys()
        self.assertIn("DISCORD_WEBHOOK_URL", keys)


class ValidateAndRenderTest(unittest.TestCase):
    def _rows_ok(self):
        return [
            ec.Row(
                workload="w",
                namespace="ns",
                endpoint=h,
                reason="r",
                breakage="b",
            )
            for h in sorted(ec.MANDATORY_HOSTS)
        ]

    def test_missing_mandatory_host_fails(self):
        rows = self._rows_ok()[:-1]  # 1 つ欠く
        with self.assertRaises(ec.CensusError) as ctx:
            ec.validate_census(rows)
        self.assertIn("必須 host", str(ctx.exception))

    def test_short_ledger_fails(self):
        rows = [
            ec.Row(workload="w", namespace="ns", endpoint="x.test", reason="r", breakage="b")
            for _ in range(3)
        ]
        with self.assertRaises(ec.CensusError):
            ec.validate_census(rows)

    def test_record_missing_reason_fails(self):
        rows = [
            ec.Row(workload="w", namespace="ns", endpoint=h, reason="", breakage="b")
            for h in sorted(ec.MANDATORY_HOSTS)
        ]
        with self.assertRaises(ec.CensusError):
            ec.validate_census(rows)

    def test_render_is_deterministic(self):
        findings = ec.scan_paths(
            [ec.APPS_DIR, ec.NIX_DIR, ec.RULES_PATH]
        )
        rows, excluded = ec.build_records(findings)
        j1, m1 = ec.render_json(rows, excluded), ec.render_md(rows, excluded)
        j2, m2 = ec.render_json(rows, excluded), ec.render_md(rows, excluded)
        self.assertEqual(j1, j2)
        self.assertEqual(m1, m2)
        doc = json.loads(j1)
        self.assertGreaterEqual(len(doc["endpoints"]), ec.MIN_ENDPOINTS)
        keys = [(e["namespace"], e["workload"], e["endpoint"]) for e in doc["endpoints"]]
        expected = sorted(keys, key=lambda k: (ec._ns_sort_key(k[0]), k[1], k[2]))
        self.assertEqual(keys, expected)


class RealLedgerInvariantsTest(unittest.TestCase):
    """コミット済み台帳が現リポジトリの再生成結果と一致すること (--check 相当)。"""

    def test_committed_outputs_are_current(self):
        json_text, md_text = ec.build_all()
        self.assertEqual(ec.OUT_JSON.read_text(encoding="utf-8"), json_text)
        self.assertEqual(ec.OUT_MD.read_text(encoding="utf-8"), md_text)

    def test_md_carries_human_required_sections(self):
        md = ec.OUT_MD.read_text(encoding="utf-8")
        self.assertIn("Doppler", md)
        self.assertIn("Backblaze", md)
        self.assertIn("既定拒否", md)
        self.assertIn("この穴が塞がれると壊れるもの", md)


if __name__ == "__main__":
    unittest.main()
