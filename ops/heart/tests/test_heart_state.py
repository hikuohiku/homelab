"""正が Project CR に移った後の doc の組み立てと、その置き場 (設計 4b-2)。

固定するもの:

1. CR の一覧から projects.json と同じ doc が復元できる (棄却案は混ざらない)
2. doc のスカラ (stop_engaged / last_*) が HeartState CR に載り、読み戻せる
3. HeartState の CRD が heart の書くキーを全部宣言している
   (構造スキーマは未宣言のフィールドを持つ CR を 500 で丸ごと拒否する)
4. **単一書き手**: Project / HeartState を書けるのは heart の SA だけ
"""

import re
import unittest
from pathlib import Path

import yaml

from ops.heart import heartstate, projectcr, statefiles

REPO = Path(__file__).resolve().parents[3]
RBAC = REPO / "apps" / "autopilot" / "rbac.yaml"
CRD = REPO / "apps" / "autopilot" / "crd-heartstate.yaml"
NS = "autopilot"
TERMINAL = statefiles.TERMINAL_STATES

WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def project(pid="P-0001", state="active", **kw):
    p = {
        "id": pid, "title": "t", "state": state,
        "branch": f"project/{pid.lower()}", "irreversible": False,
        "capabilities": [], "budget": {"used_tokens": 0}, "created": "2026-08-24",
    }
    p.update(kw)
    return p


class DocFromCrs(unittest.TestCase):
    def test_round_trip(self):
        doc = {"version": 1, "chores": [], "projects": [project(), project("P-0002")]}
        items = [projectcr.to_cr(p, NS, TERMINAL) for p in doc["projects"]]
        self.assertEqual(projectcr.doc_from_crs(items), doc)

    def test_rejected_crs_are_not_in_the_working_set(self):
        """棄却案は 250 件超ある。作業集合に混ざると decide が毎ビート全部を舐める。"""
        rej = projectcr.to_cr(
            projectcr.to_rejected_project({"id": "P-0900", "adopted": False}),
            NS, projectcr.REJECTED_TERMINAL,
        )
        items = [projectcr.to_cr(project(), NS, TERMINAL), rej]
        self.assertEqual(
            [p["id"] for p in projectcr.doc_from_crs(items)["projects"]], ["P-0001"]
        )

    def test_order_is_stable(self):
        items = [projectcr.to_cr(project(p), NS, TERMINAL)
                 for p in ("P-0003", "P-0001", "P-0002")]
        self.assertEqual(
            [p["id"] for p in projectcr.doc_from_crs(items)["projects"]],
            ["P-0001", "P-0002", "P-0003"],
        )

    def test_scalars_ride_along(self):
        doc = projectcr.doc_from_crs([], {"stop_engaged": True, "last_critic_at": "x"})
        self.assertTrue(doc["stop_engaged"])
        self.assertEqual(doc["last_critic_at"], "x")
        self.assertEqual(doc["projects"], [])

    def test_scalars_cannot_smuggle_projects_back_in(self):
        doc = projectcr.doc_from_crs([], {"projects": [project("P-9999")]})
        self.assertEqual(doc["projects"], [])

    def test_a_cr_without_an_id_is_dropped(self):
        self.assertEqual(projectcr.doc_from_crs([{"spec": {}}, {}])["projects"], [])

    def test_the_result_is_a_valid_projects_doc(self):
        items = [projectcr.to_cr(project(), NS, TERMINAL)]
        self.assertEqual(
            statefiles.validate_projects(projectcr.doc_from_crs(items)), []
        )


class AdoptedSpecsFromCrs(unittest.TestCase):
    def test_adopted_spec_is_read_from_the_cr(self):
        spec = {"id": "P-0001", "title": "t", "verify": ["true"]}
        items = [projectcr.to_cr(project(spec=spec), NS, TERMINAL)]
        self.assertEqual(projectcr.adopted_specs_from_items(items), {"P-0001": spec})

    def test_rejected_specs_are_not_adopted(self):
        rej = projectcr.to_rejected_project(
            {"id": "P-0900", "adopted": False, "reject_reason": "だめ"}
        )
        items = [projectcr.to_cr(rej, NS, projectcr.REJECTED_TERMINAL)]
        self.assertEqual(projectcr.adopted_specs_from_items(items), {})


