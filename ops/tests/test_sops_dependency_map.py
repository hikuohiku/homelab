"""ops/tools/sops_dependency_map.py の純粋関数と実リポジトリ走査を固定する (P-0105)。

リポジトリルートから `python3 -m unittest ops.tests.test_sops_dependency_map`。
実リポジトリだけを見る検査は「今たまたま通っている」と「正しい」を区別できないので、
探索・分類の純関数側は合成入力で両方向を固定する。
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ops.tools import sops_dependency_map as sdm

RECIPIENT = "age1qqqexample00000000000000000000000000000000000000000000synth"

# 実物と同じ形の暗号化ファイル。ENC マーカー (data: 以降に実長 base64) と
# sops メタデータブロックの両方を持つ — 片方だけだと検出されない
ENCRYPTED_YAML = f"""doppler-token: ENC[AES256_GCM,data:{'A' * 64},iv:{'B' * 44},tag:{'C' * 44},type:str]
sops:
    age:
        - recipient: {RECIPIENT}
          enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
            {'Z' * 64}
            -----END AGE ENCRYPTED FILE-----
    lastmodified: "2025-12-13T16:42:11Z"
    mac: ENC[AES256_GCM,data:{'D' * 64},iv:{'E' * 44},tag:{'F' * 44},type:str]
    unencrypted_suffix: _unencrypted
    version: 3.11.0
"""

SOPS_CONFIG = """keys:
  - &node01 %s

creation_rules:
  - path_regex: nix/images/proxmox-cloud/secrets\\.yaml$
    key_groups:
      - age:
          - *node01
