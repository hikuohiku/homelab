"""常駐コア (apps/autopilot-core) の opencode permission を機械で縛る。

なぜ要るか: opencode の permission は **マッチするルールが無いと既定が `ask`**。
誰も答えられない環境で `ask` を踏むと、コアはそのまま応答待ちで固まる
(CHARTER §5.1 — run #1 が同型の事故で丸ごと消えた)。
「`ask` を書かない」だけでは足りず、**書き漏らしが `ask` になる**のがこの穴の本体。
だからツール種別の網羅を検査する。

検査するもの:
  1. permission のどこにも `ask` が無い
  2. 指定可能なツール種別が全部明示されている (書き漏らし = 暗黙の ask)
  3. `edit` は deny のまま (設計 D30: コアは git に書かない)
  4. `bash` は既定 deny のパターン表で、ブランケット allow が無い
  5. MCP は remote かつ `oauth: false` (未指定だと OAuth 自動検出が走る)

リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`。
"""

import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / "apps" / "autopilot-core" / "config.yaml"

# opencode が permission のキーとして解釈する種別 (2026-08-24 実測)。
# opencode を上げてキーが増えたら、ここへ足すと同時に config.yaml も埋めること。
# 埋めなかったものは暗黙に ask になる。
KNOWN_TOOLS = frozenset(
    {
        "read",
        "edit",
        "glob",
        "grep",
        "list",
        "bash",
        "task",
        "external_directory",
        "todowrite",
        "question",
        "webfetch",
        "websearch",
        "lsp",
        "doom_loop",
        "skill",
    }
)


def load_opencode_config() -> dict:
    doc = yaml.safe_load(CONFIG.read_text())
    return json.loads(doc["data"]["opencode.json"])


def actions(value) -> list[str]:
    """permission の値 (文字列 or パターン表) から動作の一覧を返す。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [v for inner in value.values() for v in actions(inner)]
    raise AssertionError(f"permission の値が文字列でも表でもない: {value!r}")


class CorePermissions(unittest.TestCase):
    def setUp(self):
        self.cfg = load_opencode_config()
        self.perm = self.cfg["permission"]

    def test_no_ask_anywhere(self):
        for tool, value in self.perm.items():
            for action in actions(value):
                self.assertNotEqual(
                    action,
                    "ask",
                    f"{tool} に ask がある。誰も答えられないので deny にすること (CHARTER §5.1)",
                )

    def test_every_known_tool_is_explicit(self):
        missing = sorted(KNOWN_TOOLS - set(self.perm))
        self.assertFalse(
            missing,
            f"permission に書かれていないツールがある: {missing}。"
            "マッチするルールが無いと既定は ask なので、allow か deny を明示すること",
        )

    def test_only_allow_or_deny(self):
        for tool, value in self.perm.items():
            for action in actions(value):
                self.assertIn(action, ("allow", "deny"), f"{tool}: 未知の動作 {action!r}")

    def test_edit_stays_denied(self):
        # 設計 D30。コアは git に書かない。実装は heart の担当
        self.assertEqual(self.perm["edit"], "deny")

    def test_bash_is_a_deny_by_default_pattern_table(self):
        bash = self.perm["bash"]
        self.assertIsInstance(bash, dict, "bash はコマンド単位のパターン表にすること")
        self.assertEqual(
            bash.get("*"), "deny", "bash の既定は deny。列挙した形だけを allow する"
        )
        allowed = sorted(k for k, v in bash.items() if v == "allow")
        self.assertTrue(allowed, "何も allow されていないなら bash ごと deny にすべき")
        for pattern in allowed:
            self.assertNotEqual(pattern, "*", "ブランケット allow は置かない")
            # 履歴を読むための git と date だけ。ファイルの中身は read/grep で読める
            self.assertTrue(
                pattern.startswith("git -C /data/repo ") or pattern.startswith("date"),
                f"想定外の allow パターン: {pattern!r}。"
                "作業コピーを指した git か date 以外を足すときは、ここも一緒に広げること",
            )

    def test_mcp_servers_are_remote_without_oauth(self):
        mcp = self.cfg["mcp"]
        self.assertTrue(mcp, "MCP が 1 つも無い")
        for name, spec in mcp.items():
            self.assertEqual(spec["type"], "remote", f"{name}: 秘密をコアの env に戻さない")
            self.assertIs(
                spec.get("oauth"),
                False,
                f"{name}: oauth を明示しないと OAuth 自動検出が走る",
            )
            self.assertNotIn(
                "headers", spec, f"{name}: headers は GET /config でコアに丸見えになる"
            )


class PermissionHelpers(unittest.TestCase):
    """判定そのものが緩んでいないことを合成入力で固定する。
    実 config だけを見るテストは「今たまたま通っている」と「正しい」を区別できない。"""

    def test_actions_flattens_pattern_tables(self):
        self.assertEqual(actions("deny"), ["deny"])
        self.assertEqual(sorted(actions({"*": "deny", "git log*": "allow"})), ["allow", "deny"])

    def test_nested_ask_would_be_caught(self):
        self.assertIn("ask", actions({"*": "deny", "kubectl*": "ask"}))


if __name__ == "__main__":
    unittest.main()
