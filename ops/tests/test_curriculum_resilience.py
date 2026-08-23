"""curriculum Job の「黙って死ぬ」経路への構造的な歯止めを固定する (P-0227)。

2026-08-23 の health 実測で curriculum Job の Pod 3 本が Failed。検死
(ops/projects/logs/P-0227/failures.md) で断定した死因は **auth** — opencode zen
が瞬間的に 401 を返す窓でエンジンセッションが死に、runner の mode_curriculum
には再試行が無いので 1 回の死が Job 全体 (backoffLimit: 0) の Failed になって
いた。judge フェーズの死は生成済み proposals.json (20〜30 万トークン) を
道連れにしていた。

このテストは 2 つを固定する:

  1. **死因分類の対応表** (DoD 2)。実測の type=error イベント (raw transcript
     より引用。文言は捏造しない) が classify_session_failure 単体では unknown
     に落ち、直後の API プローブ 401 によって auth に寄せられること —
     これが failures.md の `root_cause: auth` の根拠チェーン全体。
     6 種のうち usage_limit / auth / network は分類器とプローブ写像、
     timeout は outcome=session_timeout、budget は state=budget_exhausted と
     別層で表現される (PROJECT.md 前提)。unknown は最後の砦として残る
  2. **歯止めの発火条件** (DoD 3/4)。curriculum_next_action() の 4 値
     ('done' | 'retry' | 'quota_wait' | 'give_up') マッピング。usage_limit は
     何回でも quota_wait (worker と同じ P-0026 流儀)、それ以外の非 completed
     は CURRICULUM_MAX_CONSECUTIVE_ERRORS 回まで有界リトライ、
     「completed なのに産物無し」は高価なので再試行しない

リポジトリルートから `python3 -m unittest ops.tests.test_curriculum_resilience`
(CI は discover -s ops/tests -t .)。
"""

import unittest

from ops.runner.runner import (
    CURRICULUM_MAX_CONSECUTIVE_ERRORS,
    build_failure_info,
    classify_session_failure,
    curriculum_next_action,
    probe_failure_kind,
)

# 2026-08-23T17:19Z 死亡セッション (cur-judge) の生 transcript から引用した
# type=error イベント 1 行。原本: ops/projects/logs/P-0227/
# raw-transcript-20260823T171755-cur-judge.jsonl (opencode CLI v1.18.21 実測)。
# 直前の step_start 6 回が ~75 秒の指数退避リトライ (=opencode 自前の再試算)。
MEASURED_ERROR_EVENT = (
    '{"type":"error","timestamp":1787505559690,'
    '"sessionID":"ses_fd05ef2feffes266FKEFvuttg5",'
    '"error":{"name":"APIError",'
    '"data":{"message":"Provider finish_reason: network_error",'
    '"isRetryable":true,"metadata":{"code":"ProviderResponseStreamError"}}}}'
)
# 同死亡 Job の result.json に記録された分類入力 (stderr_tail 実測値)
MEASURED_STDERR_TAIL = "\nProvider finish_reason: network_error"


def fake_prober(status, kind):
    """build_failure_info の prober 注入点。実測 (401 → auth) を模する。"""
    calls = []

    def probe(model):
        calls.append(model)
        return status, kind

    probe.calls = calls
    return probe


class TestMeasuredDeathClassifiedAsAuth(unittest.TestCase):
    """実測の死が auth に断定できること (failures.md の根拠チェーン)。"""

    def test_engine_message_alone_is_unknown(self):
        # エンジンの言葉 ("finish_reason: network_error") は FAILURE_PATTERNS の
        # どれにも一致しない。network と誤って寄せないこと (知らない文字列は
        # 捏造せず unknown に落とすのが表の規律)
        self.assertEqual(classify_session_failure(MEASURED_STDERR_TAIL), "unknown")

    def test_probe_401_promotes_unknown_to_auth(self):
        info = build_failure_info(
            MEASURED_STDERR_TAIL,
            model="opencode-go/ox-alpha-free",
            outcome="error",
            prober=fake_prober(401, "auth"),
        )
        self.assertEqual(info["failure_kind"], "auth")
        self.assertEqual(info["probe_status"], "auth")
        self.assertEqual(info["probe_http_status"], 401)

    def test_measured_event_json_reaches_classifier_via_consume_path(self):
        # 生イベント行から Session.run() と同じ構成で blob を作っても
        # 分類が変わらないこと (CLI 出力経路の変化への保険)
        import json

        ev = json.loads(MEASURED_ERROR_EVENT)
        errors = []
        from ops.runner.runner import consume_stream_event

        consume_stream_event(ev, {"tokens": 0, "cost": 0.0}, errors)
        blob = "\n" + "\n".join(errors)
        self.assertEqual(classify_session_failure(blob), "unknown")
        self.assertIn("provider finish_reason: network_error", blob.lower())


