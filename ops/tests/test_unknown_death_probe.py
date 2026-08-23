"""unknown 死の直後プローブと別カウンタの契約テスト (P-0141)。

なぜ要るか: opencode は HTTP 429 も鍵未設定も UnknownError に潰すため、
上限死が unknown に落ち、3 回連続 error 判定から stalled 化する経路が
実在した (substrate.md 2026-08-22 実測, 2026-08-08 の 26 セッション空費の再演条件)。

固定するもの:

  1. プローブの写像: HTTP 401 → auth / 429 → usage_limit / 接続不可 → network。
     それ以外は確定できないので None (unknown 維持 — 捏造しない)
  2. endpoint の導出: models.json 流 model 文字列の provider 部から。実測原本
     (fixtures/engine_stderr の error.data.metadata.url) との一致まで見る
  3. build_failure_info: unknown 死の直後だけプローブを打ち、既知死因に寄せられた
     分類を差し替える。substrate 実測文言 4 種 (UnknownError / Cannot connect /
     Invalid API key / 429) を fixture で網羅する
  4. parse_usage_limit_reset: opencode 形 best-effort 解析。取れない場合は
     None を正直に返す (実測 blob から時刻を捏造しない)
  5. worker ループ: unknown のまま残った死は既知死因とは**別カウンタ**で数え、
     閾値 (rules.runner.unknown_error_max_rounds) 超過で heart 既存配線
     (result state "error" → incident 型通知) に乗る

リポジトリルートから `python3 -m unittest ops.tests.test_unknown_death_probe`
(CI は discover -s ops/tests -t .)。HTTP 層・プローブは注入可能なので network フリー。
"""

import json
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ops.runner import runner as R
from ops.runner.tests.test_quota_flow import FAIL, FakeRunner, PASS, QuotaFlowTest
from ops.tests.test_failure_patterns import load_fixture, runner_blob

ROOT = Path(__file__).resolve().parent.parent.parent


# --- テスト用の fake ---

class FakeResponse:
    """urlopen が返す応答の最小模造品 (context manager)。"""

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


def counting_opener(status=None, exc=None):
    """1 呼び出しで status を返す / exc を送出する urlopen の替わり。"""
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        if exc is not None:
            raise exc
        return FakeResponse(status)

    opener.calls = calls
    return opener


# --- 写像と endpoint 導出 ---


class TestProbeMapping(unittest.TestCase):
    def test_status_to_kind_table(self):
        cases = [
            (401, "auth"),
            (429, "usage_limit"),
            # API には届いたが死因とは言えない応答 → 確定できない
            (200, None),
            (400, None),
            (403, None),
            (500, None),
            (503, None),
            (None, None),
        ]
        for status, want in cases:
            with self.subTest(status=status):
                self.assertEqual(R.probe_failure_kind(status), want)


class TestProbeEndpointDerivation(unittest.TestCase):
    def test_matches_the_observed_metadata_url(self):
        # 出典の実測 (fixture の error.data.metadata.url) と一致すること。
        # URL を捏造しないための固定
        fx = load_fixture("network_refused.txt")
        ev = json.loads(fx["stdout_lines"][0])
        observed = ev["error"]["data"]["metadata"]["url"]
        self.assertEqual(
            R.probe_endpoint("opencode-go/ox-alpha-free"), observed
        )
        self.assertIn("opencode.ai", observed)

    def test_unknown_or_claude_models_are_not_probed(self):
        self.assertIsNone(R.probe_endpoint("claude-sonnet-5"))
        self.assertIsNone(R.probe_endpoint(""))
        self.assertIsNone(R.probe_endpoint(None))
        self.assertIsNone(R.probe_endpoint("unknown-provider/m"))


