"""Project CR の変換と CRD スキーマ (設計 state-out-of-git Phase 4a)。

固定するもの:

1. CRD の state enum / required が ops/heart/statefiles.py と一致する
   (rejected を足すときに片方だけ動くのを止める)
2. CRD が実物の projects.json の全キーを覆う。構造スキーマは未知フィールドを
   **黙って落とす**ので、覆えていないと状態が音もなく欠ける
3. lifecycle ラベルが終端と非終端を分ける
4. 変わった CR だけ書き、doc から消えたプロジェクトの CR は消さない
5. **CR の書き込みが失敗してもビートが落ちない** (正はまだ projects.json)
"""

import contextlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from ops.heart import facts, gitutil, projectcr, spawn, statefiles
from ops.heart.heart import Heart
from ops.heart.notify import Notifier
from ops.heart.statefiles import StateFiles

REPO = Path(__file__).resolve().parents[3]
CRD = REPO / "apps" / "autopilot" / "crd-project.yaml"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "projects.json"
NS = "autopilot"
TERMINAL = statefiles.TERMINAL_STATES


def crd():
    return yaml.safe_load(CRD.read_text())


def spec_schema():
    version = crd()["spec"]["versions"][0]
    return version["schema"]["openAPIV3Schema"]["properties"]["spec"]


def fixture_doc():
    return json.loads(FIXTURE.read_text())


def undeclared(value, schema, path=""):
    """value のうちスキーマが宣言していないキーの一覧 (構造スキーマは黙って落とす)。"""
    missing = []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k, v in value.items():
            if k not in props:
                missing.append(f"{path}{k}")
                continue
            missing += undeclared(v, props[k], f"{path}{k}.")
    elif isinstance(value, list):
        items = schema.get("items")
        if items is not None:
            for v in value:
                missing += undeclared(v, items, f"{path}[].")
    return missing


def type_errors(value, schema, path="spec"):
    """スキーマの type と実データの食い違い。"""
    kinds = {
        "object": dict, "array": list, "string": str,
        "integer": int, "boolean": bool, "number": (int, float),
    }
    errors = []
    want = schema.get("type")
    if value is None:
        if not schema.get("nullable"):
            errors.append(f"{path}: null は許していない")
        return errors
    if want in kinds:
        # bool は int の派生なので integer と取り違えないよう先に弾く
        bad = not isinstance(value, kinds[want]) or (
            want in ("integer", "number") and isinstance(value, bool)
        )
        if bad:
            errors.append(f"{path}: type={want} だが {type(value).__name__}")
            return errors
    if isinstance(value, dict):
        for k, v in value.items():
            if k in schema.get("properties", {}):
                errors += type_errors(v, schema["properties"][k], f"{path}.{k}")
    elif isinstance(value, list) and schema.get("items"):
        for i, v in enumerate(value):
            errors += type_errors(v, schema["items"], f"{path}[{i}]")
    return errors