class TestFailureKindMapping(unittest.TestCase):
    """6 種 (usage_limit / auth / network / budget / timeout / unknown) の対応。"""

    def test_probe_status_mapping_401_and_429(self):
        self.assertEqual(probe_failure_kind(401), "auth")
        self.assertEqual(probe_failure_kind(429), "usage_limit")
        self.assertIsNone(probe_failure_kind(200))
        self.assertIsNone(probe_failure_kind(500))

    def test_known_wordings_still_classified_without_probe(self):
        # 既知文言はプローブを打たずに確定する (プローブ呼び出し回数 0 の実測)
        prober = fake_prober(None, None)
        cases = {
            "Claude AI usage limit reached|1754697600": "usage_limit",
            "Invalid API key.": "auth",
            "Cannot connect to API: Unable to connect.": "network",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                info = build_failure_info(
                    text, model="opencode-go/ox-alpha-free",
                    outcome="error", prober=prober,
                )
                self.assertEqual(info["failure_kind"], want)
        self.assertEqual(prober.calls, [])

    def test_timeout_and_budget_are_represented_outside_classifier(self):
        # timeout は outcome (session_timeout)、budget は result state
        # (budget_exhausted) という別層の表現であることが現行契約
        # (PROJECT.md 前提)。分類器がこれらを内包しに来たら、その時点で
        # このテストは更新すること
        kinds = {"usage_limit", "auth", "network", "unknown"}
        got = classify_session_failure(MEASURED_STDERR_TAIL)
        self.assertIn(got, kinds)


class TestCurriculumNextAction(unittest.TestCase):
    """歯止めの発火条件。4 値マッピングを網羅する。"""

    def test_completed_with_artifact_is_done(self):
        self.assertEqual(curriculum_next_action("completed", True, None, 0), "done")

    def test_completed_without_artifact_gives_up_immediately(self):
        # 「成功を名乗ったのに産物無し」(2026-08-22 実績) は再試行しない。
        # 高価なフルセッションの空振りを繰り返さないための固定
        self.assertEqual(
            curriculum_next_action("completed", False, None, 0), "give_up"
        )

    def test_transient_death_retries_bounded(self):
        # 実測死因 auth: 1・2 回目は retry、3 回目で give_up (全 3 試行)
        seen = [
            curriculum_next_action("error", False, "auth", n)
            for n in range(CURRICULUM_MAX_CONSECUTIVE_ERRORS)
        ]
        self.assertEqual(seen, ["retry", "retry", "give_up"])

    def test_all_non_usage_limit_kinds_share_the_same_bound(self):
        for kind in ("auth", "network", "unknown", None):
            with self.subTest(kind=kind):
                self.assertEqual(
                    curriculum_next_action("error", False, kind,
                                           CURRICULUM_MAX_CONSECUTIVE_ERRORS - 1),
                    "give_up",
                )
                self.assertEqual(
                    curriculum_next_action("error", False, kind, 0), "retry"
                )

    def test_usage_limit_never_counts_toward_give_up(self):
        # 上限待ち (P-0026 流儀) は何周しても連続エラーに数えない
        for n in range(CURRICULUM_MAX_CONSECUTIVE_ERRORS + 3):
            self.assertEqual(
                curriculum_next_action("error", False, "usage_limit", n),
                "quota_wait",
            )

    def test_timeout_and_inactivity_killed_are_retryable_not_fatal(self):
        # runner 自身が殺した系 (session_timeout / inactive_killed) も
        # 有界リトライの対象。1 回で Job を落とさない
        for outcome in ("session_timeout", "inactive_killed"):
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    curriculum_next_action(outcome, False, None, 0), "retry"
                )
                self.assertEqual(
                    curriculum_next_action(outcome, False, None,
                                           CURRICULUM_MAX_CONSECUTIVE_ERRORS - 1),
                    "give_up",
                )


if __name__ == "__main__":
    unittest.main()
