"""生存の Lease (設計 state-out-of-git Phase 7)。

固定するもの:

1. Lease は **ビートが最後まで通ったときだけ**更新される。プロセスが起きたまま
   ループが止まっていれば renewTime は古いまま (P-0027「止まったまま死んだ」)
2. 閾値は ops/rules.json が単一情報源。コードに埋めない
3. 書けなくてもビートは落ちない (書けないことは沈黙として現れる = fail-closed)
4. 名前が読み手 (apps/autopilot-core の silence.go) と揃っている
"""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.heart import facts, gitutil, lease, spawn
from ops.heart.heart import Heart
from ops.heart.notify import Notifier
from ops.heart.statefiles import StateFiles

REPO = Path(__file__).resolve().parents[3]
NS = "autopilot"


class Shape(unittest.TestCase):
    def test_lease_shape(self):
        doc = lease.to_lease(NS, "heart/pod-1", 42, "2026-08-25T12:00:00Z", 7200)
        self.assertEqual(doc["apiVersion"], "coordination.k8s.io/v1")
        self.assertEqual(doc["kind"], "Lease")
        self.assertEqual(doc["metadata"]["name"], "autopilot-heart")
        self.assertEqual(doc["metadata"]["namespace"], NS)
        self.assertEqual(doc["metadata"]["annotations"][lease.BEAT_ANNOTATION], "42")
        self.assertEqual(doc["spec"]["renewTime"], "2026-08-25T12:00:00Z")
        self.assertEqual(doc["spec"]["leaseDurationSeconds"], 7200)
        self.assertEqual(doc["spec"]["holderIdentity"], "heart/pod-1")

    def test_reader_and_writer_agree_on_the_name(self):
        """読み手 (core の silence.go) の既定と揃っていること。

        ずれると誰も見張っていない状態に静かに戻る (読み手は 404 を沈黙と
        判定するので鳴りはするが、鳴り続けるだけで原因が分からない)。
        """
        source = (REPO / "apps" / "autopilot-core" / "app" / "silence.go").read_text()
        self.assertIn(f'"CORE_HEART_LEASE_NAME", "{lease.NAME}"', source)

    def test_rbac_names_the_same_lease(self):
        rbac = (REPO / "apps" / "autopilot" / "rbac.yaml").read_text()
        self.assertIn(f'resourceNames: ["{lease.NAME}"]', rbac)


class Threshold(unittest.TestCase):
    def test_stale_seconds_comes_from_rules(self):
        rules = json.loads((REPO / "ops" / "rules.json").read_text())
        self.assertIsInstance(rules["heartbeat"]["stale_seconds"], int)
        self.assertIsInstance(rules["health"]["stale_seconds"], int)

    def test_heart_does_not_hardcode_the_threshold(self):
        source = (REPO / "ops" / "heart" / "heart.py").read_text()
        self.assertIn('self.cfg.rules["heartbeat"]["stale_seconds"]', source)


class RecordingK8s:
    """apply_lease を記録するだけの k8s。他の呼び出しは無害に済ませる。"""

    def __init__(self):
        self.leases = []

    def apply_lease(self, namespace, name, body):
        self.leases.append((namespace, name, body))
        return body

    def list_custom(self, *a, **k):
        return []

    def apply_custom(self, *a, **k):
        return {}


class BrokenLeaseK8s(RecordingK8s):
    def apply_lease(self, namespace, name, body):
        raise RuntimeError("k8s API 403: leases is forbidden")


class LeaseFollowsTheBeat(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(
            os.environ, {"HEART_DATA_DIR": tmp.name, "HEART_MODE": "shadow"}
        )
        env.start()
        self.addCleanup(env.stop)
        self.h = Heart(REPO)
        # doc の置き場は PVC (設計 state-out-of-git 4b-2b)。空の doc を先に
        # 置く — 無いと load_doc が Project CR からの復元に落ちる
        self.h.docs.save_projects({"version": 1, "projects": [], "chores": []})
        self.sf = self.h.docs

    def beat(self, k8s, fail_before_end=False):
        patches = [
            mock.patch.object(gitutil, "sync_main", lambda *a, **k: None),
            mock.patch.object(Heart, "k8s_client", lambda self: k8s),
            mock.patch.object(facts, "load_health", lambda *a, **k: ([], True, None)),
            mock.patch.object(facts, "load_adopted_specs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_jobs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_prs", lambda *a, **k: ({}, {})),
            mock.patch.object(facts, "collect_curriculum", lambda *a, **k: None),
            mock.patch.object(facts, "collect_critic", lambda *a, **k: None),
            mock.patch.object(
                facts,
                "collect_feedback",
                lambda gh, rd, cursors, *a, **k: (
                    [], [], False, [], False, [], [], dict(cursors)
                ),
            ),
            mock.patch.object(spawn, "create", lambda *a, **k: "job-dummy"),
            mock.patch.object(Notifier, "send", lambda *a, **k: None),
            mock.patch.object(Notifier, "flush_outbox", lambda *a, **k: None),
        ]
        if fail_before_end:
            # ビートの途中で落ちる。「プロセスは生きているがループが回っていない」
            # を再現する最短の形
            patches.append(
                mock.patch.object(
                    Heart, "execute", mock.Mock(side_effect=RuntimeError("途中で死んだ"))
                )
            )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.h.beat(1)

    def test_completed_beat_renews_the_lease(self):
        k8s = RecordingK8s()
        self.beat(k8s)
        self.assertEqual(len(k8s.leases), 1)
        namespace, name, body = k8s.leases[0]
        self.assertEqual((namespace, name), (NS, lease.NAME))
        self.assertEqual(
            body["spec"]["leaseDurationSeconds"],
            self.h.cfg.rules["heartbeat"]["stale_seconds"],
        )

    def test_stuck_beat_does_not_renew_the_lease(self):
        """**この試験がこの仕組みの存在理由。**

        ビートが途中で止まればプロセスが生きていても renewTime は進まない。
        /healthz を見る実装にすると P-0027 の事故を再現する。
        """
        k8s = RecordingK8s()
        with self.assertRaises(RuntimeError):
            self.beat(k8s, fail_before_end=True)
        self.assertEqual(k8s.leases, [], "止まったビートで Lease が更新された")

    def test_lease_failure_does_not_stop_the_beat(self):
        self.beat(BrokenLeaseK8s())
        # ビートは最後まで通っている
        self.assertTrue((self.h.doc_dir / "heartbeat.json").exists())


if __name__ == "__main__":
    unittest.main()
