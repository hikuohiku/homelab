"""spawn.build_job が組み立てる Job spec のリソース指定を固定する。

2026-08-24 18:18 JST、node01 (4 コア) が CPU 枯渇で応答不能になった。原因は
この Job テンプレートに CPU limit が無く、requests (200m) が実測 (~1000m) の
1/5 しかなかったこと。スケジューラは requests を見て詰め込むので、4 コアに
6 本が載り、合計 3761m/4000m に達してホストごと落ちた。

このテストは「CPU limit が付いていること」と「requests が実測に近いこと」を
仕様として固定する。ここが無いと将来また消える (実際、コメントだけでは残らなかった)。
memory limits が**付いていない**ことも同時に固定する — CHARTER §4 の
「OOMKill は回復しない」に対する歯止め。
"""

import unittest
from pathlib import Path

from ops.heart import config, spawn

REPO = Path(__file__).resolve().parents[3]

# Job 1 本に許す CPU。node01 の実測ピーク (runner 1012m) の位置。
JOB_CPU = "1"

# 全 kind が同じテンプレートを共有する (spawn.build_job の kind 分岐は SA / env だけ)
KINDS = ["runner", "reviewer", "curriculum", "consolidation", "critic", "chore"]


def cfg():
    return config.load(REPO, env={"AUTOPILOT_IMAGE": "example.invalid/autopilot:test"})


def container(kind):
    job = spawn.build_job(cfg(), kind, project_id="P-0001")
    containers = job["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def cpu_millis(value):
    """"1" / "500m" を millicore の int に直す。"""
    s = str(value)
    return int(s[:-1]) if s.endswith("m") else int(float(s) * 1000)


class TestJobCpuLimits(unittest.TestCase):
    def test_every_kind_has_cpu_limit(self):
        """CPU limit が無いと Job 1 本がノードの 1 コアを丸ごと食える。"""
        for kind in KINDS:
            with self.subTest(kind=kind):
                res = container(kind)["resources"]
                self.assertEqual(res["limits"]["cpu"], JOB_CPU)

    def test_every_kind_requests_cpu_close_to_limit(self):
        """requests が limit から乖離すると、スケジューラが実態より多く詰め込む。

        200m (旧値) に戻したらここで落ちる。
        """
        for kind in KINDS:
            with self.subTest(kind=kind):
                res = container(kind)["resources"]
                self.assertEqual(res["requests"]["cpu"], JOB_CPU)

    def test_no_memory_limit(self):
        """memory limits は付けない (CHARTER §4: OOMKill は throttle と違い回復しない)。"""
        for kind in KINDS:
            with self.subTest(kind=kind):
                res = container(kind)["resources"]
                self.assertNotIn("memory", res.get("limits", {}))
                self.assertEqual(res["requests"]["memory"], "512Mi")


class TestConcurrencyFitsNode(unittest.TestCase):
    """rules.json の同時実行数が node01 (4 コア) に収まることを固定する。

    Job 1 本のコストと max_concurrent は別ファイルにあり、片方だけ動かすと
    また溢れる。両者の積をここで突き合わせる。
    """

    # node01 の vCPU。増やしたらここと rules.json の _max_concurrent_comment を直す
    NODE_MILLICORES = 4000
    # autopilot 常駐 (heart / bus-sidecar / core / dashboard / nats / telegram-adapter)
    # と、他 namespace + k3s 自身のために空けておく分
    RESERVED_MILLICORES = 1700

    def test_max_concurrent_runners_fit(self):
        rules = cfg().rules
        per_job = cpu_millis(container("runner")["resources"]["requests"]["cpu"])
        total = per_job * rules["runner"]["max_concurrent"]
        self.assertLessEqual(
            total,
            self.NODE_MILLICORES - self.RESERVED_MILLICORES,
            f"runner {rules['runner']['max_concurrent']} 本 x {per_job}m = {total}m は "
            f"node01 ({self.NODE_MILLICORES}m) に収まらない。"
            "並列度を上げたいならノードの vCPU を増やすこと",
        )


if __name__ == "__main__":
    unittest.main()