""" % RECIPIENT


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(
    root: Path,
    *,
    encrypted: str = ENCRYPTED_YAML,
    config: str | None = SOPS_CONFIG,
    enc_path: str = "nix/images/proxmox-cloud/secrets.yaml",
) -> None:
    if config is not None:
        write(root, ".sops.yaml", config)
    write(root, enc_path, encrypted)
    write(
        root,
        "nix/images/proxmox-cloud/configuration.nix",
        "sops.defaultSopsFile = ./secrets.yaml;\n",
    )


class TestIsSopsEncrypted(unittest.TestCase):
    def test_real_shape_is_detected(self):
        self.assertTrue(sdm.is_sops_encrypted(ENCRYPTED_YAML))

    def test_prose_mentioning_enc_marker_is_not_detected(self):
        """PROJECT.md の `ENC[` のような散文言及はマーカー単独では当てにならない。"""
        prose = "本文の `ENC[` マーカーから暗号化ファイルを探す (sops の話)\n"
        self.assertFalse(sdm.is_sops_encrypted(prose))

    def test_marker_without_metadata_is_not_detected(self):
        text = f"foo: ENC[AES256_GCM,data:{'A' * 64},type:str]\n"
        self.assertFalse(sdm.is_sops_encrypted(text))

    def test_plain_yaml_is_not_detected(self):
        self.assertFalse(
            sdm.is_sops_encrypted("apiVersion: v1\nkind: Secret\ndata: {}\n")
        )

    def test_short_blob_is_not_detected(self):
        """data: 以降が短すぎる (= 実物の形でない) 引用は落とす。"""
        text = f"note: ENC[AES256_GCM,data:xx]\nsops:\n  version: 3\n"
        self.assertFalse(sdm.is_sops_encrypted(text))


class TestParseEncryptedFile(unittest.TestCase):
    def test_extracts_keys_and_recipients(self):
        entry, problems = sdm.parse_encrypted_file("a/secrets.yaml", ENCRYPTED_YAML)
        self.assertEqual(problems, [])
        self.assertEqual(entry["keys_in_file"], ["doppler-token"])
        self.assertEqual(entry["recipients"], [RECIPIENT])

    def test_no_recipient_fails_closed(self):
        text = f"key: ENC[AES256_GCM,data:{'A' * 64}]\nsops:\n  version: 3\n"
        _, problems = sdm.parse_encrypted_file("a/secrets.yaml", text)
        self.assertTrue(any("recipient" in p for p in problems))


class TestLoadCreationRules(unittest.TestCase):
    def test_anchor_and_key_groups_are_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root, ".sops.yaml", SOPS_CONFIG)
            rules, problems = sdm.load_creation_rules(root)
        self.assertEqual(problems, [])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["age_recipients"], [RECIPIENT])
        self.assertIsNotNone(rules[0]["compiled"].search("nix/images/proxmox-cloud/secrets.yaml"))

    def test_missing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            rules, problems = sdm.load_creation_rules(Path(d))
        self.assertEqual(rules, [])
        self.assertEqual(len(problems), 1)


class TestFindReferences(unittest.TestCase):
    def test_classification_and_line_numbers(self):
        files = {
            "nix/images/proxmox-cloud/configuration.nix": 'defaultSopsFile = ./secrets.yaml;\n',
            ".github/workflows/ci.yml": "- run: cat secrets.yaml\n",
            "docs/recovery.md": "# secrets.yaml の復元\n",
            "ops/projects/logs/P-0105/PROGRESS.md": "secrets.yaml を触った\n",
            "other/my-secrets.yaml": "unrelated: true\n",  # 左境界のない言及は数えない
            ".sops.yaml": "path_regex: secrets\\.yaml$\n",
            "nix/images/proxmox-cloud/secrets.yaml": ENCRYPTED_YAML,  # 自分自身は除く
        }
        refs = sdm.find_references("nix/images/proxmox-cloud/secrets.yaml", files)
        kinds = set(refs)
        self.assertIn("nix", kinds)
        self.assertIn("ci", kinds)
        self.assertIn("doc", kinds)
        self.assertIn("log", kinds)
        self.assertNotIn("other", kinds)
        self.assertEqual(refs["nix"][0]["lines"], [1])
        # .sops.yaml と自分自身は参照として載らない
        for entries in refs.values():
            for entry in entries:
                self.assertNotIn(entry["path"], (".sops.yaml", "nix/images/proxmox-cloud/secrets.yaml"))


class TestKeyLocations(unittest.TestCase):
    def test_variable_chain_is_found(self):
        files = {
            "terraform/proxmox/variables.tf": 'variable "age_private_key" {\n}\n',
            "terraform/proxmox/vm-nixos.tf": "content: ${jsonencode(var.age_private_key)}\n"
            "  - path: /var/lib/sops-nix/key.txt\n",
            "nix/images/proxmox-cloud/configuration.nix":
                'age.keyFile = "/var/lib/sops-nix/key.txt";\n',
        }
        locs = sdm.find_key_locations(files)
        var = locs["private_key_variable"]
        self.assertEqual(var["defined_in"], ["terraform/proxmox/variables.tf"])
        self.assertEqual(var["referenced_in"], ["terraform/proxmox/vm-nixos.tf"])
        mentions = {m["path"] for m in locs["key_file"]["mentioned_in"]}
        self.assertEqual(
            mentions,
            {
                "terraform/proxmox/vm-nixos.tf",
                "nix/images/proxmox-cloud/configuration.nix",
            },
        )
        self.assertEqual(locs["key_file"]["path_on_node"], "/var/lib/sops-nix/key.txt")


class TestProbeAgentEnv(unittest.TestCase):
    def probe(self, env=None, home=".", which=lambda name: None):
        return sdm.probe_agent_env(env=env or {}, home=home, which_fn=which)

    def test_empty_environment_cannot_decrypt(self):
        cap = self.probe()
        self.assertFalse(cap["can_decrypt_now"])
        self.assertFalse(cap["sops_binary_found"])

    def test_binary_alone_is_not_enough(self):
        cap = self.probe(which=lambda name: "/usr/bin/sops")
        self.assertTrue(cap["sops_binary_found"])
        self.assertFalse(cap["can_decrypt_now"])

    def test_env_var_with_binary_is_optimistic_yes(self):
        cap = self.probe(env={"SOPS_AGE_KEY": "SECRET-VALUE"}, which=lambda name: "/usr/bin/sops")
        self.assertTrue(cap["can_decrypt_now"])
        self.assertEqual(cap["key_env_vars_present"], ["SOPS_AGE_KEY"])


class TestBuildMapProblems(unittest.TestCase):
    """fail-closed の各筋。地図は「何も見つけられない」「宣言と合わない」を黙らせない。"""

    def build(self, setup):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            setup(root)
            return sdm.build_map(root)

    def test_zero_encrypted_files_is_a_problem(self):
        _, problems = self.build(lambda root: write(root, "plain.yaml", "a: 1\n"))
        self.assertTrue(any("1 つも見つけられなかった" in p for p in problems))

    def test_unmatched_creation_rule_is_a_problem(self):
        def setup(root: Path):
            make_repo(root, config=SOPS_CONFIG.replace("proxmox-cloud", "somewhere-else"))
        _, problems = self.build(setup)
        self.assertTrue(any("creation_rule" in p for p in problems))

    def test_orphan_encrypted_file_is_a_problem(self):
        def setup(root: Path):
            write(root, ".sops.yaml", SOPS_CONFIG)
            write(root, "nix/images/proxmox-cloud/secrets.yaml", ENCRYPTED_YAML)
        _, problems = self.build(setup)
        self.assertTrue(any("消費されていない" in p for p in problems))

    def test_healthy_fixture_has_no_problems(self):
        def setup(root: Path):
            make_repo(root)
            write(root, "docs/recovery.md", "# secrets.yaml 復元\n")
        map_data, problems = self.build(setup)
        self.assertEqual(problems, [], "\n".join(problems))
        self.assertEqual(map_data["encrypted_files"][0]["recipients"], [RECIPIENT])
        self.assertEqual(
            map_data["encrypted_files"][0]["references"]["nix"][0]["path"],
            "nix/images/proxmox-cloud/configuration.nix",
        )
        self.assertTrue(map_data["creation_rules"][0]["matches_any_encrypted_file"])


class TestMainContract(unittest.TestCase):
    """main() の exit code 契約。verify は rc と生成物の存在しか見ない。"""

    def run_main(self, tmp: Path) -> int:
        out = tmp / "out" / "map.json"
        buf = io.StringIO()
        with mock.patch.object(sdm, "ROOT", tmp), \
             mock.patch.object(sys, "argv", ["sops_dependency_map.py", str(out)]), \
             redirect_stdout(buf):
            return sdm.main()

    def test_writes_json_and_returns_zero_on_healthy_repo(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_repo(tmp)
            rc = self.run_main(tmp)
            self.assertEqual(rc, 0)
            data = json.loads((tmp / "out" / "map.json").read_text())
            self.assertEqual(data["problems"], [])

    def test_returns_one_when_scan_finds_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            write(tmp, "plain.yaml", "a: 1\n")
            self.assertEqual(self.run_main(tmp), 1)


class TestRealRepo(unittest.TestCase):
    """今のリポジトリの地形。ここが壊れたら依存連鎖か走査のどちらかが変わった。"""

    @classmethod
    def setUpClass(cls):
        cls.map_data, cls.problems = sdm.build_map(sdm.ROOT)

    def test_current_repo_has_no_problems(self):
        self.assertEqual(self.problems, [], "\n".join(self.problems))

    def test_secrets_yaml_is_mapped(self):
        paths = [e["path"] for e in self.map_data["encrypted_files"]]
        self.assertIn("nix/images/proxmox-cloud/secrets.yaml", paths)

    def test_recipient_matches_sops_config_public_key(self):
        entry = next(
            e
            for e in self.map_data["encrypted_files"]
            if e["path"] == "nix/images/proxmox-cloud/secrets.yaml"
        )
        self.assertEqual(entry["keys_in_file"], ["doppler-token"])
        self.assertEqual(
            entry["recipients"],
            ["age1u55u5prakalcplze25mvkr98ura4r4paduqx52xed0c8gh69j5psfp9tek"],
        )
        self.assertEqual(
            entry["matched_creation_rule"]["age_recipients"],
            ["age1u55u5prakalcplze25mvkr98ura4r4paduqx52xed0c8gh69j5psfp9tek"],
        )

    def test_nix_consumer_is_found_and_ci_reference_is_zero(self):
        entry = next(
            e
            for e in self.map_data["encrypted_files"]
            if e["path"] == "nix/images/proxmox-cloud/secrets.yaml"
        )
        nix_paths = [r["path"] for r in entry["references"].get("nix", [])]
        self.assertIn("nix/images/proxmox-cloud/configuration.nix", nix_paths)
        self.assertNotIn("ci", entry["references"])

    def test_key_chain_points_to_cloud_init_and_keyfile(self):
        locs = self.map_data["key_locations"]
        self.assertIn(
            "terraform/proxmox/variables.tf",
            locs["private_key_variable"]["defined_in"],
        )
        mentioned = {m["path"] for m in locs["key_file"]["mentioned_in"]}
        self.assertIn("terraform/proxmox/vm-nixos.tf", mentioned)
        self.assertIn("nix/images/proxmox-cloud/configuration.nix", mentioned)
        self.assertEqual(locs["key_file"]["path_on_node"], "/var/lib/sops-nix/key.txt")

    def test_agent_environment_reports_presence_only(self):
        """env 変数の**値**は絶対に JSON に載らない。名前と在否のみ。"""
        cap = self.map_data["agent_environment"]
        self.assertIsInstance(cap["can_decrypt_now"], bool)
        self.assertTrue(all(isinstance(k, str) for k in cap["key_env_vars_present"]))


if __name__ == "__main__":
    unittest.main()
