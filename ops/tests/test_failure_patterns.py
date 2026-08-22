"""opencode CLI の死因分類を**実測フィクスチャ**で固定する (P-0101)。

なぜ要るか: ops/models.json は 2026-08-22 に全役を opencode-go へ切り替えたが、
FAILURE_PATTERNS の出典は claude CLI の既知の出力形を根拠にした候補で、opencode
の死因出力は 1 文字も観測されていなかった。上限死が unknown に落ちると 3 連続
error 判定から stalled 化する (2026-08-08 の「26 セッション空費」の再演条件)。

ops/runner/tests/test_exit_reason.py が「この文字列が来たらこう分類する」仕様表
(合成文字列) であるのに対し、このテストは **実測原本 → 分類器** の接続を見る:

  1. fixture (ops/tests/fixtures/engine_stderr/*.txt) は opencode CLI v1.18.21 を
     実際に壊して得た生の出力。収集手順は各ファイルのヘッダコメントに記録した
  2. 本番経路: stdout JSONL を consume_stream_event() に通して抽出した本文 +
     stderr を結合した blob (Session.run() と同じ構成) を classify_session_failure()
     に流し、期待死因に分類されること
  3. 生イベント丸ごとの blob でも期待死因が変わらないこと (CLI 出力経路の変化への
     保険)。ヘッダコメントは分類対象ではない

**実測で分からなかったものは unknown に落とす** (spec DoD「偽りの完全性を作らない」):

  - 鍵が env に無い故障と HTTP 429 は、どちらも UnknownError
    ("Unexpected server error.") に潰れ同一出力になる → auth / usage_limit には
    分類できない。CLI が出力形を変えるまで捏造したパターンは足さない
  - 本物の zen API の 429 応答は未観測 (ローカルモックによる CLI 出力形の実測まで)。
    上限で死んだ回の result.json `stderr_tail` を証拠に fixture・表・テストへ追記する

リポジトリルートから `python3 -m unittest ops.tests.test_failure_patterns`
(CI は discover -s ops/tests -t .)。
"""

import json
import re
import unittest
from pathlib import Path

from ops.runner.runner import (
    FAILURE_PATTERNS,
    classify_session_failure,
    consume_stream_event,
    parse_usage_limit_reset,
)

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = ROOT / "ops" / "tests" / "fixtures" / "engine_stderr"

# ファイル名 → 実測時の rc と期待死因。「unknown」は「分類不能」の実測結果であり、
# バグでも妥協でもない。将来 CLI が出力形を変えて復元できるようになったら、
# fixture を差し替えてここの期待値を上げる
EXPECTED = {
    "ok_success.txt": {"rc": 0, "expect": "unknown"},
    "auth_invalid_api_key.txt": {"rc": 1, "expect": "auth"},
    # 鍵が「誤っている」場合は 401 が出るが、「無い」場合は潰れて unknown
    "auth_key_missing.txt": {"rc": 1, "expect": "unknown"},
    # 接続拒否と DNS 失敗は同一文言を実測
    "network_refused.txt": {"rc": 1, "expect": "network"},
    "network_dns_failure.txt": {"rc": 1, "expect": "network"},
    # モックによる HTTP 429 は UnknownError に潰れる → usage_limit 復元不能
    "usage_limit_429_mocked.txt": {"rc": 1, "expect": "unknown"},
}


def load_fixture(name):
    """fixture ファイルをヘッダ meta / stdout 行列 / stderr に分解する。

    ヘッダ (# 行) は人間向けの記録であってエンジンの出力ではない — 分類入力には
    混ぜない。混ぜるとメタの語彙 (「rate limit」等) が分類を汚染する。
    """
    path = FIXTURES_DIR / name
    text = path.read_text()
    head, _, rest = text.partition("--- stdout ---")
    stdout_blob, _, stderr_blob = rest.partition("--- stderr ---")
    meta = {}
    for line in head.splitlines():
        if line.startswith("#"):
            key, sep, value = line.lstrip("#").strip().partition(":")
            if sep:
                meta[key.strip()] = value.strip()
    return {
        "meta": meta,
        "stdout_lines": [l for l in stdout_blob.splitlines() if l.strip()],
        "stderr": stderr_blob.strip(),
        "rc": int((meta.get("rc") or "?").split("/")[0].strip()),
        "expect": meta.get("expect_failure_kind"),
    }


def runner_blob(fixture):
    """Session.run() と同じ結合順の分類入力を作る。

    runner は `"".join(err_tail) + "\\n" + "\\n".join(result_errors)` を分類する。
    opencode 実測では stderr が常に空のため、実質 result_errors が本体。
    """
    result_errors = []
    usage = {"tokens": 0, "cost": 0.0}
    for line in fixture["stdout_lines"]:
        consume_stream_event(json.loads(line), usage, result_errors)
    err_tail_lines = [fixture["stderr"] + "\n"] if fixture["stderr"] else []
    return "".join(err_tail_lines) + "\n" + "\n".join(result_errors)


def full_output_blob(fixture):
    """stdout 生イベント行を全部足した blob。CLI の出力経路が変わっても
    死因文言自体は拾えることを確認するための太い入力。"""
    return (
        fixture["stderr"]
        + "\n"
        + "\n".join(fixture["stdout_lines"])
    )


