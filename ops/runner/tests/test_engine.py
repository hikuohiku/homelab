"""思考エンジンの選択とイベント解釈の契約テスト (2026-08-22 opencode go 移行)。

opencode の JSON イベント形は v1.18.21 の実測 (ox-alpha-free への
`opencode run --format json` の出力) を根拠にしている。CLI 更新で形が変わったら
まず実測し、この表と consume_stream_event を併せて更新すること。
"""

import unittest

from ops.runner.runner import build_session_cmd, consume_stream_event


class TestBuildSessionCmd(unittest.TestCase):
    def test_provider_slash_model_uses_opencode(self):
        cmd = build_session_cmd("opencode-go/ox-alpha-free", "やって")
        self.assertEqual(cmd[0], "opencode")
        self.assertIn("--format", cmd)
        self.assertIn("json", cmd)
        self.assertIn("opencode-go/ox-alpha-free", cmd)
        self.assertEqual(cmd[-1], "やって")

    def test_plain_model_uses_claude(self):
        cmd = build_session_cmd("claude-sonnet-5", "やって")
        self.assertEqual(cmd[0], "claude")
        self.assertIn("--permission-mode", cmd)
        self.assertEqual(cmd[-1], "やって")


class TestConsumeStreamEvent(unittest.TestCase):
    def test_claude_result_event(self):
        usage = {"tokens": 0, "cost": 0.0}
        errs = []
        consume_stream_event(
            {"type": "result", "total_cost_usd": 0.5,
             "usage": {"input_tokens": 100, "output_tokens": 20}},
            usage, errs,
        )
        self.assertEqual(usage["tokens"], 120)
        self.assertAlmostEqual(usage["cost"], 0.5)
        self.assertEqual(errs, [])

    def test_claude_error_result_collects_text(self):
        usage = {"tokens": 0, "cost": 0.0}
        errs = []
        consume_stream_event(
            {"type": "result", "is_error": True, "subtype": "error_during_execution",
             "result": "Claude AI usage limit reached|1754800000"},
            usage, errs,
        )
        self.assertIn("Claude AI usage limit reached|1754800000", errs)

    def test_opencode_step_finish_event(self):
        """v1.18.21 実測形: part.tokens {total,input,output,reasoning,cache} / part.cost。"""
        usage = {"tokens": 0, "cost": 0.0}
        errs = []
        consume_stream_event(
            {"type": "step_finish", "part": {
                "type": "step-finish", "cost": 0,
                "tokens": {"total": 8745, "input": 8718, "output": 19,
                           "reasoning": 8, "cache": {"write": 0, "read": 0}}}},
            usage, errs,
        )
        self.assertEqual(usage["tokens"], 8737)
        self.assertEqual(errs, [])

    def test_opencode_error_event_collects_message(self):
        """v1.18.21 実測形: {type: error, error: {name, data: {message}}}。"""
        usage = {"tokens": 0, "cost": 0.0}
        errs = []
        consume_stream_event(
            {"type": "error", "error": {"name": "UnknownError",
             "data": {"message": "Unexpected server error."}}},
            usage, errs,
        )
        self.assertEqual(errs, ["Unexpected server error."])

    def test_unknown_event_is_ignored(self):
        usage = {"tokens": 0, "cost": 0.0}
        errs = []
        consume_stream_event({"type": "text", "part": {"text": "hi"}}, usage, errs)
        self.assertEqual(usage, {"tokens": 0, "cost": 0.0})
        self.assertEqual(errs, [])


class TestSetupOpencode(unittest.TestCase):
    def test_writes_external_directory_allow(self):
        """cwd 外 I/O の許可設定が無いと reviewer/curriculum/critic の /data 契約が
        全滅する (2026-08-22 事故)。設定ファイルの中身を契約として固定する。"""
        import json as _json
        import os
        import tempfile
        from unittest import mock

        from ops.runner import runner as r

        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                os.environ.pop("XDG_CONFIG_HOME", None)
                inst = object.__new__(r.Runner)  # __init__ は I/O だらけなので回避
                inst.setup_opencode()
                cfg = _json.loads(
                    open(f"{home}/.config/opencode/opencode.json").read()
                )
        self.assertEqual(cfg["permission"]["external_directory"], "allow")


if __name__ == "__main__":
    unittest.main()
