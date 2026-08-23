"""ops/tools/check_cert_expiry.py の契約を固定する (P-0188)。

リポジトリルートから `python3 -m unittest ops.tests.test_cert_expiry`
(CI の `unittest discover -s ops/tests -t .` も同じ物を掴む)。

受入の 3 系列 (正常・期限切れ・パース不能) は、test_pve_tls_docs の実物証明書
(openssl 生成・テスト専用の使い捨て) を土台に、notAfter だけを同長の日付へ
差し替えた DER で固定する。長さ不変なので構造は実物のまま — 自前 ASN.1 パーサを
自作エンコーダで試すという循環を避けるため、パーサの正しさの錨はこの実物側に
置く。ネットワークなしで通る。

あわせて手動同期コピー (apps/ops-health-reporter/check_cert_expiry.py) との
同一性を sha256 で機械検査する。version_watch.py の「反映を忘れる」を、忘れられ
ない形にしたもの。
"""

import base64
import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from ops.tools import check_cert_expiry as cce
from ops.tests.test_pve_tls_docs import CERT_PEM  # 実物 (openssl 生成・使い捨て)

REPO = Path(__file__).resolve().parents[2]
CANONICAL = REPO / "ops" / "tools" / "check_cert_expiry.py"
COPY = REPO / "apps" / "ops-health-reporter" / "check_cert_expiry.py"

NOW = datetime.datetime(2026, 8, 23, 12, 0, 0, tzinfo=datetime.timezone.utc)


def crt_b64(pem_text):
    return base64.b64encode(pem_text.encode()).decode()


def patch_not_after(pem_text, new_utc13):
    """実物 DER の notAfter (2 番目の UTCTime) だけを同じ長さの日付へ差し替える。

    長さ不変なので DER 全体の構造は openssl 生成物のまま保たれる。
    """
    raw = bytearray(cce.pem_chain_to_ders(pem_text.encode())[0])
    needle = b"\x17\x0d"
    spots = []
    i = raw.find(needle)
    while i != -1:
        spots.append(i)
        i = raw.find(needle, i + 1)
    assert len(spots) == 2, f"UTCTime が {len(spots)} 個 (想定 2): notBefore/notAfter"
    pos = spots[1] + 2
    assert len(new_utc13) == 13 and new_utc13.endswith("Z")
    raw[pos : pos + 13] = new_utc13.encode()
    b64 = base64.b64encode(bytes(raw)).decode()
    lines = [b64[j : j + 64] for j in range(0, len(b64), 64)]
    return (
        "-----BEGIN CERTIFICATE-----\n"
        + "\n".join(lines)
        + "\n-----END CERTIFICATE-----\n"
    )


HEALTHY_PEM = CERT_PEM if CERT_PEM.endswith("\n") else CERT_PEM + "\n"
WARN_PEM = patch_not_after(CERT_PEM, "260915120000Z")  # 2026-09-15T12:00:00Z (残り 23 日)
EXPIRED_PEM = patch_not_after(CERT_PEM, "260801000000Z")  # 2026-08-01T00:00:00Z (失効済み)


def secret_item(ns, name, crt_b64_value=None):
    item = {"metadata": {"name": name, "namespace": ns}, "type": cce.K8S_TLS_TYPE}
    if crt_b64_value is not None:
        item["data"] = {"tls.crt": crt_b64_value}
    return item


# ---------------------------------------------------------------------------
# 受入 3 系列: 正常・期限切れ・パース不能 (+ パーサ単体)
# ---------------------------------------------------------------------------