class TestFixtures(unittest.TestCase):
    def test_at_least_four_fixtures_exist(self):
        files = sorted(FIXTURES_DIR.glob("*.txt"))
        self.assertGreaterEqual(len(files), 4)

    def test_every_fixture_is_declared_and_consistent(self):
        files = {p.name for p in FIXTURES_DIR.glob("*.txt")}
        self.assertEqual(set(EXPECTED), files)
        for name, want in EXPECTED.items():
            with self.subTest(name=name):
                fx = load_fixture(name)
                self.assertEqual(fx["expect"], want["expect"], "ヘッダと表の不一致")
                self.assertEqual(fx["rc"], want["rc"], "ヘッダと表の不一致")

    def test_headers_do_not_leak_classification_vocabulary(self):
        # メタコメントの語彙が分類を汚染しないための下敷き: ヘッダ単体は
        # failure に分類されないこと (fixture 全文を分類対象にしたくなる誘惑への保険)
        for name in EXPECTED:
            with self.subTest(name=name):
                fx = load_fixture(name)
                head_only = "\n".join(
                    f"{k}: {v}" for k, v in sorted(fx["meta"].items())
                )
                self.assertNotEqual(classify_session_failure(head_only), None)


class TestClassificationFromObservedOutput(unittest.TestCase):
    def test_runner_path_classifies_each_death(self):
        for name, want in EXPECTED.items():
            with self.subTest(name=name):
                got = classify_session_failure(runner_blob(load_fixture(name)))
                self.assertEqual(got, want["expect"])

    def test_full_output_blob_does_not_change_verdict(self):
        for name, want in EXPECTED.items():
            with self.subTest(name=name):
                got = classify_session_failure(full_output_blob(load_fixture(name)))
                self.assertEqual(got, want["expect"])

    def test_ok_session_yields_no_error_input(self):
        # 成功セッションは分類そのものが走らないが、うっかり result イベントや
        # 本文を拾って failure 扱いしないことの否対照
        fx = load_fixture("ok_success.txt")
        errors = []
        consume_stream_event(
            json.loads(fx["stdout_lines"][2]), {"tokens": 0, "cost": 0.0}, errors
        )
        self.assertEqual(errors, [])
        self.assertEqual(classify_session_failure(full_output_blob(fx)), "unknown")


class TestObservedShapes(unittest.TestCase):
    def _message(self, name):
        fx = load_fixture(name)
        ev = json.loads(fx["stdout_lines"][0])
        self.assertEqual(ev.get("type"), "error", f"{name}: 先頭イベントが type=error")
        self.assertTrue(fx["stderr"] == "", f"{name}: opencode の stderr は空のはず")
        return ev

    def test_invalid_key_is_apieror_with_401(self):
        ev = self._message("auth_invalid_api_key.txt")
        data = ev["error"]["data"]
        self.assertEqual(ev["error"]["name"], "APIError")
        self.assertEqual(data["statusCode"], 401)
        self.assertIn("invalid api key", data["message"].lower())

    def test_network_variants_share_one_message(self):
        # 接続拒否も DNS 失敗も同一文言に潰れる (v1.18.21 実測)。ネットワーク層の
        # 故障種別は出力から区別できない
        refused = self._message("network_refused.txt")
        dns = self._message("network_dns_failure.txt")
        self.assertEqual(refused["error"]["data"]["message"],
                         dns["error"]["data"]["message"])
        self.assertIn("cannot connect to api",
                      refused["error"]["data"]["message"].lower())

    def test_429_and_missing_key_are_indistinguishable(self):
        # どちらも UnknownError / "Unexpected server error." に潰れる。これが
        # usage_limit / auth 復元不能の根拠であり、パターンを捏造しない理由
        rate = self._message("usage_limit_429_mocked.txt")
        missing = self._message("auth_key_missing.txt")
        self.assertEqual(rate["error"]["name"], "UnknownError")
        self.assertEqual(rate["error"]["data"]["message"],
                         missing["error"]["data"]["message"])

    def test_opencode_error_message_reaches_classifier_via_consume(self):
        # consume_stream_event が error.data.message を分類入力に乗せること
        # (runner が拾えないと全死因が unknown に落ちる)
        fx = load_fixture("auth_invalid_api_key.txt")
        errors = []
        consume_stream_event(
            json.loads(fx["stdout_lines"][0]), {"tokens": 0, "cost": 0.0}, errors
        )
        self.assertEqual(errors, ["Invalid API key."])


class TestUnobservedStaysUnknown(unittest.TestCase):
    def test_unknown_error_shape_is_not_fabricated_into_kinds(self):
        blob = runner_blob(load_fixture("usage_limit_429_mocked.txt"))
        self.assertEqual(blob, "\nUnexpected server error. Check server logs "
                               "for details.")

    def test_claude_patterns_survive_for_rollback(self):
        # claude 用パターンは削除しない (models.json は PR 経由で戻せる)。
        # 温存していることが壊れていないかの最低限の確認
        self.assertEqual(
            classify_session_failure("Claude AI usage limit reached|1754697600"),
            "usage_limit",
        )
        self.assertIsNotNone(
            parse_usage_limit_reset("Claude AI usage limit reached|1754697600")
        )
        kinds = [kind for kind, _ in FAILURE_PATTERNS]
        self.assertEqual(kinds, ["usage_limit", "auth", "network"])

    def test_observed_patterns_are_present_in_table(self):
        # 実測で分類できた死因の文言が表に入っていること (表だけ直して fixture を
        # 更新し忘れる、またはその逆の片肺を防ぐ)
        network_patterns = dict(FAILURE_PATTERNS)["network"]
        joined = "|".join(network_patterns)
        message = json.loads(
            load_fixture("network_refused.txt")["stdout_lines"][0]
        )["error"]["data"]["message"].lower()
        matched = [p for p in network_patterns if re.search(p, message)]
        self.assertTrue(matched, f"実測文言 {message!r} に一致するパターンが無い")
        self.assertIn(matched[0], network_patterns)
        self.assertIn("cannot connect to api", joined)


if __name__ == "__main__":
    unittest.main()