class CrdMatchesStatefiles(unittest.TestCase):
    def test_state_enum(self):
        enum = spec_schema()["properties"]["state"]["enum"]
        self.assertEqual(
            tuple(enum),
            statefiles.PROJECT_STATES,
            "CRD の state enum が statefiles.PROJECT_STATES とずれている。"
            "単一情報源は statefiles 側で、rejected を足すときは両方を同時に動かすこと",
        )

    def test_required_fields(self):
        self.assertEqual(
            tuple(spec_schema()["required"]),
            statefiles.REQUIRED_PROJECT_FIELDS,
            "CRD の required が REQUIRED_PROJECT_FIELDS とずれている",
        )

    def test_group_and_kind(self):
        c = crd()
        self.assertEqual(c["spec"]["group"], projectcr.GROUP)
        self.assertEqual(c["spec"]["names"]["kind"], projectcr.KIND)
        self.assertEqual(c["spec"]["names"]["plural"], projectcr.PLURAL)
        self.assertEqual(c["spec"]["scope"], "Namespaced")
        self.assertEqual(c["spec"]["versions"][0]["name"], projectcr.VERSION)

    def test_no_escape_hatch(self):
        # x-kubernetes-preserve-unknown-fields で逃げると、スキーマが実データを
        # 表していない状態に静かに戻る
        def walk(node):
            if isinstance(node, dict):
                self.assertNotIn("x-kubernetes-preserve-unknown-fields", node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(crd())


class CrdCoversRealData(unittest.TestCase):
    """実物の projects.json (fixture) がスキーマを通ること。"""

    def setUp(self):
        self.doc = fixture_doc()
        self.schema = spec_schema()

    def test_fixture_is_a_valid_projects_doc(self):
        self.assertEqual(statefiles.validate_projects(self.doc), [])

    def test_every_field_is_declared(self):
        missing = sorted(
            {m for p in self.doc["projects"] for m in undeclared(p, self.schema)}
        )
        self.assertEqual(
            missing, [], f"CRD が宣言していないフィールドがある: {missing}"
        )

    def test_types_match(self):
        errors = [e for p in self.doc["projects"] for e in type_errors(p, self.schema)]
        self.assertEqual(errors, [])

    def test_code_written_fields_are_declared(self):
        """heart が書くフィールドは CRD にも居ること。

        reconcile.py / heart.py の `p["x"] = ...` を拾う。増やしたのに CRD へ
        足し忘れると、その値は API に届いた時点で捨てられる。
        """
        pattern = re.compile(r"""\bp(?:roject)?\[["']([a-z_]+)["']\] *=""")
        written = set()
        for name in ("reconcile.py", "heart.py"):
            written |= set(pattern.findall((REPO / "ops" / "heart" / name).read_text()))
        undeclared_fields = sorted(written - set(self.schema["properties"]))
        self.assertEqual(
            undeclared_fields,
            [],
            f"heart が書くのに CRD に無いフィールド: {undeclared_fields}。"
            "crd-project.yaml へ足すこと",
        )

    def test_names_are_valid_kubernetes_names(self):
        name_re = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
        for p in self.doc["projects"]:
            name = projectcr.cr_name(p["id"])
            self.assertRegex(name, name_re)
            self.assertLessEqual(len(name), 253)


class Conversion(unittest.TestCase):
    def test_cr_shape(self):
        p = fixture_doc()["projects"][0]
        cr = projectcr.to_cr(p, NS, TERMINAL)
        self.assertEqual(cr["apiVersion"], "autopilot.homelab.hikuohiku.dev/v1")
        self.assertEqual(cr["kind"], "Project")
        self.assertEqual(cr["metadata"]["name"], p["id"].lower())
        self.assertEqual(cr["metadata"]["namespace"], NS)
        # spec はエントリそのまま。写しであることを目で確かめられる形にしてある
        self.assertEqual(cr["spec"], p)

    def test_spec_is_a_copy(self):
        p = dict(fixture_doc()["projects"][0])
        cr = projectcr.to_cr(p, NS, TERMINAL)
        cr["spec"]["state"] = "vetoed"
        self.assertNotEqual(p["state"], "vetoed")

    def test_lifecycle_label_splits_terminal(self):
        for state in statefiles.PROJECT_STATES:
            labels = projectcr.labels({"state": state}, TERMINAL)
            self.assertEqual(labels["state"], state)
            self.assertEqual(
                labels["lifecycle"],
                projectcr.TERMINAL if state in TERMINAL else projectcr.LIVE,
                f"{state} の lifecycle が違う",
            )

    def test_terminal_states_are_the_ones_statefiles_says(self):
        live = [
            s for s in statefiles.PROJECT_STATES
            if projectcr.labels({"state": s}, TERMINAL)["lifecycle"] == projectcr.LIVE
        ]
        self.assertEqual(
            set(live), set(statefiles.PROJECT_STATES) - set(TERMINAL)
        )


class Plan(unittest.TestCase):
    def doc(self, *projects):
        return {"version": 1, "projects": list(projects)}

    def project(self, pid="P-0001", state="active"):
        return {
            "id": pid, "title": "t", "state": state,
            "branch": f"project/{pid.lower()}", "irreversible": False,
            "capabilities": [], "budget": {"used_tokens": 0}, "created": "2026-08-24",
        }

    def test_all_new_are_written(self):
        write, orphans = projectcr.plan(
            self.doc(self.project()), [], NS, TERMINAL
        )
        self.assertEqual([c["metadata"]["name"] for c in write], ["p-0001"])
        self.assertEqual(orphans, [])

    def test_unchanged_is_not_written(self):
        p = self.project()
        existing = [projectcr.to_cr(p, NS, TERMINAL)]
        write, _ = projectcr.plan(self.doc(p), existing, NS, TERMINAL)
        self.assertEqual(write, [])

    def test_changed_spec_is_written(self):
        p = self.project()
        existing = [projectcr.to_cr(p, NS, TERMINAL)]
        moved = dict(p, state="delivered")
        write, _ = projectcr.plan(self.doc(moved), existing, NS, TERMINAL)
        self.assertEqual(len(write), 1)
        self.assertEqual(write[0]["metadata"]["labels"]["lifecycle"], "terminal")

    def test_label_drift_is_written(self):
        p = self.project()
        stale = projectcr.to_cr(p, NS, TERMINAL)
        stale["metadata"]["labels"] = {}
        write, _ = projectcr.plan(self.doc(p), [stale], NS, TERMINAL)
        self.assertEqual(len(write), 1)

    def test_server_side_fields_do_not_count_as_drift(self):
        p = self.project()
        served = projectcr.to_cr(p, NS, TERMINAL)
        served["metadata"]["resourceVersion"] = "12345"
        served["metadata"]["labels"]["extra"] = "x"
        write, _ = projectcr.plan(self.doc(p), [served], NS, TERMINAL)
        self.assertEqual(write, [])

    def test_missing_project_is_reported_not_deleted(self):
        gone = projectcr.to_cr(self.project("P-0002"), NS, TERMINAL)
        write, orphans = projectcr.plan(
            self.doc(self.project()), [gone], NS, TERMINAL
        )
        self.assertEqual(orphans, ["p-0002"])
        # plan は削除を一切返さない。git 側が正の間、CR の削除だけが片道
        self.assertEqual([c["metadata"]["name"] for c in write], ["p-0001"])

    def test_real_doc_round_trips(self):
        doc = fixture_doc()
        write, orphans = projectcr.plan(doc, [], NS, TERMINAL)
        self.assertEqual(len(write), len(doc["projects"]))
        self.assertEqual(orphans, [])
        # 2 回目は 1 件も書かない (毎ビート 112 件を送り直さない)
        again, _ = projectcr.plan(doc, write, NS, TERMINAL)
        self.assertEqual(again, [])


class BrokenK8s:
    """すべての呼び出しで落ちる k8s。"""

    def list_custom(self, *a, **k):
        raise RuntimeError("k8s API 403: projects is forbidden")

    def apply_custom(self, *a, **k):
        raise RuntimeError("書けない")


class HalfBrokenK8s(BrokenK8s):
    """list は通るが apply が落ちる。"""

    def __init__(self):
        self.attempts = []

    def list_custom(self, *a, **k):
        return []

    def apply_custom(self, api_version, namespace, plural, name, body):
        self.attempts.append(name)
        raise RuntimeError("書けない")


class BeatSurvivesCrFailure(unittest.TestCase):
    """CR が壊れてもビートは落ちない。正はまだ projects.json。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        env = mock.patch.dict(
            os.environ,
            {"HEART_DATA_DIR": str(self.data_dir), "HEART_MODE": "shadow"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.h = Heart(REPO)
        self.sf = StateFiles(self.h.state_dir)

    def beat(self, k8s):
        patches = [
            mock.patch.object(gitutil, "sync_main", lambda *a, **k: None),
            mock.patch.object(gitutil, "sync_state_branch", lambda *a, **k: None),
            mock.patch.object(gitutil, "commit_and_push_state", lambda *a, **k: None),
            mock.patch.object(type(self.h.gh), "ensure_branch", lambda *a, **k: None),
            mock.patch.object(Heart, "k8s_client", lambda self: k8s),
            mock.patch.object(facts, "load_health", lambda *a, **k: ([], True, None)),
            mock.patch.object(facts, "load_adopted_specs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_jobs", lambda *a, **k: {}),
            mock.patch.object(facts, "collect_prs", lambda *a, **k: ({}, {})),
            mock.patch.object(facts, "collect_curriculum", lambda *a, **k: None),
            mock.patch.object(facts, "collect_critic", lambda *a, **k: None),
            mock.patch.object(
                facts, "collect_feedback",
                lambda gh, rd, cursors, *a, **k: (
                    [], [], False, [], False, [], [], dict(cursors)
                ),
            ),
            mock.patch.object(spawn, "create", lambda *a, **k: "job-dummy"),
            mock.patch.object(Notifier, "send", lambda *a, **k: None),
            mock.patch.object(Notifier, "flush_outbox", lambda *a, **k: None),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.h.beat(1)

    def seed(self):
        doc = self.sf.load_projects()
        doc["projects"] = [
            {
                "id": "P-0001", "title": "t", "state": "delivered",
                "branch": "project/p-0001", "irreversible": False,
                "capabilities": [], "budget": {"used_tokens": 0},
                "created": "2026-08-24",
            }
        ]
        self.sf.save_projects(doc)

    def test_list_failure_does_not_stop_the_beat(self):
        self.seed()
        self.beat(BrokenK8s())
        # ビートは最後まで通り、git 側の写しは書かれている
        self.assertTrue((self.h.state_dir / "heartbeat.json").exists())
        self.assertEqual(len(self.sf.load_projects()["projects"]), 1)

    def test_apply_failure_does_not_stop_the_beat(self):
        self.seed()
        k8s = HalfBrokenK8s()
        self.beat(k8s)
        self.assertEqual(k8s.attempts, ["p-0001"])
        self.assertTrue((self.h.state_dir / "heartbeat.json").exists())

    def test_per_cr_failure_is_swallowed_inside_sync(self):
        # 呼び出し側の try/except に頼らず、1 件の apply 失敗はここで飲まれる
        self.h.k8s = HalfBrokenK8s()
        self.h.sync_project_crs({"projects": [{"id": "P-0001", "state": "active"}]})


if __name__ == "__main__":
    unittest.main()