class DerParserTest(unittest.TestCase):
    """自前最小パーサの錨は openssl 生成の実物証明書。"""

    def test_real_cert_not_after_and_san(self):
        parsed = cce.parse_tls_crt_b64(crt_b64(HEALTHY_PEM))
        self.assertEqual(cce.iso_z(parsed["not_after"]), "2036-08-19T18:21:38Z")
        self.assertEqual(
            parsed["san"],
            ["DNS:localhost", "DNS:pve-tls-test.invalid", "IP:127.0.0.1"],
        )
        self.assertEqual(parsed["certs_in_chain"], 1)

    def test_generalized_time_and_utc_time_boundary(self):
        # X.509 の世紀規則: UTCTime は >=50 が 19xx、<50 が 20xx (strptime %y とは違う)
        utc49 = cce._parse_asn1_time(0x17, b"491231235959Z")
        self.assertEqual(utc49.year, 2049)
        utc50 = cce._parse_asn1_time(0x17, b"500101000000Z")
        self.assertEqual(utc50.year, 1950)
        gen = cce._parse_asn1_time(0x18, b"20500101000000Z")
        self.assertEqual((gen.year, gen.tzinfo), (2050, datetime.timezone.utc))
        frac = cce._parse_asn1_time(0x18, b"20500101000000.123Z")
        self.assertEqual(frac, gen)

    def test_garbage_times_fail_closed(self):
        for tag, raw in (
            (0x17, b"zzzzzzzzzzz"),
            (0x18, b"20501301000000Z"),  # 13 月は存在しない
            (0x05, b"260101000000Z"),  # NULL は時刻タグでない
        ):
            with self.subTest(tag=tag, raw=raw):
                with self.assertRaises(ValueError):
                    cce._parse_asn1_time(tag, raw)

    def test_truncated_and_garbage_der_raise(self):
        der = cce.pem_chain_to_ders(HEALTHY_PEM.encode())[0]
        for label, blob in (("切り詰め", der[:40]), ("空", b""), ("ゴミ", b"\xff\xff\xff\xff")):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    cce.parse_certificate_der(blob)

    def test_pem_without_certificate_block_raises(self):
        pem = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"
        with self.assertRaises(ValueError):
            cce.pem_chain_to_ders(pem.encode())


class K8sEntryThreeSeriesTest(unittest.TestCase):
    """受入 3 系列: 正常 / 期限切れ / パース不能。"""

    def collect(self, item):
        return cce.build_k8s_entry(item, NOW)

    def test_normal_series_is_ok_with_derived_values_only(self):
        entry = self.collect(secret_item("argocd", "argocd-tls", crt_b64(HEALTHY_PEM)))
        self.assertEqual(entry["status"], "ok")
        self.assertGreaterEqual(entry["days_left"], cce.WARN_DAYS)
        self.assertIn("DNS:pve-tls-test.invalid", entry["san"])
        # 台帳エントリに載るキーは派生値だけ。Secret の値 (tls.key 等) が
        # 混入する余地を構造的に塞ぐ
        self.assertEqual(
            sorted(entry.keys()),
            [
                "certs_in_chain",
                "days_left",
                "kind",
                "name",
                "namespace",
                "not_after",
                "san",
                "status",
            ],
        )

    def test_expired_series_is_critical(self):
        entry = self.collect(secret_item("immich", "immich-tls", crt_b64(EXPIRED_PEM)))
        self.assertEqual(entry["status"], "critical")
        self.assertLess(entry["days_left"], 0)
        self.assertEqual(entry["not_after"], "2026-08-01T00:00:00Z")

    def test_unparseable_series_is_parse_error_not_ignored(self):
        entry = self.collect(
            secret_item(
                "vaultwarden", "vw-admin-tls",
                base64.b64encode(b"this is not a certificate").decode(),
            )
        )
        self.assertEqual(entry["status"], "parse_error")
        self.assertIn("CERTIFICATE ブロック", entry["error"])

    def test_missing_tls_crt_is_parse_error(self):
        entry = self.collect(secret_item("ns", "no-crt"))
        self.assertEqual(entry["status"], "parse_error")
        self.assertIn("tls.crt", entry["error"])

    def test_broken_base64_is_parse_error(self):
        entry = self.collect(secret_item("ns", "bad-b64", "!!!not-base64!!!"))
        self.assertEqual(entry["status"], "parse_error")

    def test_warn_series_boundary(self):
        entry = self.collect(secret_item("coder", "coder-access", crt_b64(WARN_PEM)))
        self.assertEqual(entry["status"], "warn")
        self.assertEqual(entry["days_left"], 23)


