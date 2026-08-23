"""ops/tools/version_watch.py の判定ロジックを固定する (P-0126)。

リポジトリルートから `python3 -m unittest ops.tests.test_version_watch`。
HTTP 層は FakeFetcher (URL -> (status, body) の辞書) に差し替え、**一切ネットワークに
出ない**。辞書に無い URL へのアクセスは即座に失敗するので、実装が勝手に別の
エンドポイントを叩いて通ってしまうことも防ぐ。
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ops.tools import version_watch as vw


class FakeFetcher:
    """URL をキーにレスポンスを返す fetch。network-free の要。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError("fixture に無い URL へのアクセス: {}".format(url))
        return self.responses[url]


def body(obj):
    return json.dumps(obj).encode()


class TestParseCore(unittest.TestCase):
    def test_examples(self):
        cases = {
            "v1.98.9": (1, 98, 9),
            "argo-cd-9.1.7": (9, 1, 7),
            "1.37.1-alpine": (1, 37, 1),
            "16.9-0.4.3": (16, 9, 0, 4, 3),
            "2026.7.1": (2026, 7, 1),
            "latest": (),
        }
        for raw, want in cases.items():
            self.assertEqual(vw.parse_core(raw), want, raw)


class TestVariantOf(unittest.TestCase):
    def test_examples(self):
        cases = {
            "1.37.1-alpine": "alpine",
            "3.14-alpine": "alpine",
            "17.10": None,
            "16.9-0.4.3": None,
            "v3.1.0": None,
        }
        for raw, want in cases.items():
            self.assertEqual(vw.variant_of(raw), want, raw)


class TestCoresEqual(unittest.TestCase):
    def test_prefix_is_equal(self):
        """major 系 pin (v7) が patch の存在だけで drift 扱いされないこと。"""
        self.assertTrue(vw.cores_equal((7,), (7, 0, 5)))

    def test_different_is_not_equal(self):
        self.assertFalse(vw.cores_equal((3, 21, 3), (3, 22, 0)))
        self.assertFalse(vw.cores_equal((1, 37, 1), (1, 38, 0)))


class TestIsComparableCurrent(unittest.TestCase):
    def test_digest_pins_and_placeholders_are_not_comparable(self):
        for raw in ("sha256 digest pin", "sha256:c610fcdf", "flake.lock の rev"):
            self.assertFalse(vw.is_comparable_current(raw), raw)

    def test_versions_are_comparable(self):
        for raw in ("1.37.1-alpine", "v7", "2026.7.1 (index digest pin)", "0.111.1"):
            self.assertTrue(vw.is_comparable_current(raw), raw)


class TestStripPrefixes(unittest.TestCase):
    def test_release_prefix_and_v(self):
        self.assertEqual(
            vw.strip_version_prefixes("argo-cd-9.1.7", "argo-cd-"), "9.1.7"
        )
        self.assertEqual(vw.strip_version_prefixes("v1.102.2", None), "1.102.2")
        # release_prefix 無しでタグに repo 名接頭子が付く形 (dex-0.25.0) は
        # 剥がれない。比較自体は core で行うので drift 判定には影響しない
        self.assertEqual(vw.strip_version_prefixes("dex-0.25.0", None), "dex-0.25.0")


class TestGithubLatest(unittest.TestCase):
    def url(self, repo="argoproj/argo-helm"):
        return "https://api.github.com/repos/{}/releases/latest".format(repo)

    def test_strips_release_prefix(self):
        fetch = FakeFetcher({self.url(): (200, body({"tag_name": "argo-cd-9.1.7"}))})
        self.assertEqual(
            vw.github_latest(fetch, "argoproj/argo-helm", "argo-cd-"), "9.1.7"
        )

    def test_no_stable_release_is_none(self):
        fetch = FakeFetcher({self.url("octo/no-releases"): (404, b"{}")})
        self.assertIsNone(vw.github_latest(fetch, "octo/no-releases"))

    def test_http_error_raises(self):
        fetch = FakeFetcher(
            {self.url("octo/broken"): (403, b'{"message": "rate limit"}')}
        )
        with self.assertRaises(RuntimeError):
            vw.github_latest(fetch, "octo/broken")


