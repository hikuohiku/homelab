"""ops/tools/reachability_probe.py の判定ロジックを固定する (P-9034)。

`python3 -m pytest ops/tests/test_reachability_probe.py -q` と
`python3 -m unittest ops.tests.test_reachability_probe` の両方で通る
(unittest.TestCase ベース。pytest は unittest を素通りで collect する)。

ネットワーク層 (probe) は make_fixture_probe に差し替え、**一切ネットワークに出ない**。
adguard の「DNS 死」は ops/tests/fixtures/reachability/adguard-dns-dead.json が再現する
(計器がこの失敗モードを clusterIP / tailnet の双方で捕まえられることを固定する)。
"""

import json
import struct
import tempfile
import unittest
from pathlib import Path

from ops.tools import reachability_probe as rp

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "reachability"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ADGUARD_DNS_DEAD_TIDS():
    """adguard「DNS 死」fixture が fail と宣言する target id。"""
    return set(load_fixture("adguard-dns-dead.json")["fail"].keys())


class TestBuildDnsQuery(unittest.TestCase):
    def test_structure(self):
        q = rp.build_dns_query()
        # header(12) + QNAME(example=1+7, com=1+3) + 終端(1) + type/class(4) = 29
        self.assertEqual(len(q), 12 + 1 + 7 + 1 + 3 + 1 + 4)
        self.assertEqual(q[:2], struct.pack(">H", rp.DNS_ID))

    def test_question_section(self):
        q = rp.build_dns_query(name="foo.bar", qid=1)
        self.assertIn(b"\x03foo\x03bar\x00", q)
        # 末尾の type=1(A) / class=1(IN)
        self.assertEqual(q[-4:], b"\x00\x01\x00\x01")


class TestParseDnsResponse(unittest.TestCase):
    def test_valid_response_ok(self):
        resp = _response(qid=rp.DNS_ID, flags=0x8180, an=1)
        ok, detail = rp.parse_dns_response(resp)
        self.assertTrue(ok)
        self.assertIn("rcode=0", detail)

    def test_nxdomain_is_alive(self):
        # NXDOMAIN (rcode=3) でも「サーバは生きている」= 到達
        resp = _response(qid=rp.DNS_ID, flags=0x8183, an=0)
        ok, _ = rp.parse_dns_response(resp)
        self.assertTrue(ok)

    def test_id_mismatch_fails(self):
        resp = _response(qid=0x0000, flags=0x8180, an=0)
        ok, detail = rp.parse_dns_response(resp)
        self.assertFalse(ok)
        self.assertIn("ID 不一致", detail)

    def test_no_qr_bit_fails(self):
        # クエリ (QR ビット無し) を応答として誤判定しない
        resp = _response(qid=rp.DNS_ID, flags=0x0100, an=0)
        ok, detail = rp.parse_dns_response(resp)
        self.assertFalse(ok)
        self.assertIn("QR", detail)

    def test_short_data_fails(self):
        ok, _ = rp.parse_dns_response(b"\x00")
        self.assertFalse(ok)


class TestTargetList(unittest.TestCase):
    def test_covers_spec_apps(self):
        targets = rp.build_targets(rp.DEFAULT_TAILNET_DOMAIN)
        apps = {t["app"] for t in targets}
        for app in ("ops-dashboard", "coder", "coder-postgres", "nats", "vaultwarden",
                    "syncthing", "immich-postgres", "autopilot-heart", "adguard",
                    "syncthing-sync"):
            self.assertIn(app, apps)

    def test_dual_path_for_tailnet_published(self):
        targets = rp.build_targets(rp.DEFAULT_TAILNET_DOMAIN)
        # adguard は clusterIP でも tailnet でも入口を持つ (LoadBalancer に clusterIP が
        # 割り当たるため)。syncthing-sync は sync プロトコルの tailnet 公開のみ (clusterIP
        # は人間の入口でない)
        adguard_routes = {t["route"] for t in targets if t["app"] == "adguard"}
        self.assertEqual(adguard_routes, {"clusterip", "tailnet"})
        syncthing_sync_routes = {t["route"] for t in targets if t["app"] == "syncthing-sync"}
        self.assertEqual(syncthing_sync_routes, {"tailnet"})

    def test_target_ids_unique(self):
        targets = rp.build_targets(rp.DEFAULT_TAILNET_DOMAIN)
        ids = [rp.target_id(t) for t in targets]
        self.assertEqual(len(ids), len(set(ids)), "target id が重複している")
        self.assertEqual(len(ids), 17)

    def test_fixture_ids_all_in_targets(self):
        # fixture の fail id が実対象一覧に全部存在する (fixture の腐り止め)
        targets = rp.build_targets(rp.DEFAULT_TAILNET_DOMAIN)
        ids = {rp.target_id(t) for t in targets}
        missing = ADGUARD_DNS_DEAD_TIDS() - ids
        self.assertEqual(missing, set())