class ThresholdTest(unittest.TestCase):
    def test_boundaries(self):
        cases = [
            (30, "ok"),
            (29, "warn"),
            (7, "warn"),
            (6, "critical"),
            (0, "critical"),
            (-5, "critical"),
        ]
        for days, want in cases:
            with self.subTest(days=days):
                self.assertEqual(cce.entry_status(days), want)

    def test_days_until_floor_semantics(self):
        # 30 日ちょうど前 (29日23時間) は 29 → warn。「30 日ある」誤判定を防ぐ
        soon = NOW + datetime.timedelta(days=29, hours=23)
        self.assertEqual(cce.days_until(soon, NOW), 29)


class SanMatchTest(unittest.TestCase):
    def test_prefix_and_case_insensitive(self):
        self.assertTrue(cce.san_contains(["DNS:PvE.example.TEST"], "pve.example.test"))
        self.assertTrue(cce.san_contains(["pve.example.test"], "pve.example.test"))
        self.assertFalse(cce.san_contains(["IP:127.0.0.1"], "127.0.0.1"))
        self.assertFalse(cce.san_contains(["DNS:other.example.test"], "pve.example.test"))

    def test_t0107_reality_is_unresolved(self):
        # T-0107 の実測: 現行 pveproxy 証明書の SAN は localhost/127.0.0.1 系で、
        # 接続先 hikuo-homeserver.tailae6c2.ts.net と不一致
        san = ["DNS:localhost", "DNS:pve-tls-test.invalid", "IP:127.0.0.1"]
        self.assertIs(cce.san_contains(san, cce.DEFAULT_EXPECTED_PVE_NAME), False)

    def test_empty_expected_name_is_none(self):
        self.assertIsNone(cce.san_contains(["DNS:x"], ""))


# ---------------------------------------------------------------------------
# Proxmox 側と T-0107 フィールド
# ---------------------------------------------------------------------------


def pve_info(notafter_epoch, san=None, subject="/CN=node01"):
    return {
        "subject": subject,
        "issuer": "/CN=Proxmox Virtual Environment Cluster CA",
        "notbefore": 1753689600,
        "notafter": notafter_epoch,
        "public-key-bits": 2048,
        "public-key-type": "rsa",
        "san": san or [],
    }


def epoch(y, m, d):
    return int(datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc).timestamp())


class ProxmoxEntriesTest(unittest.TestCase):
    def test_normal_entry_shape(self):
        resp = {
            "data": {
                "info": [
                    pve_info(epoch(2030, 1, 1), san=["DNS:localhost", "IP:192.168.1.2"])
                ]
            }
        }
        (entry,) = cce.build_proxmox_entries(
            "node01", resp, NOW, cce.DEFAULT_EXPECTED_PVE_NAME
        )
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["not_after"], "2030-01-01T00:00:00Z")
        self.assertIs(entry["san_match"], False)  # T-0107 未解消の形
        self.assertEqual(entry["expected_name"], cce.DEFAULT_EXPECTED_PVE_NAME)

    def test_wrapped_and_bare_responses_both_accepted(self):
        bare = {"info": [pve_info(epoch(2030, 1, 1))]}
        wrapped = {"data": bare}
        for label, resp in (("bare", bare), ("wrapped", wrapped)):
            with self.subTest(label=label):
                entries = cce.build_proxmox_entries("n", resp, NOW, "x.example")
                self.assertEqual(len(entries), 1)
                self.assertNotIn("error", entries[0])

    def test_missing_or_empty_info_is_parse_error(self):
        for bad in ({}, {"data": {}}, {"data": {"info": []}}, None):
            with self.subTest(bad=bad):
                (entry,) = cce.build_proxmox_entries("node01", bad, NOW, "x.example")
                self.assertEqual(entry["status"], "parse_error")
                self.assertIn("info 配列", entry["error"])

    def test_malformed_notafter_is_parse_error_per_entry(self):
        resp = {"info": [pve_info("soon-ish"), pve_info(epoch(2030, 1, 1))]}
        entries = cce.build_proxmox_entries("node01", resp, NOW, "x.example")
        self.assertEqual(entries[0]["status"], "parse_error")
        self.assertEqual(entries[1]["status"], "ok")  # 1 枚壊れても残りは生きる

    def test_resolved_when_san_has_expected_name(self):
        resp = {
            "info": [
                pve_info(
                    epoch(2030, 1, 1),
                    san=["DNS:hikuo-homeserver.tailae6c2.ts.net"],
                )
            ]
        }
        (entry,) = cce.build_proxmox_entries(
            "node01", resp, NOW, cce.DEFAULT_EXPECTED_PVE_NAME
        )
        self.assertIs(entry["san_match"], True)


