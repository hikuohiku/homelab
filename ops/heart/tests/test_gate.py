"""admission gate (ops/heart/gate.py) の受け口テスト。

判定そのものの表は test_reconcile.py の AdmissionGateDecision にある。
ここで固定するのは **判定の外側** — 実際に HTTP で叩いたときに Job が
1 本しか立たないこと、capability が付かないこと、停止が効くこと。
"""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ops.heart import dispatch, gate, reconcile, spawn

RULES_PATH = Path(__file__).resolve().parents[2] / "rules.json"
with open(RULES_PATH) as f:
    RULES = json.load(f)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


class FakeCfg:
    def __init__(self, data_dir):
        self.rules = RULES
        self.namespace = "autopilot"
        self.repo = "hikuohiku/homelab"
        self.image = "ghcr.io/example/autopilot@sha256:0"
        self.data_dir = Path(data_dir)
        self.models = {"roles": {"project": "m", "review": "m"}}

    def model_for(self, role):
        return "m"


class FakeK8s:
    """spawn.create が使う口だけを持つ。409 (既にある) は正常扱い。"""

    def __init__(self):
        self.created = []

    def create_job(self, namespace, job):
        name = job["metadata"]["name"]
        if any(j["metadata"]["name"] == name for j in self.created):
            raise spawn.K8sError(409, "already exists")
        self.created.append(job)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = FakeCfg(self.tmp.name)
        self.k8s = FakeK8s()
        self.gate = gate.AdmissionGate(
            cfg_provider=lambda: self.cfg,
            k8s_provider=lambda: self.k8s,
            data_dir=self.tmp.name,
            repo_url="https://example.invalid/repo.git",
            now=lambda: NOW,
        )
        self.publish()

    def publish(self, projects=(), stop_engaged=False, at=NOW, shadow=False):
        doc = {"version": 1, "projects": list(projects), "stop_engaged": stop_engaged}
        self.gate.update(doc, RULES, at, shadow)

    def request(self, **kw):
        base = {
            "title": "ops-dashboard の 500 を直す",
            "body": "snapshot API が 500 を返している",
        }
        base.update(kw)
        return base

    def post(self, payload=None):
        return self.gate.dispatch_request(payload or self.request())

    def drain(self):
        """worker スレッドを起こさずに、キューに積まれたぶんを同期で処理する。"""
        done = []
        while not self.gate.queue.empty():
            done.append(self.gate.run_one(self.gate.queue.get()))
        return done

    # --- 受理して Job まで ---
    def test_accepted_request_creates_exactly_one_job(self):
        code, body = self.post()
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], reconcile.ADMIT_ACCEPTED)
        self.assertTrue(body["project_id"].startswith(dispatch.PROJECT_ID_PREFIX))
        # 同期の応答は Job を待たない (Job 作成は非同期)
        self.assertEqual(self.k8s.created, [])

        [record] = self.drain()
        self.assertEqual(record["status"], dispatch.DISPATCHED)
        self.assertEqual(len(self.k8s.created), 1)

    def test_same_request_twice_creates_one_job(self):
        """冪等。二重着手は取りこぼしより高くつく。"""
        first_code, first = self.post()
        second_code, second = self.post()
        self.assertEqual(first_code, 202)
        self.assertEqual(second_code, 200)
        self.assertEqual(second["status"], reconcile.ADMIT_DUPLICATE)
        self.assertEqual(first["dispatch_id"], second["dispatch_id"])
        self.drain()
        self.assertEqual(len(self.k8s.created), 1)

    def test_duplicate_after_the_beat_folded_it(self):
        code, first = self.post()
        self.assertEqual(code, 202)
        self.drain()
        # ビートが projects.json に折り込んだ後の再送も 1 件に畳む
        self.publish(projects=[{
            "id": first["project_id"], "state": "active",
            "dispatch_id": first["dispatch_id"],
        }])
        _, second = self.post()
        self.assertEqual(second["status"], reconcile.ADMIT_DUPLICATE)
        self.drain()
        self.assertEqual(len(self.k8s.created), 1)

    def test_job_never_gets_the_write_service_account(self):
        """capability を宣言していない要求に kubectl-write の SA は付かない。
        即時 dispatch は宣言連鎖 (spec → 予告) を通らないので、そもそも
        capability を名乗れない。"""
        self.post()
        self.drain()
        pod = self.k8s.created[0]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "autopilot-runner")
        self.assertFalse(pod["automountServiceAccountToken"])

    def test_capability_request_is_refused_before_any_job(self):
        code, body = self.post(self.request(capabilities=["kubectl-write"]))
        self.assertEqual(code, 409)
        self.assertEqual(body["reason"], "capability_not_declared")
        self.assertEqual(self.k8s.created, [])

    def test_spec_rides_along_in_the_job_env(self):
        """main の archive.jsonl を経由しないので、spec は Job の env で渡る。"""
        self.post()
        self.drain()
        env = {
            e["name"]: e.get("value")
            for e in self.k8s.created[0]["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        spec = json.loads(env["HEART_SPEC_JSON"])
        self.assertEqual(spec["capabilities"], [])
        # 受入検証は持たない (2026-08-24 の所有者判断)
        self.assertEqual(spec["verify"], [])
        self.assertTrue(env["PROJECT_BRANCH"].startswith("project/p-9"))

    # --- 断る ---
    def test_stop_engaged_refuses(self):
        self.publish(stop_engaged=True)
        code, body = self.post()
        self.assertEqual(code, 409)
        self.assertEqual(body["reason"], "stop_engaged")
        self.assertEqual(self.k8s.created, [])

    def test_stop_after_admission_aborts_before_the_job(self):
        """受理してから Job を作るまでの間に来た停止を無視しない。"""
        self.post()
        self.publish(stop_engaged=True)
        [record] = self.drain()
        self.assertEqual(record["status"], dispatch.ABORTED)
        self.assertEqual(record["reason"], "human_stop")
        self.assertEqual(self.k8s.created, [])

    def test_capacity_refuses(self):
        running = [
            {"id": f"P-000{i}", "state": "active"}
            for i in range(RULES["runner"]["max_concurrent"])
        ]
        self.publish(projects=running)
        code, body = self.post()
        self.assertEqual(code, 409)
        self.assertEqual(body["reason"], "capacity")

    def test_stale_snapshot_refuses(self):
        self.publish(at=NOW - timedelta(hours=1))
        _, body = self.post()
        self.assertEqual(body["reason"], "state_stale")

    def test_no_adopt_gate_runs_before_the_job(self):
        """採択ゲートは通さない (2026-08-24 の所有者判断)。
        受理したらそのまま Job を作る — verify が無いことは差し戻し理由にならない。"""
        self.post()
        [record] = self.drain()
        self.assertEqual(record["status"], dispatch.DISPATCHED)
        self.assertNotIn("adopt_gate", record)
        self.assertEqual(len(self.k8s.created), 1)

    def test_spawn_error_is_recorded_not_swallowed(self):
        def boom(*a, **kw):
            raise RuntimeError("k8s に届かない")

        self.gate._create_job = boom
        self.post()
        [record] = self.drain()
        self.assertEqual(record["status"], dispatch.ABORTED)
        self.assertEqual(record["reason"], "spawn_error")
        self.assertIn("k8s", record["detail"])

    # --- ビートへの受け渡し ---
    def test_result_lands_in_the_inbox_for_the_next_beat(self):
        _, body = self.post()
        self.drain()
        inbox = Path(self.tmp.name) / dispatch.DISPATCH_DIR / dispatch.INBOX
        [path] = list(inbox.iterdir())
        record = json.loads(path.read_text())
        self.assertEqual(record["dispatch_id"], body["dispatch_id"])
        self.assertEqual(record["requested_by"], "core")

    def test_ledger_survives_a_restart(self):
        _, body = self.post()
        self.drain()
        revived = gate.AdmissionGate(
            cfg_provider=lambda: self.cfg,
            k8s_provider=lambda: self.k8s,
            data_dir=self.tmp.name,
            repo_url="https://example.invalid/repo.git",
            now=lambda: NOW,
        )
        # 払い出し済みの id を覚え直している = 再起動後に同じ id を配らない
        self.assertIn(body["project_id"], revived.allocated)
        self.assertEqual(len(revived.recent), 1)

    def test_project_ids_do_not_collide_with_the_curriculum_series(self):
        self.publish(projects=[{"id": "P-0302", "state": "delivered"}])
        _, body = self.post()
        self.assertTrue(body["project_id"].startswith("P-9"))

    def test_inflight_blocks_the_next_request_at_capacity(self):
        """受理済みでゲート実行中のものも走行数に数える。"""
        limit = RULES["runner"]["max_concurrent"]
        self.publish(projects=[
            {"id": f"P-000{i}", "state": "active"} for i in range(limit - 1)
        ])
        code, _ = self.post()
        self.assertEqual(code, 202)
        code, body = self.post(self.request(title="別の依頼"))
        self.assertEqual(code, 409)
        self.assertEqual(body["reason"], "capacity")


class GateHTTPTest(unittest.TestCase):
    """cluster 内から HTTP で叩く形そのもの。開けている口は 2 つだけ。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = FakeCfg(self.tmp.name)
        self.gate = gate.AdmissionGate(
            cfg_provider=lambda: cfg,
            k8s_provider=lambda: FakeK8s(),
            data_dir=self.tmp.name,
            repo_url="https://example.invalid/repo.git",
            now=lambda: NOW,
        )
        self.gate.update({"version": 1, "projects": []}, RULES, NOW, False)
        server = self.gate.start("127.0.0.1:0")
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_dispatch_over_http(self):
        code, body = self.call("/dispatch", {
            "title": "直す", "body": "壊れている",
        })
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], reconcile.ADMIT_ACCEPTED)

    def test_healthz_reports_snapshot_age(self):
        code, body = self.call("/healthz")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["snapshot_age_seconds"], 0)

    def test_unknown_paths_are_closed(self):
        self.assertEqual(self.call("/metrics")[0], 404)
        self.assertEqual(self.call("/kill", {})[0], 404)

    def test_broken_json_is_a_400_not_a_crash(self):
        req = urllib.request.Request(
            self.base + "/dispatch", data=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