class TestDockerhubLatest(unittest.TestCase):
    def url(self, path="library/postgres"):
        return (
            "https://hub.docker.com/v2/repositories/{}/tags"
            "?page_size=100&ordering=-last_updated".format(path)
        )

    def results(self, names):
        return {"results": [{"name": n} for n in names]}

    def test_picks_newest_same_variant(self):
        fetch = FakeFetcher(
            {
                self.url(): (
                    200,
                    body(
                        self.results(
                            ["18.2-alpine3.22", "17.11-alpine", "17.10", "latest"]
                        )
                    ),
                )
            }
        )
        self.assertEqual(
            vw.dockerhub_latest(fetch, "library/postgres", "alpine"), "17.11-alpine"
        )

    def test_plain_current_ignores_variant_tags(self):
        """plain 運用の対象が alpine 版の新番に引きずられないこと。"""
        fetch = FakeFetcher(
            {self.url(): (200, body(self.results(["18.2-alpine", "17.11", "17.10"])))}
        )
        self.assertEqual(vw.dockerhub_latest(fetch, "library/postgres", None), "17.11")

    def test_numeric_dash_tag_counts_as_plain(self):
        fetch = FakeFetcher(
            {
                self.url("library/vectorchord"): (
                    200,
                    body(self.results(["16.14-1.1.1", "16.9-0.4.3", "latest"])),
                )
            }
        )
        self.assertEqual(
            vw.dockerhub_latest(fetch, "library/vectorchord", None), "16.14-1.1.1"
        )

    def test_no_matching_tag_returns_none(self):
        fetch = FakeFetcher({self.url(): (200, body(self.results(["latest", "edge"])))})
        self.assertIsNone(vw.dockerhub_latest(fetch, "library/postgres", "alpine"))

    def test_http_error_raises(self):
        fetch = FakeFetcher({self.url(): (503, b"unavailable")})
        with self.assertRaises(RuntimeError):
            vw.dockerhub_latest(fetch, "library/postgres", None)


class TestNpmLatest(unittest.TestCase):
    def test_dist_tag_latest(self):
        url = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"
        fetch = FakeFetcher({url: (200, body({"version": "2.1.224"}))})
        self.assertEqual(vw.npm_latest(fetch, "@anthropic-ai/claude-code"), "2.1.224")


class TestCheckTarget(unittest.TestCase):
    GH = "https://api.github.com/repos/dani-garcia/vaultwarden/releases/latest"

    def target(self, **over):
        t = {
            "id": "vaultwarden",
            "kind": "image",
            "name": "vaultwarden/server",
            "current": "1.37.1-alpine",
            "upstream": "github:dani-garcia/vaultwarden",
            "policy": "auto",
        }
        t.update(over)
        return t

    def test_drift_true(self):
        fetch = FakeFetcher({self.GH: (200, body({"tag_name": "1.38.0"}))})
        r = vw.check_target(self.target(), fetch)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["drifted"])
        self.assertEqual(r["latest"], "1.38.0")

    def test_drift_false_with_v_and_variant_noise(self):
        fetch = FakeFetcher({self.GH: (200, body({"tag_name": "v1.37.1-alpine"}))})
        r = vw.check_target(self.target(), fetch)
        self.assertFalse(r["drifted"])

    def test_uncomparable_current_is_skipped_without_fetching(self):
        fetch = FakeFetcher({})
        r = vw.check_target(
            self.target(current="sha256 digest pin", upstream="github:o/r"), fetch
        )
        self.assertEqual(r["status"], "uncomparable")
        self.assertEqual(fetch.calls, [], "比較不能な対象へ取りに行ってはいけない")

    def test_unknown_scheme_is_error_not_silent(self):
        fetch = FakeFetcher({})
        r = vw.check_target(self.target(upstream="gitlab:o/r"), fetch)
        self.assertEqual(r["status"], "error")
        self.assertIn("未知の upstream scheme", r["error"])

    def test_network_exception_becomes_error_entry(self):
        def boom(url):
            raise OSError("timed out")

        r = vw.check_target(self.target(), boom)
        self.assertEqual(r["status"], "error")
        self.assertIn("OSError", r["error"])

    def test_github_404_is_error(self):
        fetch = FakeFetcher({self.GH: (404, b"{}")})
        r = vw.check_target(self.target(), fetch)
        self.assertEqual(r["status"], "error")
        self.assertIn("安定リリース", r["error"])

    def test_major_only_pin_against_patch_bump_is_not_drift(self):
        """actions/checkout (current v7) vs 上流 v7.0.5 — 接頭辞 core は同値。"""
        gh = "https://api.github.com/repos/actions/checkout/releases/latest"
        fetch = FakeFetcher({gh: (200, body({"tag_name": "v7.0.5"}))})
        r = vw.check_target(
            {
                "id": "gha-checkout",
                "kind": "github-action",
                "current": "v7",
                "upstream": "github:actions/checkout",
            },
            fetch,
        )
        self.assertFalse(r["drifted"])


