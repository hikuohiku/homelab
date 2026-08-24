"""load_health() の契約を固定する (設計 state-out-of-git Phase 5)。

読み先は GitHub のブランチではなく、同じ namespace の ConfigMap。
守りたいのは 2 つ:

- reporter が書いた形 (data の latest.json キーに JSON 文字列) をそのまま読めること
- 読めない・古いを「健全」に化けさせないこと (fail-closed)。generated_at が
  HEALTH_FRESH_SECONDS より古ければ fresh=False で、decide 側が保守的に倒す
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from ops.heart import facts

NAMESPACE = "autopilot"
CONFIGMAP = "ops-health-report"


def report(generated_at, applications=()):
    return json.dumps(
        {"generated_at": generated_at, "applications": list(applications)},
        ensure_ascii=False,
    )


class FakeK8s:
    """get_configmap だけを持つ最小の k8s クライアント。"""

    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.asked = None

    def get_configmap(self, namespace, name):
        self.asked = (namespace, name)
        if self.error is not None:
            raise self.error
        return {"metadata": {"name": name}, "data": self.data}


def iso(delta_seconds):
    at = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestLoadHealth(unittest.TestCase):
    def test_reads_reporter_configmap(self):
        k8s = FakeK8s({facts.HEALTH_KEY: report(iso(-60), [
            {"name": "immich", "sync": "Synced", "health": "Degraded"},
            {"name": "coder", "sync": "Synced", "health": "Healthy"},
        ])})
        unhealthy, fresh, doc = facts.load_health(k8s, NAMESPACE, CONFIGMAP)

        self.assertEqual(k8s.asked, (NAMESPACE, CONFIGMAP))
        self.assertEqual(unhealthy, ["immich"])
        self.assertTrue(fresh)
        self.assertEqual(doc["applications"][0]["name"], "immich")

    def test_stale_report_is_not_fresh(self):
        # 産出側 (CronJob) が死ぬと generated_at が伸びる。中身は読めても信じない
        old = iso(-(facts.HEALTH_FRESH_SECONDS + 60))
        _, fresh, doc = facts.load_health(
            k8s := FakeK8s({facts.HEALTH_KEY: report(old)}), NAMESPACE, CONFIGMAP
        )
        self.assertIsNotNone(doc)
        self.assertFalse(fresh)
        self.assertEqual(k8s.asked, (NAMESPACE, CONFIGMAP))

    def test_boundary_just_inside_is_fresh(self):
        _, fresh, _ = facts.load_health(
            FakeK8s({facts.HEALTH_KEY: report(iso(-(facts.HEALTH_FRESH_SECONDS - 60)))}),
            NAMESPACE,
            CONFIGMAP,
        )
        self.assertTrue(fresh)

    def test_missing_key_is_unknown(self):
        # reporter がまだ一度も書いていない ConfigMap。「全部 healthy」ではない
        self.assertEqual(
            facts.load_health(FakeK8s({}), NAMESPACE, CONFIGMAP), (None, False, None)
        )

    def test_broken_json_is_unknown(self):
        self.assertEqual(
            facts.load_health(FakeK8s({facts.HEALTH_KEY: "{"}), NAMESPACE, CONFIGMAP),
            (None, False, None),
        )

    def test_api_error_is_unknown(self):
        # 403 も接続不能も「観測できなかった」。ここで例外を投げるとビートが止まる
        self.assertEqual(
            facts.load_health(FakeK8s(error=RuntimeError("k8s API 403")), NAMESPACE, CONFIGMAP),
            (None, False, None),
        )

    def test_no_client_is_unknown(self):
        self.assertEqual(
            facts.load_health(None, NAMESPACE, CONFIGMAP), (None, False, None)
        )


if __name__ == "__main__":
    unittest.main()
