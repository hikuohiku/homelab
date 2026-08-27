"""「読めたが 0 件」でビートを落とす (設計 state-out-of-git の未対応だった穴)。

CR が読めないことは例外になるので検知できる。塞ぐのはその裏側 —
CRD の消失・RBAC 事故・namespace の取り違えでは API が 200 と空リストを返し、
heart は「やることが無い」として静かに回り続ける。

これは fail-closed の装置なので、鳴れば仕事が止まる。だから固定するのは 2 つ:
**壊れたときに必ず鳴る**ことと、**正常時に鳴らない**こと。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.heart import heart as heart_mod, projectcr
from ops.heart.heart import CrCensusStop, Heart

from .test_state_out_of_git import BeatCase, FakeK8s, project_entry

REPO = Path(__file__).resolve().parents[3]


class CensusVerdict(unittest.TestCase):
    """判定は純関数。しきい値の意味をここで固定する。"""

    def test_no_floor_passes_even_at_zero(self):
        """初回 (新規デプロイ・PVC の作り直し) は必ず通す。落とすと二度と起動しない。"""
        self.assertIsNone(projectcr.census_problem(0, None))
        self.assertIsNone(projectcr.census_problem(112, None))

    def test_zero_below_a_floor_is_broken(self):
        problem = projectcr.census_problem(0, 112)
        self.assertIsNotNone(problem)
        self.assertIn("0 件", problem)

    def test_a_collapse_is_broken(self):
        """112 件が 3 件になるのも「終わった」ではなく「壊れた」。"""
        self.assertIsNotNone(projectcr.census_problem(3, 112))

    def test_a_normal_decrease_passes(self):
        """終端の間引き・手で数個消す運用では鳴らさない (1 割の余地)。"""
        for count in (112, 120, 111, 101):
            self.assertIsNone(projectcr.census_problem(count, 112), count)

    def test_a_floor_of_zero_stays_open(self):
        """空から始まった器はそのまま回る (0 → 0 は減っていない)。"""
        self.assertIsNone(projectcr.census_problem(0, 0))


class CensusFloorOnBeat(BeatCase):
    """床の置き場は PVC、更新は正常に読めたビートだけ。"""

    def setUp(self):
        super().setUp()
        self.h.docs.save_projects(
            {"version": 1, "projects": [project_entry("P-0001")], "chores": []}
        )

    def floor(self):
        return self.h.docs.load_census().get("cr_count")

    def test_the_first_beat_passes_with_no_crs(self):
        self.beat(FakeK8s())
        self.assertEqual(self.floor(), 0)
        self.assertTrue((self.h.doc_dir / "heartbeat.json").exists())

    def test_the_floor_lives_in_the_pvc(self):
        self.beat(FakeK8s(names=[f"p-{i:04d}" for i in range(10)]))
        self.assertEqual(self.floor(), 10)
        self.assertTrue((self.h.doc_dir / "cr-census.json").exists())

    def test_a_collapse_stops_the_beat(self):
        self.beat(FakeK8s(names=[f"p-{i:04d}" for i in range(10)]))
        with self.assertRaises(CrCensusStop):
            self.beat(FakeK8s(), n=2)

    def test_the_floor_does_not_drop_on_a_stopped_beat(self):
        """落としたビートで床を下げると、次のビートは通ってしまう。"""
        self.beat(FakeK8s(names=[f"p-{i:04d}" for i in range(10)]))
        for n in (2, 3):
            with self.assertRaises(CrCensusStop):
                self.beat(FakeK8s(), n=n)
            self.assertEqual(self.floor(), 10)

    def test_a_normal_beat_keeps_running(self):
        """正常時に鳴らない — 誤爆は器を止める。"""
        names = [f"p-{i:04d}" for i in range(10)]
        self.beat(FakeK8s(names=names))
        self.beat(FakeK8s(names=names + ["p-0010"]), n=2)
        self.assertEqual(self.floor(), 11)

    def test_an_unreadable_list_does_not_stop_the_beat(self):
        """読めないことでは止めない (既存の経路が扱う)。床も動かさない。"""
        self.beat(FakeK8s(names=[f"p-{i:04d}" for i in range(10)]))

        class Unreadable(FakeK8s):
            def list_custom(self, *a, **k):
                raise RuntimeError("k8s API 403: projects is forbidden")

        self.beat(Unreadable(), n=2)
        self.assertEqual(self.floor(), 10)

    def test_the_census_list_is_read_once_per_beat(self):
        """全件 list を 1 ビートに 2 度打たない (census の一覧を sync が使い回す)。"""
        k8s = FakeK8s(names=[f"p-{i:04d}" for i in range(10)])
        calls = []
        inner = k8s.list_custom

        def counting(api_version, namespace, plural, label_selector=None):
            calls.append(label_selector)
            return inner(api_version, namespace, plural, label_selector)

        k8s.list_custom = counting
        self.beat(k8s)
        self.assertEqual([c for c in calls if c is None], [None])


class CensusAlarm(unittest.TestCase):
    """止めたことが人間に届く (metrics.jsonl には読み手が居ない)。"""

    class Spy:
        def __init__(self):
            self.sent = []

        def send(self, ntype, text, now):
            self.sent.append((ntype, text))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # shadow では通知が握られてログになる。ここで見たいのは鳴る条件なので active
        env = mock.patch.dict(
            os.environ, {"HEART_DATA_DIR": tmp.name, "HEART_MODE": "active"}
        )
        env.start()
        self.addCleanup(env.stop)
        self.h = Heart(REPO)
        self.spy = self.Spy()

    def test_the_first_stopped_beat_rings(self):
        """書き込み失敗と違って猶予を置かない。ビートは既に止まっている。"""
        self.h.note_cr_census("Project CR が 0 件", self.spy, None)
        self.assertEqual(len(self.spy.sent), 1)
        self.assertEqual(self.spy.sent[0][0], "incident")
        self.assertIn("docs/backup.md", self.spy.sent[0][1])

    def test_it_does_not_ring_every_beat(self):
        for _ in range(heart_mod.CENSUS_ALERT_BEATS):
            self.h.note_cr_census("Project CR が 0 件", self.spy, None)
        self.assertEqual(len(self.spy.sent), 1)
        self.h.note_cr_census("Project CR が 0 件", self.spy, None)
        self.assertEqual(len(self.spy.sent), 2)

    def test_a_stopped_beat_does_not_also_ring_the_write_alarm(self):
        """通知は 2 本立てない。件数が割れたビートは書き込みまで進まない。"""
        k8s = FakeK8s()
        self.h.docs.save_census({"version": 1, "cr_count": 112})
        with mock.patch.object(Heart, "k8s_client", lambda self: k8s):
            with self.assertRaises(CrCensusStop):
                self.h.check_cr_census(self.spy, None)
        self.assertEqual(k8s.applied, [])
        self.assertEqual(len(self.spy.sent), 1)


if __name__ == "__main__":
    unittest.main()
