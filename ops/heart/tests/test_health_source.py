"""健全性レポートの読み先を固定する (設計 state-out-of-git Phase 5)。

正はクラスタ内の ConfigMap で、ブランチはそれが読めない/古いときの逃げ道。
両端が同じクラスタに居るのに GitHub を往復していた構図を切るのがこの変更で、
ここで縛るのは 3 点:

  - ConfigMap が新しければ git を一切見ない
  - ConfigMap が読めない・壊れている・古いときだけブランチへ落ちる
  - **どちらも読めなければ「健全」にしない** (unhealthy_apps=None, fresh=False)。
    ops/check_heartbeat_fresh.py と同じ fail-closed の作法
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from ops.heart import facts

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def report(generated_at, apps=None):
    return json.dumps(
        {
            "generated_at": generated_at,
            "applications": apps
            if apps is not None
            else [{"name": "immich", "sync": "Synced", "health": "Healthy"}],
        }
    )


def stamp(offset):
    return (NOW + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeK8s:
    """get_configmap だけを持つ最小の k8s。raises を渡すとそれを投げる。"""

    def __init__(self, data=None, raises=None):
        self.data = data
        self.raises = raises
        self.calls = []

    def get_configmap(self, namespace, name):
        self.calls.append((namespace, name))
        if self.raises is not None:
            raise self.raises
        return {"data": self.data} if self.data is not None else {}


class Git:
    """gitutil.show の差し替え。呼ばれたかどうかも見る。"""

    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def show(self, repo_dir, ref, path):
        self.calls += 1
        return self.raw


def load(k8s, branch_raw, now=NOW):
    git = Git(branch_raw)
    with mock.patch.object(facts.gitutil, "show", git.show):
        return facts.load_health("/repo", "ops-health-report", k8s, now), git


class TestConfigMapIsThePrimarySource(unittest.TestCase):
    def test_fresh_configmap_is_used_without_touching_git(self):
        k8s = FakeK8s({"latest.json": report(stamp(-timedelta(minutes=5)))})
        (unhealthy, fresh, doc), git = load(k8s, report(stamp(-timedelta(minutes=1))))
        self.assertEqual(unhealthy, [])
        self.assertTrue(fresh)
        self.assertIsNotNone(doc)
        self.assertEqual(git.calls, 0, "ConfigMap が読めたのに git を見ている")
        self.assertEqual(k8s.calls, [("ops-health-reporter", "ops-health-report")])

    def test_unhealthy_applications_come_through(self):
        k8s = FakeK8s(
            {
                "latest.json": report(
                    stamp(-timedelta(minutes=5)),
                    [{"name": "immich", "sync": "OutOfSync", "health": "Healthy"}],
                )
            }
        )
        (unhealthy, fresh, _), _ = load(k8s, None)
        self.assertEqual(unhealthy, ["immich"])
        self.assertTrue(fresh)


class TestFallbackToTheBranch(unittest.TestCase):
    def test_unreadable_configmap_falls_back(self):
        k8s = FakeK8s(raises=RuntimeError("403"))
        (unhealthy, fresh, doc), git = load(k8s, report(stamp(-timedelta(minutes=1))))
        self.assertEqual(unhealthy, [])
        self.assertTrue(fresh)
        self.assertIsNotNone(doc)
        self.assertEqual(git.calls, 1)

    def test_broken_json_in_the_configmap_falls_back(self):
        k8s = FakeK8s({"latest.json": "{ これは JSON ではない"})
        (_, fresh, _), git = load(k8s, report(stamp(-timedelta(minutes=1))))
        self.assertTrue(fresh)
        self.assertEqual(git.calls, 1)

    def test_missing_key_falls_back(self):
        k8s = FakeK8s({"別のキー": "{}"})
        (_, fresh, _), git = load(k8s, report(stamp(-timedelta(minutes=1))))
        self.assertTrue(fresh)
        self.assertEqual(git.calls, 1)

    def test_stale_configmap_loses_to_a_fresh_branch(self):
        k8s = FakeK8s({"latest.json": report(stamp(-timedelta(hours=9)))})
        (_, fresh, doc), git = load(k8s, report(stamp(-timedelta(minutes=1))))
        self.assertTrue(fresh)
        self.assertEqual(doc["generated_at"], stamp(-timedelta(minutes=1)))
        self.assertEqual(git.calls, 1)

    def test_no_k8s_client_keeps_the_legacy_route(self):
        (unhealthy, fresh, _), git = load(None, report(stamp(-timedelta(minutes=1))))
        self.assertEqual(unhealthy, [])
        self.assertTrue(fresh)
        self.assertEqual(git.calls, 1)


class TestFailClosed(unittest.TestCase):
    def test_neither_route_readable_is_unknown_not_healthy(self):
        k8s = FakeK8s(raises=RuntimeError("403"))
        (unhealthy, fresh, doc), git = load(k8s, None)
        self.assertIsNone(unhealthy, "読めないことを「異常なし」に倒してはいけない")
        self.assertFalse(fresh)
        self.assertIsNone(doc)
        self.assertEqual(git.calls, 1)

    def test_both_stale_stays_not_fresh(self):
        k8s = FakeK8s({"latest.json": report(stamp(-timedelta(hours=9)))})
        (unhealthy, fresh, doc), _ = load(k8s, report(stamp(-timedelta(hours=20))))
        self.assertFalse(fresh)
        # 古くても読めた事実は残す (doc を捨てると budget/smoke の警報が消える)
        self.assertEqual(unhealthy, [])
        self.assertEqual(doc["generated_at"], stamp(-timedelta(hours=9)))

    def test_missing_generated_at_is_not_fresh(self):
        k8s = FakeK8s({"latest.json": json.dumps({"applications": []})})
        (_, fresh, doc), _ = load(k8s, None)
        self.assertFalse(fresh)
        self.assertIsNotNone(doc)

    def test_unparsable_generated_at_is_not_fresh(self):
        k8s = FakeK8s({"latest.json": report("きのう")})
        (_, fresh, _), _ = load(k8s, None)
        self.assertFalse(fresh)


class TestPickHealth(unittest.TestCase):
    """2 経路の選択そのもの (純関数)。"""

    FRESH = (["a"], True, {"generated_at": "cm"})
    STALE = (["b"], False, {"generated_at": "branch"})

    def test_fresh_primary_wins(self):
        self.assertEqual(facts.pick_health(self.FRESH, self.STALE), self.FRESH)

    def test_fresh_fallback_wins_over_stale_primary(self):
        self.assertEqual(facts.pick_health(self.STALE, self.FRESH), self.FRESH)

    def test_primary_wins_when_both_are_stale(self):
        other = (["c"], False, {"generated_at": "x"})
        self.assertEqual(facts.pick_health(self.STALE, other), self.STALE)

    def test_readable_fallback_beats_an_unreadable_primary(self):
        self.assertEqual(
            facts.pick_health(facts.HEALTH_UNKNOWN, self.STALE), self.STALE
        )

    def test_nothing_readable_is_unknown(self):
        self.assertEqual(
            facts.pick_health(facts.HEALTH_UNKNOWN, facts.HEALTH_UNKNOWN),
            facts.HEALTH_UNKNOWN,
        )


if __name__ == "__main__":
    unittest.main()
