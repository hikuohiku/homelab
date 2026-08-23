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
    def latest_url(self, repo="argoproj/argo-helm"):
        return "https://api.github.com/repos/{}/releases/latest".format(repo)

    def list_url(self, repo="argoproj/argo-helm"):
        return "https://api.github.com/repos/{}/releases?per_page=100".format(repo)

    def rel(self, tag, **over):
        r = {"tag_name": tag, "draft": False, "prerelease": False}
        r.update(over)
        return r

    def test_unprefixed_uses_releases_latest(self):
        fetch = FakeFetcher(
            {self.latest_url("octo/repo"): (200, body({"tag_name": "v1.2.3"}))}
        )
        self.assertEqual(vw.github_latest(fetch, "octo/repo"), "1.2.3")

    def test_prefixed_picks_first_stable_matching_release(self):
        """repo 全体の最新が別チャートのリリースでも、prefix 一致の安定版を拾う。
        argo-helm の初回実測 (2026-08-23): repo latest は argo-workflows-2.0.2 で、
        これを argocd-chart の「上流最新」として載せてしまう誤報を固定する。"""
        fetch = FakeFetcher(
            {
                self.list_url(): (
                    200,
                    body(
                        [
                            self.rel("argo-workflows-2.0.2"),
                            self.rel("argo-cd-9.1.6"),
                            self.rel("argo-workflows-1.9.9"),
                        ]
                    ),
                )
            }
        )
        self.assertEqual(
            vw.github_latest(fetch, "argoproj/argo-helm", "argo-cd-"), "9.1.6"
        )

    def test_prefixed_skips_prerelease(self):
        """/releases の一覧には prerelease が含まれるので自分で除く。"""
        fetch = FakeFetcher(
            {
                self.list_url(): (
                    200,
                    body(
                        [
                            self.rel("argo-cd-9.2.0", prerelease=True),
                            self.rel("argo-cd-9.1.6"),
                        ]
                    ),
                )
            }
        )
        self.assertEqual(
            vw.github_latest(fetch, "argoproj/argo-helm", "argo-cd-"), "9.1.6"
        )

    def test_prefixed_no_match_is_none(self):
        fetch = FakeFetcher(
            {self.list_url("octo/repo"): (200, body([self.rel("other-1.0")]))}
        )
        self.assertIsNone(vw.github_latest(fetch, "octo/repo", "argo-cd-"))

    def test_no_stable_release_is_none(self):
        fetch = FakeFetcher({self.latest_url("octo/no-releases"): (404, b"{}")})
        self.assertIsNone(vw.github_latest(fetch, "octo/no-releases"))

    def test_http_error_raises(self):
        fetch = FakeFetcher(
            {self.latest_url("octo/broken"): (403, b'{"message": "rate limit"}')}
        )
        with self.assertRaises(RuntimeError):
            vw.github_latest(fetch, "octo/broken")


class TestHubTagsUrl(unittest.TestCase):
    def test_plain_and_filtered_urls(self):
        """FakeFetcher が URL 完全一致で照合するため、URL 形式自体を契約として固定する。"""
        self.assertEqual(
            vw.hub_tags_url("library/python"),
            "https://hub.docker.com/v2/repositories/library/python/tags"
            "?page_size=100&ordering=-last_updated",
        )
        self.assertEqual(
            vw.hub_tags_url("library/python", "3.14"),
            "https://hub.docker.com/v2/repositories/library/python/tags"
            "?page_size=100&ordering=-last_updated&name=3.14",
        )


class TestNumericHead(unittest.TestCase):
    def test_examples(self):
        cases = {
            "3.14-alpine": "3.14",
            "17.10": "17.10",
            "9.1.1-alpine": "9.1.1",
            # 別系統タグ (数字始まりでない) は空。dockerhub 候補のフィルタに使う
            "buildroot-2014.02": "",
            "latest": "",
            "v1.2.0": "",
        }
        for raw, want in cases.items():
            self.assertEqual(vw.numeric_head(raw), want, raw)