class TestCheckAllAndSummarize(unittest.TestCase):
    def test_order_preserved_and_summary_correct(self):
        gh = "https://api.github.com/repos/o/r/releases/latest"
        hub = (
            "https://hub.docker.com/v2/repositories/library/busybox/tags"
            "?page_size=100&ordering=-last_updated"
        )
        targets = [
            {"id": "a", "kind": "image", "current": "1.0.0", "upstream": "github:o/r"},
            {
                "id": "b",
                "kind": "image",
                "current": "2.0.0",
                "upstream": "dockerhub:library/busybox",
            },
            {
                "id": "c",
                "kind": "image",
                "current": "sha256 digest pin",
                "upstream": "github:o/r",
            },
        ]
        fetch = FakeFetcher(
            {
                gh: (200, body({"tag_name": "v2.0.0"})),
                hub: (200, body({"results": [{"name": "2.0.0"}, {"name": "latest"}]})),
            }
        )
        results = vw.check_all(targets, fetch)
        self.assertEqual([r["id"] for r in results], ["a", "b", "c"])
        s = vw.summarize(results)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["ok"], 2)
        self.assertEqual(s["drifted"], 1)
        self.assertEqual(s["errors"], 0)
        self.assertEqual(s["uncomparable"], 1)


class TestMainContract(unittest.TestCase):
    """main() の exit code 契約。verify は rc と stdout の JSON しか見ない。"""

    def run_main(self, inventory_path, fetch):
        buf = io.StringIO()
        with mock.patch.object(vw, "http_get", fetch), redirect_stdout(buf):
            rc = vw.main([inventory_path])
        return rc, buf.getvalue()

    def write_inventory(self, root, targets):
        path = Path(root) / "inventory.json"
        path.write_text(json.dumps({"targets": targets}), encoding="utf-8")
        return str(path)

    def test_prints_json_and_returns_zero(self):
        gh = "https://api.github.com/repos/o/r/releases/latest"
        with tempfile.TemporaryDirectory() as d:
            path = self.write_inventory(
                d,
                [
                    {
                        "id": "a",
                        "kind": "image",
                        "current": "1.0.0",
                        "upstream": "github:o/r",
                    }
                ],
            )
            rc, out = self.run_main(
                path, FakeFetcher({gh: (200, body({"tag_name": "v2.0.0"}))})
            )
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["summary"]["drifted"], 1)

    def test_returns_one_when_inventory_unreadable(self):
        rc, _ = self.run_main("/nonexistent/inventory.json", FakeFetcher({}))
        self.assertEqual(rc, 1)


class TestRealRepo(unittest.TestCase):
    """今のリポジトリの地形。

    ここが壊れたら inventory の upstream 記法か watcher の対応表のどちらかが
    変わったということ。未知 scheme は check_all では error 記録になるが、
    こちらでは fail させて気づけるようにする。
    """

    @classmethod
    def setUpClass(cls):
        cls.targets = vw.load_inventory(vw.INVENTORY)

    def test_every_upstream_scheme_is_supported(self):
        for t in self.targets:
            scheme = vw._scheme_of(t.get("upstream") or "")
            self.assertIn(
                scheme,
                vw.SUPPORTED_SCHEMES,
                "{} の upstream {} は未対応 scheme".format(t.get("id"), t.get("upstream")),
            )

    def test_required_fields_exist_on_every_target(self):
        for t in self.targets:
            for key in ("id", "kind", "current", "upstream"):
                self.assertTrue(t.get(key), "{} に {} が無い".format(t.get("id"), key))

    def test_uncomparable_targets_are_a_known_minority(self):
        """digest pin 等の比較不能対象は意図的に少数派であることを保つ。"""
        uncomparable = [t for t in self.targets if not vw.is_comparable_current(t["current"])]
        self.assertGreaterEqual(len(uncomparable), 2)
        self.assertLess(len(uncomparable), len(self.targets) // 4)


if __name__ == "__main__":
    unittest.main()