class TestProbeInferenceApi(unittest.TestCase):
    MODEL = "opencode-go/ox-alpha-free"
    ENV = {"OPENCODE_API_KEY": "sk-test-key-000000"}

    def test_429_maps_to_usage_limit_with_one_request(self):
        opener = counting_opener(status=429)
        status, kind = R.probe_inference_api(
            self.MODEL, env=self.ENV, urlopen=opener
        )
        self.assertEqual((status, kind), (429, "usage_limit"))
        self.assertEqual(len(opener.calls), 1)  # spec: 1 リクエスト, リトライ無し

    def test_401_maps_to_auth_even_as_http_error(self):
        # urllib は 4xx を HTTPError として送出する。それも「API からの応答」
        opener = counting_opener(exc=urllib.error.HTTPError(
            "u", 401, "Unauthorized", {}, None
        ))
        status, kind = R.probe_inference_api(
            self.MODEL, env=self.ENV, urlopen=opener
        )
        self.assertEqual((status, kind), (401, "auth"))

    def test_connection_failure_maps_to_network(self):
        for exc in (
            urllib.error.URLError("refused"),
            OSError("timed out"),
        ):
            with self.subTest(exc=type(exc).__name__):
                opener = counting_opener(exc=exc)
                got = R.probe_inference_api(
                    self.MODEL, env=self.ENV, urlopen=opener
                )
                self.assertEqual(got, (None, "network"))

    def test_unexpected_exception_keeps_unknown(self):
        opener = counting_opener(exc=RuntimeError("boom"))
        self.assertEqual(
            R.probe_inference_api(self.MODEL, env=self.ENV, urlopen=opener),
            (None, None),
        )

    def test_no_key_or_unknown_provider_means_no_request(self):
        opener = counting_opener()
        self.assertEqual(
            R.probe_inference_api(self.MODEL, env={}, urlopen=opener),
            (None, None),
        )
        self.assertEqual(
            R.probe_inference_api(
                "claude-sonnet-5", env=self.ENV, urlopen=opener
            ),
            (None, None),
        )
        self.assertEqual(opener.calls, [])


# --- build_failure_info: 実測 fixture → プローブ → 分類差し替え ---


class TestBuildFailureInfo(unittest.TestCase):
    def probe_calls_recorder(self, result=(None, None)):
        calls = []

        def prober(model):
            calls.append(model)
            return result

        prober.calls = calls
        return prober

    def test_429_unknown_error_becomes_usage_limit_after_probe(self):
        # substrate 実測文言 1・2: UnknownError / "Unexpected server error."
        # (HTTP 429 が潰れた形)。プローブの 429 が usage_limit に寄せる
        fx = load_fixture("usage_limit_429_mocked.txt")
        ev = json.loads(fx["stdout_lines"][0])
        self.assertEqual(ev["error"]["name"], "UnknownError")
        blob = runner_blob(fx)
        self.assertIn("unexpected server error", blob.lower())
        prober = self.probe_calls_recorder(result=(429, "usage_limit"))
        info = R.build_failure_info(blob, "opencode-go/ox-alpha-free",
                                    prober=prober)
        self.assertEqual(info["failure_kind"], "usage_limit")
        self.assertEqual(info["probe_status"], "usage_limit")
        self.assertEqual(info["probe_http_status"], 429)
        self.assertEqual(prober.calls, ["opencode-go/ox-alpha-free"])

    def test_429_stays_unknown_when_probe_is_inconclusive(self):
        # **プローブ自体も失敗した場合のみ unknown を維持する**
        blob = runner_blob(load_fixture("usage_limit_429_mocked.txt"))
        prober = self.probe_calls_recorder(result=(503, None))
        info = R.build_failure_info(blob, "opencode-go/ox-alpha-free",
                                    prober=prober)
        self.assertEqual(info["failure_kind"], "unknown")
        self.assertIsNone(info["probe_status"])
        self.assertEqual(info["probe_http_status"], 503)

    def test_invalid_api_key_is_auth_without_probing(self):
        # substrate 実測文言 3: Invalid API key (statusCode 401)。分類が
        # 既に確定している回はプローブを打たない (1 リクエストの節約)
        blob = runner_blob(load_fixture("auth_invalid_api_key.txt"))
        self.assertIn("invalid api key", blob.lower())
        prober = self.probe_calls_recorder()
        info = R.build_failure_info(blob, "opencode-go/ox-alpha-free",
                                    prober=prober)
        self.assertEqual(info["failure_kind"], "auth")
        self.assertNotIn("probe_status", info)
        self.assertEqual(prober.calls, [])

    def test_cannot_connect_is_network_without_probing(self):
        # substrate 実測文言 4: Cannot connect to API ...
        blob = runner_blob(load_fixture("network_refused.txt"))
        self.assertIn("cannot connect to api", blob.lower())
        prober = self.probe_calls_recorder()
        info = R.build_failure_info(blob, "opencode-go/ox-alpha-free",
                                    prober=prober)
        self.assertEqual(info["failure_kind"], "network")
        self.assertNotIn("probe_status", info)
        self.assertEqual(prober.calls, [])

    def test_empty_blob_with_dead_network_probes_to_network(self):
        # 接続不可で即死した CLI は stderr も error イベントも空になりうる。
        # 分類は unknown → プローブの接続不可が network に寄せる
        prober = self.probe_calls_recorder(result=(None, "network"))
        info = R.build_failure_info("", "opencode-go/ox-alpha-free",
                                    prober=prober)
        self.assertEqual(info["failure_kind"], "network")
        self.assertEqual(info["probe_status"], "network")

    def test_session_timeout_does_not_probe(self):
        # timeout / 無活動 kill はエンジンの報告した死ではない。プローブで
        # 既知死因に寄せると待機や stalled 判定を誤らせる
        prober = self.probe_calls_recorder(result=(200, None))
        info = R.build_failure_info("", "opencode-go/ox-alpha-free",
                                    outcome="session_timeout", prober=prober)
        self.assertEqual(info["failure_kind"], "unknown")
        self.assertNotIn("probe_status", info)
        self.assertEqual(prober.calls, [])

    def test_claude_form_needs_no_probe_and_keeps_reset_parsing(self):
        prober = self.probe_calls_recorder()
        info = R.build_failure_info(
            "Claude AI usage limit reached|1754697600", "claude-sonnet-5",
            prober=prober,
        )
        self.assertEqual(info["failure_kind"], "usage_limit")
        self.assertIsNotNone(info["reset_at"])
        self.assertNotIn("probe_status", info)
        self.assertEqual(prober.calls, [])


