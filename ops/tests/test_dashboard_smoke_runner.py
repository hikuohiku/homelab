"""dashboard-smoke CronJob 埋め込みランナー (P-0193) の純関数を固定する。

ランナーは apps/ops-dashboard/dashboard-smoke-cronjob.yaml に埋め込まれており、
モジュールとして import すると ServiceAccount token 読みなどの import 副作用が
あるため cluster 外からはそのままロードできない。そこで
test_download_ledger_script.py と同じく YAML から実抽出したソースのうち、
副作用を持たない関数と定数だけを AST で取り出して名前空間に入れて試す。

このファイルが固定する契約:
- スモーク本体が --out に書いた結果 JSON をそのまま ConfigMap へ渡すこと。
  無い・壊れている・dict 以外は None になり、代役レコードへ切り替わる
- 代役レコード (rc=2 = 装置自身の故障) は「ページの嘘」と区別できる形であること:
  failed_checks は空のまま、tool_error / tool_error_rc に原因を載せる。
  stderr は 400 文字で切る (STDERR_TAIL_LIMIT)
- generated_at は run_smoke と同じ "%Y-%m-%dT%H:%M:%SZ" 書式 (reporter の鮮度判定が
  この書式しか解釈しないため)
"""

import ast
import datetime
import json
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_download_ledger_script_sync import extract_block_scalar  # noqa: E402

# ランナーが埋め込まれている manifest (extract_block_scalar は各 checker と共通の
# repo root 基準でパスを受ける)
RUNNER_YAML = "apps/ops-dashboard/dashboard-smoke-cronjob.yaml"

# 副作用の無い関数と、それらが参照するモジュールレベル定数だけを抜く。
# (SA token / namespace / SSL_CTX の読み込みは import 時副作用のため対象外)
FUNCTIONS = ("load_result", "fallback_result")
CONSTANTS = ("STDERR_TAIL_LIMIT",)


def load_functions():
    source = textwrap.dedent(extract_block_scalar(RUNNER_YAML, "dashboard_smoke_runner.py"))
    tree = ast.parse(source)
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            body.append(node)
        elif isinstance(node, ast.Assign):
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in CONSTANTS:
                body.append(node)
    missing = set(FUNCTIONS + CONSTANTS) - {
        n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in body
    }
    assert not missing, f"抽出に失敗: {sorted(missing)}"
    module = ast.Module(body=body, type_ignores=[])
    ns = {"datetime": datetime, "json": json}
    exec(compile(ast.fix_missing_locations(module), "<dashboard_smoke_runner>", "exec"), ns)
    return ns


runner = load_functions()


class LoadResultTest(unittest.TestCase):
    def test_valid_dict_passes_through(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "result.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ok": True, "checks": []}, f)
            self.assertEqual(
                runner["load_result"](path), {"ok": True, "checks": []}
            )

    def test_missing_file_is_none(self):
        self.assertIsNone(runner["load_result"]("/nonexistent/path/result.json"))

    def test_broken_json_is_none(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not-json")
            self.assertIsNone(runner["load_result"](path))

    def test_non_dict_json_is_none(self):
        # rc=1 経路で JSON は必ず dict だが、壊れた中身を黙って通さないための境界
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "list.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(["a"], f)
            self.assertIsNone(runner["load_result"](path))


class FallbackResultTest(unittest.TestCase):
    def test_shape_distinguishes_tool_error_from_page_lies(self):
        now = datetime.datetime(2026, 8, 23, 0, 40, 0, tzinfo=datetime.timezone.utc)
        out = runner["fallback_result"](2, "chromium を起動できない", now)
        self.assertEqual(out["schema"], 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["tool_error_rc"], 2)
        # 「ページの嘘」(failed_checks に不合格が載る) と区別するため空
        self.assertEqual(out["failed_checks"], [])
        self.assertIn("chromium", out["tool_error"])
        self.assertEqual(out["generated_at"], "2026-08-23T00:40:00Z")

    def test_stderr_tail_is_truncated_to_limit(self):
        out = runner["fallback_result"](1, "x" * 1000, datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual(len(out["tool_error"]), runner["STDERR_TAIL_LIMIT"])

    def test_empty_stderr_yields_empty_string_not_none(self):
        out = runner["fallback_result"](-9, "", datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual(out["tool_error"], "")

    def test_generated_at_format_matches_reporter_contract(self):
        out = runner["fallback_result"](2, "", datetime.datetime.now(datetime.timezone.utc))
        # reporter の _dashboard_smoke_summary が strptime できる書式であること
        datetime.datetime.strptime(out["generated_at"], "%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    unittest.main()