class BuildT0107Test(unittest.TestCase):
    def test_unjudgeable_is_none_neither_true_nor_false(self):
        t = cce.build_t0107([], "x")
        self.assertIsNone(t["resolved"])
        broken = [{"kind": "proxmox_pveproxy", "status": "parse_error"}]
        self.assertIsNone(cce.build_t0107(broken, "x")["resolved"])

    def test_any_matching_entry_resolves(self):
        entries = [
            {"kind": "proxmox_pveproxy", "san_match": False},
            {"kind": "proxmox_pveproxy", "san_match": True},
        ]
        self.assertIs(cce.build_t0107(entries, "x")["resolved"], True)
        self.assertIn("docs/pveproxy-tls.md", cce.build_t0107(entries, "x")["note"])


# ---------------------------------------------------------------------------
# summarize / build_report
# ---------------------------------------------------------------------------


def entry(status, ns="ns", name="nm", kind="k8s_tls_secret"):
    e = {"kind": kind, "namespace": ns, "name": name, "status": status}
    if status not in ("parse_error",):
        e["days_left"] = {"ok": 90, "warn": 20, "critical": 3}[status]
    if kind == "proxmox_pveproxy":
        e["node"] = name
    return e


class SummarizeTest(unittest.TestCase):
    def test_worst_status_wins_with_deterministic_reason_order(self):
        s = cce.summarize(
            [
                entry("ok", ns="aaa"),
                entry("warn", ns="mmm"),
                entry("critical", ns="zzz"),
                entry("parse_error", ns="bbb"),
            ]
        )
        self.assertEqual(s["status"], "critical")
        self.assertEqual(s["counts"]["critical"], 1)
        # 順序は critical → parse_error → warn で固定 (diff を意味のある変化だけに)
        self.assertEqual(
            s["reason"],
            "7日未満で失効: zzz/nm; "
            "読めないため判定不能: bbb/nm; "
            "30日未満で失効: mmm/nm",
        )

    def test_parse_error_floors_at_warn(self):
        s = cce.summarize([entry("ok"), entry("parse_error")])
        self.assertEqual(s["status"], "warn")

    def test_all_ok(self):
        s = cce.summarize([entry("ok"), entry("ok", ns="b")])
        self.assertEqual(s["status"], "ok")
        self.assertIn("2 件すべて", s["reason"])

    def test_no_data_when_only_unconfigured(self):
        s = cce.summarize([{"kind": "proxmox_pveproxy", "status": "unconfigured"}])
        self.assertEqual(s["status"], "no_data")
        self.assertEqual(s["counts"]["unconfigured"], 1)

    def test_proxmox_labels_use_node_name(self):
        s = cce.summarize(
            [
                {
                    "kind": "proxmox_pveproxy",
                    "node": "node01",
                    "status": "critical",
                    "days_left": -1,
                }
            ]
        )
        self.assertIn("proxmox/node01", s["reason"])


class BuildReportTest(unittest.TestCase):
    def test_report_shape(self):
        report = cce.build_report([entry("ok")], expected_name="pve.example")
        self.assertEqual(sorted(report.keys()), ["entries", "summary", "t0107"])
        self.assertEqual(report["summary"]["status"], "ok")
        self.assertEqual(report["t0107"]["expected_name"], "pve.example")