# --- parse_usage_limit_reset: opencode 形 best-effort ---


class TestParseUsageLimitResetOpencodeForm(unittest.TestCase):
    def test_iso8601_near_reset_keyword(self):
        got = R.parse_usage_limit_reset(
            "Request rate limited. Resets at 2026-08-23T05:00:00Z."
        )
        self.assertEqual(got, datetime(2026, 8, 23, 5, 0, tzinfo=timezone.utc))

    def test_offset_aware_iso_is_normalized_to_utc(self):
        got = R.parse_usage_limit_reset(
            "rate limit exceeded; retry after 2026-08-23T14:00:00+09:00"
        )
        self.assertEqual(got, datetime(2026, 8, 23, 5, 0, tzinfo=timezone.utc))

    def test_epoch_and_millis_near_keyword(self):
        base = datetime(2026, 8, 23, 5, 0, tzinfo=timezone.utc)
        epoch = int(base.timestamp())
        self.assertEqual(
            R.parse_usage_limit_reset(f"resets {epoch}"), base
        )
        self.assertEqual(
            R.parse_usage_limit_reset(f"resets at {epoch * 1000} ms"), base
        )

    def test_honest_none_for_the_observed_unknown_blob(self):
        # **取れない場合は None を正直に返す。** 実測原本の UnknownError blob
        # から時刻を捏造しないことの固定
        blob = runner_blob(load_fixture("usage_limit_429_mocked.txt"))
        self.assertIsNone(R.parse_usage_limit_reset(blob))
        self.assertIsNone(R.parse_usage_limit_reset(""))
        self.assertIsNone(R.parse_usage_limit_reset(None))

    def test_numbers_without_keywords_are_not_times(self):
        # キーワード近傍以外の数字 (トークン数等) を拾わない
        self.assertIsNone(
            R.parse_usage_limit_reset("used 1790131200 tokens in total")
        )

    def test_claude_form_still_wins(self):
        got = R.parse_usage_limit_reset(
            "Claude AI usage limit reached|1754697600"
        )
        self.assertEqual(
            got, datetime.fromtimestamp(1754697600, tz=timezone.utc)
        )


