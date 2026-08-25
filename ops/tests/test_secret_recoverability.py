"""ops/tools/secret_recoverability.py の分類決定論を固定する (P-9065)。

リポジトリルートから `python3 -m pytest ops/tests/test_secret_recoverability.py -q`
(pytest 無し環境ではルートの shim が unittest 収集で代行)。CI の
`python3 -m unittest discover -s ops/tests -t .` からも走る。

固定する契約:
- allowlist 内のキー → recoverable、外のキー → doppler_only (決定論的)
- dataFrom は「キーを列挙できない」問題として検出され、unclassifiable に載る
- 全ての keys が recovery_path を持つ (verify の assert と同型)
- recovery_path 未定義のキーは fail-closed (problems に入り、rc=1 になる元)
- ExternalSecret が 1 つも無いリポジトリは失敗扱い (走査の失敗を整合と偽らない)
"""

import json
import tempfile
import unittest
from pathlib import Path

from ops.tools import secret_recoverability as sr

ALLOWLIST = {"AUTOPILOT_GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "DISCORD_WEBHOOK_URL"}
RECOVERY_PATHS = {
    "AUTOPILOT_GITHUB_TOKEN": "GitHub で再発行し Doppler へ登録",
    "CLAUDE_CODE_OAUTH_TOKEN": "Claude Code の再ログインで再発行し Doppler へ登録",
    "DISCORD_WEBHOOK_URL": "Discord サーバー設定で再取得し Doppler へ登録",
    "VAULTWARDEN_ADMIN_TOKEN": "vaultwarden hash で再生成し Doppler へ登録",
}

ES_TMPL = """apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {name}
  namespace: {ns}
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler
  target:
    name: {name}
  data:
{data}
"""


def data_item(key: str, secret_key: str | None = None) -> str:
    return f"    - secretKey: {secret_key or key}\n      remoteRef:\n        key: {key}\n"


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_apps(root: Path, **files: str) -> Path:
    """<app>/external-secret.yaml を置いて apps_dir を返す。"""
    apps = root / "apps"
    for rel, text in files.items():
        write(apps, rel, text)
    return apps


class TestClassifyKeys(unittest.TestCase):
    def classify(self, keys, allowlist=ALLOWLIST, paths=RECOVERY_PATHS):
        return sr.classify_keys(set(keys), allowlist, paths)

    def test_allowlist_key_is_recoverable(self):
        entries, problems = self.classify(["AUTOPILOT_GITHUB_TOKEN"])
        self.assertEqual(problems, [])
        self.assertEqual(entries[0]["classification"], "recoverable")
        self.assertTrue(entries[0]["recovery_path"])

    def test_non_allowlist_key_is_doppler_only(self):
        entries, problems = self.classify(["VAULTWARDEN_ADMIN_TOKEN"])
        self.assertEqual(problems, [])
        self.assertEqual(entries[0]["classification"], "doppler_only")
        self.assertTrue(entries[0]["recovery_path"])

    def test_mixed_keys_classified_deterministically(self):
        keys = {"DISCORD_WEBHOOK_URL", "VAULTWARDEN_ADMIN_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
        entries, problems = self.classify(keys)
        self.assertEqual(problems, [])
        by_key = {e["key"]: e["classification"] for e in entries}
        self.assertEqual(by_key["DISCORD_WEBHOOK_URL"], "recoverable")
        self.assertEqual(by_key["CLAUDE_CODE_OAUTH_TOKEN"], "recoverable")
        self.assertEqual(by_key["VAULTWARDEN_ADMIN_TOKEN"], "doppler_only")
        # 決定論: 同じ入力は同じ順序で同じ分類
        entries2, _ = self.classify(keys)
        self.assertEqual([e["key"] for e in entries], [e["key"] for e in entries2])

    def test_every_entry_has_recovery_path(self):
        keys = set(ALLOWLIST) | {"VAULTWARDEN_ADMIN_TOKEN"}
        entries, _ = self.classify(keys)
        self.assertTrue(entries)
        self.assertTrue(all(e.get("recovery_path") for e in entries))

    def test_missing_recovery_path_fails_closed(self):
        entries, problems = self.classify(["RESTIC_PASSWORD"], paths=RECOVERY_PATHS)
        self.assertEqual(entries, [])
        self.assertTrue(any("recovery_path" in p for p in problems))


class TestScanExternalsecrets(unittest.TestCase):
    def scan(self, apps: Path):
        return sr.scan_externalsecrets(apps)

    def test_collects_keys_and_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            apps = root / "apps"
            write(
                apps,
                "vaultwarden/admin-token-external-secret.yaml",
                ES_TMPL.format(
                    name="vaultwarden-admin-token",
                    ns="vaultwarden",
                    data=data_item("VAULTWARDEN_ADMIN_TOKEN"),
                ),
            )
            write(
                apps,
                "autopilot/external-secret.yaml",
                ES_TMPL.format(
                    name="autopilot-credentials",
                    ns="autopilot",
                    data=data_item("CLAUDE_CODE_OAUTH_TOKEN")
                    + data_item("AUTOPILOT_GITHUB_TOKEN"),
                ),
            )
            sources, problems = self.scan(apps)
        self.assertEqual(problems, [])
        by_path = {s["path"]: s["keys"] for s in sources}
        self.assertEqual(by_path["autopilot/external-secret.yaml"],
                         ["AUTOPILOT_GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertEqual(by_path["vaultwarden/admin-token-external-secret.yaml"],
                         ["VAULTWARDEN_ADMIN_TOKEN"])

    def test_datafrom_is_unclassifiable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            apps = make_apps(
                root,
                **{
                    "weird/external-secret.yaml": ES_TMPL.format(
                        name="weird", ns="weird",
                        data="  dataFrom:\n    - extract: {}\n"),
                },
            )
            sources, problems = self.scan(apps)
        self.assertTrue(any("dataFrom" in p for p in problems))
        # keys=None のエントリ (列挙できない) が sources に載る
        self.assertEqual([s["path"] for s in sources if s["keys"] is None],
                         ["weird/external-secret.yaml"])

    def test_missing_remote_ref_key_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            apps = make_apps(
                root,
                **{
                    "broken/external-secret.yaml": ES_TMPL.format(
                        name="broken", ns="broken",
                        data="    - secretKey: X\n      remoteRef: {}\n"),
                },
            )
            sources, problems = self.scan(apps)
        self.assertTrue(any("remoteRef.key" in p for p in problems))

    def test_broken_yaml_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            apps = make_apps(root, **{"bad/external-secret.yaml": "kind: [ExternalSecret\n"})
            _, problems = self.scan(apps)
        self.assertTrue(any("YAML が読めない" in p for p in problems))

    def test_no_externalsecret_is_empty_not_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            apps = make_apps(root, **{"plain.yaml": "kind: ConfigMap\n"})
            sources, problems = self.scan(apps)
        self.assertEqual(sources, [])
        self.assertEqual(problems, [])


class TestBuildReport(unittest.TestCase):
    """build_report の JSON 出力契約 (verify の assert と同型)。"""

    def build(self, rules, apps: Path, root: Path):
        return sr.build_report(root=root, apps_dir=apps, rules=rules)

    def test_healthy_fixture_has_no_problems_and_full_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "ops").mkdir(parents=True)
            (root / "ops" / "rules.json").write_text(
                json.dumps({"allowed_autopilot_doppler_keys": sorted(ALLOWLIST)})
            )
            apps = root / "apps"
            write(apps, "autopilot/external-secret.yaml", ES_TMPL.format(
                name="autopilot-credentials", ns="autopilot",
                data=data_item("AUTOPILOT_GITHUB_TOKEN") + data_item("CLAUDE_CODE_OAUTH_TOKEN")))
            write(apps, "vaultwarden/admin-token-external-secret.yaml", ES_TMPL.format(
                name="vaultwarden-admin-token", ns="vaultwarden",
                data=data_item("VAULTWARDEN_ADMIN_TOKEN")))
            report, problems = self.build(root, apps, root)
        self.assertEqual(problems, [], "\n".join(problems))
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["keys"])
        self.assertTrue(all(k.get("recovery_path") for k in report["keys"]))
        self.assertEqual(report["unclassifiable"], [])
        for k in report["keys"]:
            self.assertIn(k["key"], ("AUTOPILOT_GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                                     "VAULTWARDEN_ADMIN_TOKEN"))

    def test_missing_rules_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            apps = make_apps(
                root,
                **{
                    "autopilot/external-secret.yaml": ES_TMPL.format(
                        name="x", ns="x", data=data_item("AUTOPILOT_GITHUB_TOKEN")),
                },
            )
            _, problems = self.build(root, apps, root)
        self.assertTrue(any("rules.json" in p for p in problems))

    def test_empty_apps_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "ops").mkdir(parents=True)
            (root / "ops" / "rules.json").write_text(
                json.dumps({"allowed_autopilot_doppler_keys": sorted(ALLOWLIST)}))
            report, problems = self.build(root, root / "apps", root)
        self.assertTrue(any("1 つも見つけられなかった" in p for p in problems))
        self.assertFalse(report["keys"])


class TestRealRepo(unittest.TestCase):
    """今のリポジトリの地形。ここが壊れたら参照キーか走査のどちらかが変わった。"""

    @classmethod
    def setUpClass(cls):
        cls.report, cls.problems = sr.build_report(sr.ROOT)

    def test_current_repo_has_no_problems(self):
        self.assertEqual(self.problems, [], "\n".join(self.problems))

    def test_keys_non_empty_and_all_have_recovery_path(self):
        # verify の assert と同型 (wrapper がそのまま実測する)
        self.assertTrue(self.report["keys"])
        self.assertTrue(all(k.get("recovery_path") for k in self.report["keys"]))

    def test_no_unclassifiable(self):
        self.assertEqual(self.report["unclassifiable"], [])

    def test_key_count_matches_declared_doppler_keys(self):
        # check_credential_map.py の DECLARED_DOPPLER_KEYS と同じ実体から来ているはず
        self.assertEqual(len(self.report["keys"]), 26)
        self.assertEqual(len(self.report["allowlist"]), 10)

    def test_allowlist_keys_are_recoverable(self):
        by_key = {k["key"]: k["classification"] for k in self.report["keys"]}
        for allow in self.report["allowlist"]:
            self.assertEqual(by_key[allow], "recoverable")
        for k, c in by_key.items():
            if k not in self.report["allowlist"]:
                self.assertEqual(c, "doppler_only")


if __name__ == "__main__":
    unittest.main()