class TestDockerhubLatest(unittest.TestCase):
    def url(self, path="library/postgres", name=None):
        return vw.hub_tags_url(path, name)

    def results(self, names):
        return {"results": [{"name": n} for n in names]}

    def test_anchored_family_wins_over_stale_global_garbage(self):
        """python の初回実測 (2026-08-23): 最近更新順の先頭 100 件には古代タグ
        (3.6.0a4-alpine 等) しか居ないことがある。家族ページの新しい patch が勝って
        「3.14-alpine -> 3.6.0a4-alpine」という偽 drift を二度と報告しないこと。"""
        fetch = FakeFetcher(
            {
                self.url("library/python", "3.14"): (
                    200,
                    body(
                        self.results(
                            ["3.14.7-alpine", "3.14-alpine", "3.13.9-alpine"]
                        )
                    ),
                ),
                self.url("library/python"): (
                    200,
                    body(self.results(["2.7.12-alpine", "3.6.0a4-alpine", "latest"])),
                ),
            }
        )
        self.assertEqual(
            vw.dockerhub_latest(fetch, "library/python", "alpine", "3.14-alpine"),
            "3.14.7-alpine",
        )

    def test_new_series_in_global_page_wins(self):
        """新系列 (major/minor 更新) は家族ページには原理的に現れない。
        全体ページの方が大きいときだけ「上がった」という意味になる。"""
        fetch = FakeFetcher(
            {
                self.url(name="17.10"): (200, body(self.results(["17.10"]))),
                self.url(): (200, body(self.results(["18.2", "9.6.3", "latest"]))),
            }
        )
        self.assertEqual(
            vw.dockerhub_latest(fetch, "library/postgres", None, "17.10"), "18.2"
        )

    def test_partial_match_of_other_series_does_not_pollute_anchor(self):
        """Hub API の name 絞り込みは部分一致なので head "9.1" が "19.1" 系にも
        引っかかる。startswith で弾くことを固定する (汚染されると偽 drift になる)。"""
        fetch = FakeFetcher(
            {
                self.url("library/valkey", "9.1.1"): (
                    200,
                    body(self.results(["19.1.5", "9.1.1"])),
                ),
                self.url("library/valkey"): (200, body(self.results(["9.1.1"]))),
            }
        )
        self.assertEqual(
            vw.dockerhub_latest(fetch, "library/valkey", None, "9.1.1"), "9.1.1"
        )

    def test_prefixed_tags_are_never_candidates(self):
        """busybox の初回実測: "buildroot-2014.02" が (2014, 2) という巨大 core で
        誤報した。数字始まりでないタグはどちらの頁でも候補外。"""
        fetch = FakeFetcher(
            {
                self.url("library/busybox", "1.38.0"): (
                    200,
                    body(self.results(["1.38.0"])),
                ),
                self.url("library/busybox"): (
                    200,
                    body(self.results(["buildroot-2014.02", "latest", "1.36.1"])),
                ),
            }
        )
        self.assertEqual(
            vw.dockerhub_latest(fetch, "library/busybox", None, "1.38.0"), "1.38.0"
        )

    def test_plain_current_ignores_variant_tags(self):
        """plain 運用の対象が alpine 版の新番に引きずられないこと。"""
        fetch = FakeFetcher(
            {
                self.url(name="17.10"): (200, body(self.results(["17.10"]))),
                self.url(): (
                    200,
                    body(self.results(["18.2-alpine", "17.10", "17.9"])),
                ),
            }
        )
        self.assertEqual(
            vw.dockerhub_latest(fetch, "library/postgres", None, "17.10"), "17.10"
        )

    def test_numeric_dash_tag_counts_as_plain(self):
        fetch = FakeFetcher(
            {
                self.url("library/vectorchord", "16.9"): (
                    200,
                    body(self.results(["16.9-0.4.3"])),
                ),
                self.url("library/vectorchord"): (
                    200,
                    body(self.results(["16.14-1.1.1", "16.9-0.4.3", "latest"])),
                ),
            }
        )
        self.assertEqual(
            vw.dockerhub_latest(fetch, "library/vectorchord", None, "16.9-0.4.3"),
            "16.14-1.1.1",
        )

    def test_no_matching_tag_returns_none(self):
        fetch = FakeFetcher(
            {
                self.url(name="1.37"): (200, body(self.results(["latest", "edge"]))),
                self.url(): (200, body(self.results(["latest", "edge"]))),
            }
        )
        self.assertIsNone(
            vw.dockerhub_latest(fetch, "library/postgres", "alpine", "1.37-alpine")
        )

    def test_http_error_raises(self):
        fetch = FakeFetcher(
            {
                self.url(name="17.10"): (503, b"unavailable"),
                self.url(): (503, b"unavailable"),
            }
        )
        with self.assertRaises(RuntimeError):
            vw.dockerhub_latest(fetch, "library/postgres", None, "17.10")


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
        hub = vw.hub_tags_url("library/busybox")
        hub_anchor = vw.hub_tags_url("library/busybox", "2.0.0")
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
                hub_anchor: (200, body({"results": [{"name": "2.0.0"}]})),
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