# ---------------------------------------------------------------------------
# 収集層 (I/O 注入)
# ---------------------------------------------------------------------------


class CollectK8sSecretsTest(unittest.TestCase):
    def test_filters_by_type_on_clusterwide_path(self):
        calls = []

        def fake_k8s_get(path):
            calls.append(path)
            return {
                "items": [
                    secret_item("argocd", "argocd-tls", "x"),
                    {  # dockerconfigjson 型は対象外
                        "metadata": {"name": "regcred", "namespace": "coder"},
                        "type": "kubernetes.io/dockerconfigjson",
                        "data": {},
                    },
                ]
            }

        items = cce.collect_k8s_tls_secrets(fake_k8s_get)
        self.assertEqual(calls, ["/api/v1/secrets"])
        self.assertEqual([i["metadata"]["name"] for i in items], ["argocd-tls"])

    def test_collect_report_injects_k8s_get_and_skips_unconfigured_proxmox(self):
        def fake_k8s_get(path):
            return {"items": [secret_item("argocd", "argocd-tls", crt_b64(HEALTHY_PEM))]}

        report = cce.collect_report(k8s_get=fake_k8s_get, env={}, now=NOW)
        kinds = sorted({e["kind"] for e in report["entries"]})
        self.assertEqual(kinds, ["k8s_tls_secret", "proxmox_pveproxy"])
        px = next(e for e in report["entries"] if e["kind"] == "proxmox_pveproxy")
        self.assertEqual(px["status"], "unconfigured")
        # unconfigured だけでは警報しない (budget の流儀)。k8s 側が ok なら summary も ok
        self.assertEqual(report["summary"]["status"], "ok")
        self.assertIsNone(report["t0107"]["resolved"])

    def test_collect_report_k8s_failure_does_not_kill_proxmox_side(self):
        def boom(path):
            raise RuntimeError("接続できません")

        env = {
            "PROXMOX_TOKEN_ID": "agent@pam!ro",
            "PROXMOX_TOKEN_SECRET": "x",
        }
        with mock.patch.object(
            cce,
            "fetch_proxmox_cert_info",
            return_value={"info": [pve_info(epoch(2030, 1, 1))]},
        ):
            report = cce.collect_report(k8s_get=boom, env=env, now=NOW)
        statuses = {e["kind"]: e["status"] for e in report["entries"]}
        self.assertEqual(statuses["k8s_tls_secret"], "parse_error")
        self.assertEqual(statuses["proxmox_pveproxy"], "ok")

    def test_proxmox_settings_from_env(self):
        self.assertIsNone(cce.proxmox_settings(env={}))
        cfg = cce.proxmox_settings(
            env={"PROXMOX_TOKEN_ID": "a!t", "PROXMOX_TOKEN_SECRET": "s"}
        )
        self.assertEqual(cfg["host"], cce.DEFAULT_PROXMOX_HOST)
        self.assertEqual(cfg["port"], 8006)
        self.assertEqual(cfg["node"], "node01")


class CollectViaKubectlTest(unittest.TestCase):
    def test_success_filters_tls_type(self):
        payload = json.dumps(
            {
                "items": [
                    secret_item("argocd", "argocd-tls", "x"),
                    {"metadata": {}, "type": "Opaque", "data": {}},
                ]
            }
        ).encode()
        with mock.patch.object(
            subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout=payload, stderr=b""),
        ) as run_mock:
            got = cce.collect_k8s_tls_secrets_via_kubectl("kubectl")
        self.assertEqual(
            run_mock.call_args.args[0],
            ["kubectl", "get", "secrets", "--all-namespaces", "-o", "json"],
        )
        self.assertEqual(run_mock.call_args.kwargs.get("timeout"), 60)
        self.assertEqual([i["metadata"]["name"] for i in got], ["argocd-tls"])

    def test_failure_raises_runtime_error(self):
        with mock.patch.object(
            subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout=b"", stderr=b"denied"),
        ):
            with self.assertRaises(RuntimeError) as cm:
                cce.collect_k8s_tls_secrets_via_kubectl("kubectl")
        self.assertIn("denied", str(cm.exception))


