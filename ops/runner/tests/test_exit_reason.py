"""セッションの死因分類の契約テスト (P-0026)。

この表が仕様。「この文字列が来たらこう分類する」を宣言している。

**上限の実文字列はまだ観測できていない** — runner が stderr を DEVNULL で捨てて
きたのが P-0026 の発端で、journal にも残っていない。下の文字列は claude CLI の
既知の出力形を根拠にした候補である。実際に別の文言を観測したら、その回の
result.json の `stderr_tail` を証拠にここと FAILURE_PATTERNS へ追記する。
"""

import unittest
from datetime import datetime, timezone

from ops.runner.runner import (
    FAILURE_PATTERNS,
    classify_session_failure,
    mask_secrets,
    parse_usage_limit_reset,
)

USAGE_LIMIT = [
    "Claude AI usage limit reached",
    "Claude AI usage limit reached|1754697600",
    "Error: usage limit exceeded for this account",
    "You've hit your 5-hour limit",
    "limit reached ∙ resets 3pm",
    '{"type":"error","error":{"type":"rate_limit_error","message":"..."}}',
    "API Error: 429 rate limit exceeded",
    "rate limit hit (status 429)",
]

AUTH = [
    "Invalid API key · Please run /login",
    '{"type":"error","error":{"type":"authentication_error"}}',
    "OAuth token has expired",
    "API Error: 401 Unauthorized",
    "API Error: 403 Forbidden",
]

NETWORK = [
    "getaddrinfo ENOTFOUND api.anthropic.com",
    "connect ECONNREFUSED 127.0.0.1:443",
    "connect ETIMEDOUT 160.79.104.10:443",
    "read ECONNRESET",
    "Error: socket hang up",
    "TypeError: fetch failed",
]

UNKNOWN = [
    "",
    "   \n  ",
    "something else entirely",
    "Traceback (most recent call last): KeyError: 'foo'",
    "error: pathspec 'origin/main' did not match any file(s) known to git",
]


class TestClassify(unittest.TestCase):
    def test_usage_limit(self):
        for text in USAGE_LIMIT:
            with self.subTest(text=text):
                self.assertEqual(classify_session_failure(text), "usage_limit")

    def test_auth(self):
        for text in AUTH:
            with self.subTest(text=text):
                self.assertEqual(classify_session_failure(text), "auth")

    def test_network(self):
        for text in NETWORK:
            with self.subTest(text=text):
                self.assertEqual(classify_session_failure(text), "network")

    def test_unknown_is_the_default(self):
        # 知らない文字列を勝手に分類しない。ここが緩むと「上限だから待つ」が
        # 本物の詰まりにも適用され、ループが空回りする
        for text in UNKNOWN:
            with self.subTest(text=text):
                self.assertEqual(classify_session_failure(text), "unknown")

    def test_none_is_unknown(self):
        self.assertEqual(classify_session_failure(None), "unknown")

    def test_usage_limit_wins_over_auth_and_network(self):
        # 文言が重なる回 (429 と 401 が同じログに出る等) は上限に寄せる。
        # 判定順 usage_limit > auth > network が仕様
        mixed = (
            "getaddrinfo ENOTFOUND proxy\n"
            "API Error: 401 Unauthorized\n"
            "Claude AI usage limit reached|1754697600\n"
        )
        self.assertEqual(classify_session_failure(mixed), "usage_limit")

    def test_auth_wins_over_network(self):
        mixed = "fetch failed\nInvalid API key\n"
        self.assertEqual(classify_session_failure(mixed), "auth")

    def test_multiline_tail_is_classified_by_any_line(self):
        tail = "\n".join(["warning: something", "", "Claude AI usage limit reached"])
        self.assertEqual(classify_session_failure(tail), "usage_limit")

    def test_pattern_table_kinds_are_the_declared_four(self):
        kinds = [kind for kind, _ in FAILURE_PATTERNS]
        self.assertEqual(kinds, ["usage_limit", "auth", "network"])


class TestParseReset(unittest.TestCase):
    def test_epoch_seconds(self):
        got = parse_usage_limit_reset("Claude AI usage limit reached|1754697600")
        self.assertEqual(got, datetime(2025, 8, 9, 0, 0, tzinfo=timezone.utc))

    def test_epoch_millis(self):
        got = parse_usage_limit_reset("Claude AI usage limit reached|1754697600000")
        self.assertEqual(got, datetime(2025, 8, 9, 0, 0, tzinfo=timezone.utc))

    def test_missing_reset_is_none(self):
        # reset が取れない = 既定の待機時間に落とす (呼び出し側の責務)
        self.assertIsNone(parse_usage_limit_reset("Claude AI usage limit reached"))
        self.assertIsNone(parse_usage_limit_reset("something else entirely"))
        self.assertIsNone(parse_usage_limit_reset(""))
        self.assertIsNone(parse_usage_limit_reset(None))


class TestMaskSecrets(unittest.TestCase):
    def test_env_literal_values_are_masked(self):
        env = {"AUTOPILOT_GITHUB_TOKEN": "s3cr3t-token-value", "GITHUB_TOKEN": ""}
        got = mask_secrets("fatal: could not read s3cr3t-token-value from remote", env)
        self.assertNotIn("s3cr3t-token-value", got)
        self.assertIn("***", got)

    def test_short_env_values_are_not_used_as_patterns(self):
        # 空や極端に短い値で全文が潰れないこと
        env = {"AUTOPILOT_GITHUB_TOKEN": "", "CLAUDE_CODE_OAUTH_TOKEN": "abc"}
        self.assertEqual(mask_secrets("abc def", env), "abc def")

    def test_token_shapes_are_masked(self):
        env = {}
        cases = [
            "remote: ghp_ABCdef0123456789ABCdef",
            "token github_pat_11ABCDEFG0abcdefghijklmn",
            "x-api-key: sk-ant-api03-abcdefghijklmno",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6",
            "https://x-access-token:ghp_ABCdef0123456789@github.com/o/r",
        ]
        for text in cases:
            with self.subTest(text=text):
                got = mask_secrets(text, env)
                self.assertIn("***", got)
                for leak in ("ghp_ABCdef0123456789", "github_pat_11ABCDEFG0",
                             "sk-ant-api03-abcdefghijklmno", "eyJhbGciOiJIUzI1NiIs"):
                    self.assertNotIn(leak, got)

    def test_plain_text_is_untouched(self):
        self.assertEqual(
            mask_secrets("Claude AI usage limit reached", {}),
            "Claude AI usage limit reached",
        )

    def test_empty(self):
        self.assertEqual(mask_secrets("", {}), "")
        self.assertEqual(mask_secrets(None, {}), "")

    def test_masked_text_still_classifies(self):
        # マスクは 2000 文字に切る前に掛かる。潰した後も死因が読めること
        env = {"AUTOPILOT_GITHUB_TOKEN": "s3cr3t-token-value"}
        tail = "s3cr3t-token-value\nClaude AI usage limit reached|1754697600\n"
        masked = mask_secrets(tail, env)
        self.assertEqual(classify_session_failure(masked), "usage_limit")
        self.assertIsNotNone(parse_usage_limit_reset(masked))


if __name__ == "__main__":
    unittest.main()