class HeartStateCr(unittest.TestCase):
    def test_round_trip(self):
        doc = {"version": 1, "projects": [project()], "stop_engaged": True,
               "last_curriculum_at": "2026-08-24T00:00:00Z"}
        cr = heartstate.to_cr(NS, doc)
        self.assertEqual(cr["kind"], "HeartState")
        self.assertEqual(cr["metadata"]["name"], heartstate.NAME)
        self.assertNotIn("projects", cr["spec"], "プロジェクトは Project CR 側が正")
        got = heartstate.from_cr(cr)
        self.assertTrue(got["stop_engaged"])
        self.assertEqual(got["last_curriculum_at"], "2026-08-24T00:00:00Z")

    def test_a_missing_cr_reads_as_defaults(self):
        self.assertEqual(heartstate.from_cr(None), {})
        self.assertEqual(heartstate.from_cr({}), {})

    def test_the_crd_declares_every_scalar_heart_writes(self):
        """`doc["x"] = ` を拾ってスキーマと突き合わせる。

        足し忘れると server-side apply が 500 で丸ごと拒否し、その瞬間から
        「止めて」が保存されなくなる。
        """
        pattern = re.compile(r"""\bdoc\[["']([a-z_]+)["']\] *=""")
        written = set()
        for name in ("reconcile.py", "heart.py"):
            written |= set(pattern.findall((REPO / "ops" / "heart" / name).read_text()))
        written |= set(projectcr.DOC_DEFAULTS)
        written -= set(heartstate.EXCLUDED)
        schema = yaml.safe_load(CRD.read_text())["spec"]["versions"][0]["schema"]
        declared = set(
            schema["openAPIV3Schema"]["properties"]["spec"]["properties"]
        )
        self.assertEqual(
            sorted(written - declared), [],
            "crd-heartstate.yaml に無いキーを heart が doc に書いている",
        )

    def test_the_crd_group_matches_the_code(self):
        crd = yaml.safe_load(CRD.read_text())
        self.assertEqual(crd["spec"]["group"], projectcr.GROUP)
        self.assertEqual(crd["spec"]["names"]["plural"], heartstate.PLURAL)
        self.assertEqual(crd["spec"]["names"]["kind"], heartstate.KIND)
        self.assertEqual(crd["spec"]["versions"][0]["name"], projectcr.VERSION)


class SingleWriterIsEnforcedByRbac(unittest.TestCase):
    """「heart が唯一の書き手」を慣習でなく RBAC で言い切る (設計の眼目)。

    ops-state の時代、Job が push するのを止めるものは何も無かった。CR にした
    狙いの半分がここなので、機械で固定する。
    """

    def setUp(self):
        self.docs = docs(RBAC)
        self.subjects = {}   # (kind, name) -> [role ...]
        self.rules = {}      # role -> rules
        for d in self.docs:
            if d["kind"] in ("Role", "ClusterRole"):
                self.rules[(d["kind"], d["metadata"]["name"])] = d.get("rules") or []
        for d in self.docs:
            if d["kind"] not in ("RoleBinding", "ClusterRoleBinding"):
                continue
            role = (d["roleRef"]["kind"], d["roleRef"]["name"])
            for s in d.get("subjects") or []:
                self.subjects.setdefault(s["name"], []).append(role)

    def writers_of(self, resource):
        out = set()
        for sa, roles in self.subjects.items():
            for role in roles:
                for rule in self.rules.get(role, []):
                    if projectcr.GROUP not in (rule.get("apiGroups") or []):
                        continue
                    if resource not in (rule.get("resources") or []):
                        continue
                    if WRITE_VERBS & set(rule.get("verbs") or []):
                        out.add(sa)
        return out

    def test_only_heart_can_write_projects(self):
        self.assertEqual(self.writers_of("projects"), {"autopilot-heart"})

    def test_only_heart_can_write_the_heart_state(self):
        self.assertEqual(self.writers_of("heartstates"), {"autopilot-heart"})

    def test_the_writer_sa_cannot_touch_the_group_at_all(self):
        """宣言制注入でプロジェクト Job に渡る SA。CR の API グループを持たない。

        ConfigMap に置かなかった理由がここ — autopilot-writer は configmaps に
        `*` を持っていて、RBAC には名前で穴を塞ぐ手段が無い。
        """
        rules = self.rules[("ClusterRole", "autopilot-writer")]
        groups = {g for r in rules for g in (r.get("apiGroups") or [])}
        self.assertNotIn(projectcr.GROUP, groups)
        self.assertNotIn("*", groups)

    def test_the_core_gets_read_only(self):
        """コアに write を渡さない (設計 D29)。"""
        self.assertNotIn("autopilot-core", self.writers_of("projects"))
        self.assertNotIn("autopilot-core", self.writers_of("heartstates"))

    def test_the_readers_can_actually_read(self):
        readers = set()
        for sa, roles in self.subjects.items():
            for role in roles:
                for rule in self.rules.get(role, []):
                    if projectcr.GROUP not in (rule.get("apiGroups") or []):
                        continue
                    if "heartstates" not in (rule.get("resources") or []):
                        continue
                    if "get" in (rule.get("verbs") or []):
                        readers.add(sa)
        self.assertLessEqual({"ops-dashboard", "autopilot-core"}, readers)


if __name__ == "__main__":
    unittest.main()