# ---------------------------------------------------------------------------
# fixture 再生と CLI 契約
# ---------------------------------------------------------------------------


def sample_fixture():
    """sample.json と同じ構成の fixture を作る (正常/warn/失効/パース不能+Proxmox)。"""
    return {
        "now": "2026-08-23T12:00:00Z",
        "k8s_secrets": {
            "items": [
                secret_item("argocd", "argocd-tls", crt_b64(HEALTHY_PEM)),
                secret_item("coder", "coder-access", crt_b64(WARN_PEM)),
                secret_item("immich", "immich-tls", crt_b64(EXPIRED_PEM)),
                secret_item(
                    "vaultwarden", "vw-admin-tls",
                    base64.b64encode(b"this is not a certificate").decode(),
                ),
            ]
        },
        "proxmox": {
            "node": "node01",
            "response": {
                "data": {
                    "info": [
                        pve_info(
                            epoch(2030, 1, 1),
                            san=["DNS:localhost", "IP:127.0.0.1"],
                        )
                    ]
                }
            },
        },
    }


class FixtureMainContractTest(unittest.TestCase):
    def run_main(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            rc = cce.main(argv)
        return rc, buf.getvalue()

    def test_fixture_run_is_offline_green_and_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.json"
            path.write_text(json.dumps(sample_fixture()), encoding="utf-8")
            first_rc, first_out = self.run_main(["--fixture", str(path)])
            second_rc, second_out = self.run_main(["--fixture", str(path)])
        self.assertEqual((first_rc, second_rc), (0, 0))
        # now が固定されているので同じ入力は byte 等しくなる (diff を汚さない)
        self.assertEqual(first_out, second_out)
        report = json.loads(first_out)
        by_name = {e.get("name"): e for e in report["entries"]}
        self.assertEqual(by_name["argocd-tls"]["status"], "ok")
        self.assertEqual(by_name["coder-access"]["status"], "warn")
        self.assertEqual(by_name["immich-tls"]["status"], "critical")
        self.assertEqual(by_name["vw-admin-tls"]["status"], "parse_error")
        self.assertEqual(report["summary"]["status"], "critical")
        self.assertIs(report["t0107"]["resolved"], False)

    def test_committed_sample_fixture_runs_green(self):
        sample = REPO / "ops" / "tests" / "fixtures" / "cert_expiry" / "sample.json"
        rc, out = self.run_main(["--fixture", str(sample)])
        self.assertEqual(rc, 0, out)
        report = json.loads(out)
        self.assertEqual(report["summary"]["status"], "critical")
        self.assertEqual(
            {e["name"]: e["status"] for e in report["entries"]
             if e.get("kind") == "k8s_tls_secret"},
            {
                "argocd-tls": "ok",
                "coder-access": "warn",
                "immich-tls": "critical",
                "vw-admin-tls": "parse_error",
            },
        )

    def test_expected_pve_name_override_flips_t0107(self):
        doc = sample_fixture()
        doc["proxmox"]["response"]["data"]["info"][0]["san"] = [
            "DNS:real-pve.tailnet.example"
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            _, out_default = self.run_main(["--fixture", str(path)])
            _, out_named = self.run_main(
                ["--fixture", str(path), "--expected-pve-name", "real-pve.tailnet.example"]
            )
        self.assertIs(json.loads(out_default)["t0107"]["resolved"], False)
        self.assertIs(json.loads(out_named)["t0107"]["resolved"], True)

    def test_missing_proxmox_key_is_unconfigured_not_alerting(self):
        doc = {"now": "2026-08-23T12:00:00Z", "k8s_secrets": {"items": []}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            rc, out = self.run_main(["--fixture", str(path)])
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertEqual(report["summary"]["status"], "no_data")

    def test_broken_fixtures_exit_1_with_stderr(self):
        err = StringIO()
        with mock.patch.object(sys, "stderr", err):
            rc_missing = cce.main(["--fixture", "/nonexistent/fixture.json"])
        self.assertEqual(rc_missing, 1)

        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with mock.patch.object(sys, "stderr", StringIO()):
                rc_bad = cce.main(["--fixture", str(bad)])
        self.assertEqual(rc_bad, 1)


# ---------------------------------------------------------------------------
# heart 側の抽出と抑制 (facts.cert_alert + budget_alert_due 再利用)
# ---------------------------------------------------------------------------


class FactsCertAlertTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ops.heart import facts

        cls.facts = facts

    def summary(self, status):
        return {"summary": {"status": status, "reason": "理由"}}

    def test_warn_and_critical_are_extracted(self):
        for status in ("warn", "critical"):
            with self.subTest(status=status):
                alert = self.facts.cert_alert({"cert_expiry": self.summary(status)})
                self.assertEqual(alert, {"status": status, "reason": "理由"})

    def test_ok_and_no_data_and_missing_section_are_none(self):
        cases = [
            None,
            {},
            {"cert_expiry": None},
            {"cert_expiry": {}},
            {"cert_expiry": {"summary": {"status": "ok"}}},
            {"cert_expiry": {"summary": {"status": "no_data"}}},
            {"cert_expiry": {"error": "収集ごと失敗"}},
            "not-a-dict",
        ]
        for doc in cases:
            with self.subTest(doc=doc):
                self.assertIsNone(self.facts.cert_alert(doc))

    def test_dedup_reuses_budget_alert_due_semantics(self):
        due = self.facts.budget_alert_due
        alert = {"status": "warn", "reason": "r"}
        self.assertTrue(due(alert, None, "2026-08-23"))
        self.assertFalse(due(alert, {"status": "warn", "date": "2026-08-23"}, "2026-08-23"))
        self.assertTrue(due(alert, {"status": "warn", "date": "2026-08-22"}, "2026-08-23"))
        # warn→critical への悪化は同日内でも再度鳴る
        self.assertTrue(
            due(alert, {"status": "critical", "date": "2026-08-23"}, "2026-08-23")
        )
        self.assertFalse(due(None, {"status": "warn", "date": "2026-08-23"}, "2026-08-23"))


# ---------------------------------------------------------------------------
# 配線契約 (同期コピー・kustomization・rbac・report・notes)
# ---------------------------------------------------------------------------


class WiringContractTest(unittest.TestCase):
    def test_manual_sync_copy_is_byte_identical(self):
        """手動同期コピーの同一性を機械検査 (version_watch.py の「忘れる」を封じる)。"""
        self.assertEqual(
            CANONICAL.read_bytes(),
            COPY.read_bytes(),
            "apps/ops-health-reporter/check_cert_expiry.py が正本と不一致。cp で同期すること",
        )
        # 正本に「コピーが存在する」旨が書いてあること (読む人への警告)
        self.assertIn("手動同期コピー", CANONICAL.read_text(encoding="utf-8"))

    def test_kustomization_embeds_the_copy(self):
        text = (REPO / "apps" / "ops-health-reporter" / "kustomization.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("check_cert_expiry.py", text)

    def test_rbac_grants_read_only_secret_access_with_guard_comment(self):
        text = (REPO / "apps" / "ops-health-reporter" / "rbac.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn('resources: ["secrets"]', text)
        self.assertIn('verbs: ["get", "list"]', text)
        # 値を出さない約束がコメントとして残っていること
        self.assertIn("tls.crt", text)

    def test_report_registers_cert_section_and_notes_explain_it(self):
        source = (REPO / "apps" / "ops-health-reporter" / "report.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("import check_cert_expiry", source)
        self.assertIn("def collect_cert_expiry(", source)
        self.assertIn('"cert_expiry": collect(collect_cert_expiry)', source)
        self.assertIn("cert_expiry キーは TLS 証明書の期限台帳", source)


if __name__ == "__main__":
    unittest.main()
