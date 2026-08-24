"""常駐コア (apps/autopilot-core) の opencode permission を機械で縛る。

なぜ要るか: opencode の permission は **マッチするルールが無いと既定が `ask`**。
誰も答えられない環境で `ask` を踏むと、そのセッションは応答待ちのまま固まる
(CHARTER §5.1 — run #1 が同型の事故で丸ごと消えた)。
「`ask` を書かない」だけでは足りず、**書き漏らしが `ask` になる**のがこの穴の本体。
だからグローバルにも、エージェントごとにも、ツール種別の網羅を検査する。

検査するもの:
  1. permission のどこにも `ask` が無い (グローバル + 各エージェント)
  2. グローバルが、指定可能なツール種別を全部明示している
  3. **各エージェント**も、指定可能なツール種別を全部明示している
     (エージェント固有のルールはグローバルに後勝ちで重なるだけなので、
      グローバルに書いていない種別は暗黙の ask のまま残る)
  4. `edit` は全員 deny (設計 D30: コアは git に書かない)
  5. `bash` は既定 deny のパターン表で、ブランケット allow が無い
  6. 立案の shadow 実行 (Phase C) の役が、副作用を持てない形になっている
  7. planner / judge のモデルが ops/models.json の curriculum 各役と一致する
  8. MCP は remote かつ `oauth: false` (未指定だと OAuth 自動検出が走る)
  9. 指示書 (AGENTS.md) が dispatch_task を実装依頼の第一手として書き、
     request_task を heart 不達時の冷スペアとして残している

リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`。
"""

import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / "apps" / "autopilot-core" / "config.yaml"
MODELS = ROOT / "ops" / "models.json"

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

# 立案の shadow 実行に使う役 (config.yaml の agent)。
SHADOW_AGENTS = ("shadow", "planner", "judge")


def load_opencode_config() -> dict:
    doc = yaml.safe_load(CONFIG.read_text())
    return json.loads(doc["data"]["opencode.json"])


def load_agents_md() -> str:
    return yaml.safe_load(CONFIG.read_text())["data"]["AGENTS.md"]