class TestResolveFailureMarksUnknown(unittest.TestCase):
    def test_unresolvable_host_is_unknown_not_probe_called(self):
        target = {
            "app": "adguard", "route": "tailnet",
            "host": "adguard.does-not-exist.ts.net", "port": 53,
            "kind": "dns-udp",
        }

        def probe(tid, kind, host, port, http_path, timeout):
            raise AssertionError("名前解決に失敗した対象に probe を呼んではいけない")

        def dead_resolver(host, timeout):
            return False, "名前解決失敗: NXDOMAIN (test)"

        result = rp.probe_target(target, probe, 1.0, resolver=dead_resolver)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["ok"])
        self.assertFalse(result["resolve"])
        self.assertIn("名前解決失敗", result["detail"])

    def test_resolve_failure_is_not_counted_as_confirmed_fail(self):
        """unknown (解決不能) は summary の fail に入れない — 死の証明ではないため。"""
        target = {
            "app": "syncthing-sync", "route": "tailnet",
            "host": "x.does-not-exist.ts.net", "port": 22000, "kind": "tcp",
        }

        def dead_resolver(host, timeout):
            return False, "NXDOMAIN"

        result = rp.probe_target(target, lambda *a: (True, "ok"), 1.0,
                                 resolver=dead_resolver)
        summary = rp.summarize([result])
        self.assertEqual(summary["fail"], 0)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["apps_fail"], [])
        self.assertEqual(summary["apps_unknown"], ["syncthing-sync"])


class TestProbeWithAdguardDnsDeadFixture(unittest.TestCase):
    """spec dod 2: adguard の「DNS 死」が計器のどの検査で捕まるかを fixture で固定する。

    resolver は ok_resolver (常に解決) を注入し、「名前解決は通るが DNS 応答が死んでいる」
    という fixture の意図どおりに probe 層だけで判定する。
    """

    def setUp(self):
        self.fixture = load_fixture("adguard-dns-dead.json")
        self.targets = rp.build_targets(rp.DEFAULT_TAILNET_DOMAIN)
        self.probe = rp.make_fixture_probe(self.fixture)
        self.resolver = rp.ok_resolver

    def test_adguard_dns_entries_are_caught(self):
        results = rp.run_probe(self.targets, self.probe, rp.DEFAULT_TIMEOUT_S,
                               resolver=self.resolver)
        failed = {rp.target_id(r) for r in results if r["state"] == "fail"}
        self.assertEqual(failed, ADGUARD_DNS_DEAD_TIDS())

    def test_failed_entries_are_exactly_dns_paths(self):
        results = rp.run_probe(self.targets, self.probe, rp.DEFAULT_TIMEOUT_S,
                               resolver=self.resolver)
        for r in results:
            if r["app"] == "adguard" and r["kind"].startswith("dns"):
                self.assertEqual(r["state"], "fail", rp.target_id(r))
                self.assertFalse(r["ok"], rp.target_id(r))
            else:
                self.assertEqual(r["state"], "ok", rp.target_id(r))
                self.assertTrue(r["ok"], rp.target_id(r))

    def test_summary_counts(self):
        results = rp.run_probe(self.targets, self.probe, rp.DEFAULT_TIMEOUT_S,
                               resolver=self.resolver)
        summary = rp.summarize(results)
        self.assertEqual(summary["total"], 17)
        self.assertEqual(summary["fail"], len(ADGUARD_DNS_DEAD_TIDS()))
        self.assertEqual(summary["unknown"], 0)
        self.assertEqual(summary["apps_fail"], ["adguard"])

    def test_all_ok_scenario_has_no_false_positive(self):
        all_ok = {"fail": {}, "default": {"detail": "ok"}}
        results = rp.run_probe(self.targets, rp.make_fixture_probe(all_ok),
                               rp.DEFAULT_TIMEOUT_S, resolver=self.resolver)
        self.assertTrue(all(r["state"] == "ok" for r in results))
        self.assertEqual(rp.summarize(results)["fail"], 0)


class TestRealProbe(unittest.TestCase):
    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            rp.real_probe("tid", "bogus", "host", 1, "/", 1.0)

    def test_dns_codec_roundtrip(self):
        # 手作りの応答 (ID + QR) が実 probe の parse で ok になること
        query = rp.build_dns_query()
        resp = _response(qid=rp.DNS_ID, flags=0x8180, an=0)
        ok, detail = rp.parse_dns_response(resp)
        self.assertTrue(ok)
        self.assertIsInstance(query, bytes)


class TestWriteReport(unittest.TestCase):
    def test_report_json_structure(self):
        results = rp.run_probe(
            rp.build_targets(rp.DEFAULT_TAILNET_DOMAIN),
            rp.make_fixture_probe(load_fixture("adguard-dns-dead.json")),
            rp.DEFAULT_TIMEOUT_S,
            resolver=rp.ok_resolver,
        )
        report = rp.build_report(results, rp.DEFAULT_TAILNET_DOMAIN, context="test-pod")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "reachability.json"
            out.write_text(json.dumps(report), encoding="utf-8")
            loaded = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["schema"], 1)
        self.assertEqual(loaded["tool"], "reachability_probe")
        self.assertEqual(loaded["project"], "P-9034")
        self.assertIn("generated_at", loaded)
        self.assertEqual(loaded["context"], "test-pod")
        self.assertEqual(loaded["summary"]["total"], 17)
        self.assertEqual(len(loaded["targets"]), 17)


# --- ヘルパ ---


def _response(qid, flags, an):
    return struct.pack(">HHHHHH", qid, flags, 1, an, 0, 0)


if __name__ == "__main__":
    unittest.main()