# --- worker ループ: unknown 別カウンタ ---


class ProbeRunner(FakeRunner):
    """FakeRunner に rules の閾値と「プローブ済みの last_session 形」を足す。

    実 Session は unknown 死の後に必ずプローブを打つので、その結果
    (ここでは不確定 = probe_status None) が failure_fields に載る形を再現する。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rules["runner"].setdefault("unknown_error_max_rounds", 3)

    def run_session(self, prompt, tag, cwd=None):
        outcome = super().run_session(prompt, tag, cwd=cwd)
        if self.last_session.get("failure_kind") == "unknown":
            self.last_session["probe_status"] = None
            self.last_session["probe_http_status"] = None
        return outcome


class TestWorkerLoopUnknownCounter(QuotaFlowTest):
    def test_three_unknown_deaths_reach_humans_as_incident_evidence(self):
        r = ProbeRunner(
            self.tmp,
            outcomes=[("error", "unknown")] * 3,
            verify_seq=[FAIL],
            initialized=True,
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 1)
        doc = r.result_doc()
        # heart の既存配線: result state "error" → incident 型通知
        self.assertEqual(doc["state"], "error")
        self.assertIn("unknown 死", doc["error"])
        self.assertIn("プローブ", doc["error"])
        # プローブの証跡が result.json に乗る (DoD (1))
        self.assertIn("probe_status", doc)
        self.assertEqual(len(r.tags), 3)  # 閾値の回数だけ回して止まる

    def test_two_unknowns_then_success_recovers_without_stalling(self):
        r = ProbeRunner(
            self.tmp,
            outcomes=[("error", "unknown"), ("error", "unknown"),
                      ("completed", None)],
            verify_seq=[FAIL, FAIL, PASS],
            initialized=True,
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 0)
        self.assertEqual(r.result_doc()["state"], "ready_for_review")

    def test_unknowns_do_not_feed_the_known_error_counter(self):
        # 旧実装なら 3 回目の error で stalled 化していた列。unknown は
        # 別カウンタなので既知死因の連続には混ざらない
        r = ProbeRunner(
            self.tmp,
            outcomes=[("error", "unknown"), ("error", "unknown"),
                      ("error", "auth"), ("completed", None)],
            verify_seq=[FAIL, FAIL, FAIL, PASS],
            initialized=True,
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 0)
        self.assertEqual(r.result_doc()["state"], "ready_for_review")

    def test_known_errors_still_stall_after_three(self):
        r = ProbeRunner(
            self.tmp,
            outcomes=[("error", "auth")] * 3,
            verify_seq=[FAIL],
            initialized=True,
        )
        rc = r.mode_worker()
        self.assertEqual(rc, 1)
        doc = r.result_doc()
        self.assertEqual(doc["state"], "error")
        self.assertIn("3 回連続", doc["error"])

    def test_threshold_comes_from_rules_json_key(self):
        r = ProbeRunner(
            self.tmp,
            outcomes=[("error", "unknown")] * 2,
            verify_seq=[FAIL],
            initialized=True,
        )
        r.rules["runner"]["unknown_error_max_rounds"] = 2
        rc = r.mode_worker()
        self.assertEqual(rc, 1)
        self.assertEqual(len(r.tags), 2)
        self.assertIn("2 回連続", r.result_doc()["error"])


class TestRulesJsonThreshold(unittest.TestCase):
    def test_runner_has_positive_int_threshold(self):
        # 受入 verify 3 項目目と同じ契約。rules.json は運用パラメータの単一情報源
        with open(ROOT / "ops" / "rules.json") as f:
            rules = json.load(f)
        value = rules.get("runner", {}).get("unknown_error_max_rounds")
        self.assertIsInstance(value, int)
        self.assertGreaterEqual(value, 1)


if __name__ == "__main__":
    unittest.main()