def actions(value) -> list[str]:
    """permission の値 (文字列 or パターン表) から動作の一覧を返す。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [v for inner in value.values() for v in actions(inner)]
    raise AssertionError(f"permission の値が文字列でも表でもない: {value!r}")


def all_permissions(cfg: dict) -> dict[str, dict]:
    """{名前: permission 表} を返す。グローバルは "(global)"。"""
    out = {"(global)": cfg["permission"]}
    for name, agent in cfg.get("agent", {}).items():
        if "permission" in agent:
            out[name] = agent["permission"]
    return out


class CorePermissions(unittest.TestCase):
    def setUp(self):
        self.cfg = load_opencode_config()
        self.perm = self.cfg["permission"]

    def test_no_ask_anywhere(self):
        for owner, perm in all_permissions(self.cfg).items():
            for tool, value in perm.items():
                for action in actions(value):
                    self.assertNotEqual(
                        action,
                        "ask",
                        f"{owner} の {tool} に ask がある。"
                        "誰も答えられないので deny にすること (CHARTER §5.1)",
                    )

    def test_only_allow_or_deny(self):
        for owner, perm in all_permissions(self.cfg).items():
            for tool, value in perm.items():
                for action in actions(value):
                    self.assertIn(
                        action, ("allow", "deny"), f"{owner} の {tool}: 未知の動作 {action!r}"
                    )

    def test_every_known_tool_is_explicit(self):
        # グローバル。エージェントを名指ししない既定のセッションがここで縛られる
        missing = sorted(KNOWN_TOOLS - set(self.perm))
        self.assertFalse(
            missing,
            f"permission に書かれていないツールがある: {missing}。"
            "マッチするルールが無いと既定は ask なので、allow か deny を明示すること",
        )

    def test_every_agent_enumerates_every_known_tool(self):
        # エージェント固有のルールはグローバルに後勝ちで重なる。グローバルにも
        # エージェントにも無い種別は、そのエージェントでは暗黙の ask になる
        for name in SHADOW_AGENTS:
            perm = self.cfg["agent"][name]["permission"]
            missing = sorted(KNOWN_TOOLS - set(perm))
            self.assertFalse(
                missing,
                f"agent {name} の permission に書かれていないツールがある: {missing}。"
                "マッチするルールが無いと既定は ask なので、allow か deny を明示すること",
            )

    def test_edit_stays_denied(self):
        # 設計 D30。コアは git に書かない。実装は heart の担当
        self.assertEqual(self.perm["edit"], "deny")

    def test_edit_stays_denied_everywhere(self):
        # サブエージェントも同じ。ここが緩むと shadow 実行に副作用が生える
        for owner, perm in all_permissions(self.cfg).items():
            if "edit" in perm:
                self.assertEqual(perm["edit"], "deny", f"{owner}: edit は deny のまま")

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

    def test_shadow_agent_cannot_do_anything_but_delegate(self):
        # subtask を撃ち込む受け皿。ここが何か呼べると shadow 実行に副作用が生える
        agent = self.cfg["agent"]["shadow"]
        self.assertEqual(agent["mode"], "primary", "driver が名指しで使う受け皿は primary")
        perm = agent["permission"]
        self.assertEqual(perm["task"], "allow", "subtask を回すのが唯一の仕事")
        for tool, value in perm.items():
            if tool == "task":
                continue
            self.assertEqual(value, "deny", f"shadow の {tool} は deny であるべき")
        tools = agent.get("tools", {})
        self.assertIs(tools.get("*"), False, "既定で全ツールを閉じること (MCP ツールを含む)")
        self.assertIs(tools.get("task"), True, "task だけを開けること")

    def test_planner_and_judge_are_subagents_without_hands(self):
        for name in ("planner", "judge"):
            agent = self.cfg["agent"][name]
            self.assertEqual(agent["mode"], "subagent", f"{name}: 親と別セッションで走らせる")
            perm = agent["permission"]
            # 手は持たせない。立案は読んで考えるだけの仕事
            for tool in ("edit", "bash", "task"):
                self.assertEqual(perm[tool], "deny", f"{name}: {tool} は deny")
            # 人間への問い合わせは headless では答える者が居ない = ask と同じ死に方
            self.assertEqual(perm["question"], "deny", f"{name}: question は deny")

    def test_planner_and_judge_models_match_models_json(self):
        roles = json.loads(MODELS.read_text())["roles"]
        for name, role in (("planner", "curriculum_generate"), ("judge", "curriculum_judge")):
            self.assertEqual(
                self.cfg["agent"][name]["model"],
                roles[role],
                f"{name} のモデルは ops/models.json の {role} と揃えること",
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


class CuriculumPromptsStayInSync(unittest.TestCase):
    """サブエージェントの指示が Job 版の判断基準を落としていないこと。

    Phase C の shadow 実行は「Job 版と突き合わせる」ためにある。判断基準が
    ずれていたら差分の意味が消えるので、外せない要素だけを機械で押さえる。
    """

    def setUp(self):
        self.agents = load_opencode_config()["agent"]

    def test_planner_keeps_the_generation_rules(self):
        prompt = self.agents["planner"]["prompt"]
        for needle in (
            "5〜10 案",
            "cell",
            "verify",
            "archive.jsonl",
            "reject_reason",
            "proposed_by",
            "request_id",
            "1/4 以上",
        ):
            self.assertIn(needle, prompt, f"生成役の指示から {needle} が落ちている")

    def test_judge_keeps_the_fixed_rubric(self):
        prompt = self.agents["judge"]["prompt"]
        for needle in (
            "固定ルーブリック",
            "interestingly new",
            "VISION",
            "検証可能性",
            "セル多様性",
            "human-request",
            "reject_reason",
            "失格条件",
            "adopted",
        ):
            self.assertIn(needle, prompt, f"判定役の指示から {needle} が落ちている")


class CoreDispatchPolicy(unittest.TestCase):
    """コアの指示書 (AGENTS.md) が dispatch_task を第一手として書いていること。

    2026-08-24 の実測: 所有者が Telegram で頼んだ 2 件の実装が request_task で
    起票され、その回の curriculum Job は既に起動済みだったため、次の立案ラウンド
    待ちになった。dispatch_task は curriculum を経由しないので、使っていれば
    その待ちは無かった。器 (ツール) はあっても指示書が知らなければ使われないので、
    ここで固定して静かな先祖返りを防ぐ。
    """

    def setUp(self):
        self.doc = load_agents_md()

    def test_dispatch_task_is_the_first_move(self):
        self.assertIn("dispatch_task", self.doc, "指示書が dispatch_task を知らない")
        self.assertIn(
            "第一手", self.doc, "実装依頼の第一手が dispatch_task だと書かれていない"
        )

    def test_request_task_survives_as_the_fallback(self):
        # heart が落ちている経路。消すと依頼そのものが落ちる
        self.assertIn("request_task", self.doc, "冷スペアの request_task が消えている")
        self.assertIn(
            "届かなかったときだけ",
            self.doc,
            "request_task へ落とす条件 (heart に届かなかったときだけ) が書かれていない",
        )

    def test_denial_reasons_are_relayed_verbatim(self):
        # 拒否理由は heart から人語で返る。名前で書いておかないと言い換えて薄まる
        for reason in (
            "stop_engaged",
            "capacity",
            "rate_limited",
            "capability_not_declared",
            "state_stale",
            "heart_not_ready",
            "shadow_mode",
            "invalid",
            "duplicate",
        ):
            self.assertIn(reason, self.doc, f"拒否理由 {reason} の扱いが書かれていない")
        self.assertIn("そのまま所有者に伝える", self.doc, "理由をそのまま伝える指示が無い")

    def test_verify_and_project_id_are_taught(self):
        self.assertIn("verify", self.doc, "verify の書き方が書かれていない")
        self.assertIn("完成したら pass", self.doc, "verify の受入基準の条件が書かれていない")
        self.assertIn("P-NNNN", self.doc, "受理時にプロジェクト ID を伝える指示が無い")

    def test_curriculum_bypass_is_explicit(self):
        # ルーブリックを通らない経路なので、使ってよい範囲を明記しておく
        self.assertIn("curriculum", self.doc, "curriculum を経由しない旨が書かれていない")
        self.assertIn(
            "明示的に頼んだものだけ", self.doc, "dispatch_task の適用範囲が書かれていない"
        )

    def test_no_promise_of_doing_it_yourself(self):
        # 既存の原則。コアは実装しない
        self.assertIn("やっておきます", self.doc, "「やっておきます」と言わない原則が消えている")
        self.assertIn("できたことにしない", self.doc, "「できたことにしない」原則が消えている")


class PermissionHelpers(unittest.TestCase):
    """判定そのものが緩んでいないことを合成入力で固定する。
    実 config だけを見るテストは「今たまたま通っている」と「正しい」を区別できない。"""

    def test_actions_flattens_pattern_tables(self):
        self.assertEqual(actions("deny"), ["deny"])
        self.assertEqual(sorted(actions({"*": "deny", "git log*": "allow"})), ["allow", "deny"])

    def test_nested_ask_would_be_caught(self):
        self.assertIn("ask", actions({"*": "deny", "kubectl*": "ask"}))

    def test_all_permissions_includes_agents(self):
        cfg = {"permission": {"edit": "deny"}, "agent": {"x": {"permission": {"read": "allow"}}}}
        self.assertEqual(sorted(all_permissions(cfg)), ["(global)", "x"])


if __name__ == "__main__":
    unittest.main()
