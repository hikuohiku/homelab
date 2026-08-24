"""apps/autopilot-projects-backup/export_projects.py の書き出しロジックを固定する
(設計 docs/design/state-out-of-git Phase 0b)。

このスクリプトは Phase 4b で projects.json を止めた後、**プロジェクトの記録を
クラスタの外に出す唯一の経路**になる。壊れても「バックアップは走った」ように
見えるので、壊れ方を先に固定しておく:

- 出力が決定的 (同じ CR 集合なら同じバイト列)。揺れると restic の重複排除が効かず、
  それ以上に「変わっていないのに差分が出る」ので変化の検知が死ぬ
- 復元の邪魔になる metadata (resourceVersion / uid / creationTimestamp …) が落ちている
- 0 件・前回比の急減・floor 割れで fail-closed
- 全件取り切る (continue トークンを追う)

k8s API へは出ない。list_projects に偽の getter を渡し、辞書に無い URL は即失敗させる。

リポジトリルートから `python3 -m unittest ops.tests.test_export_projects`。
export_projects.py は ConfigMap から直接起動される単一ファイル (パッケージではない) ため
importlib で実ファイルをロードする (test_download_budget.py と同じ形)。
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "autopilot-projects-backup" / "export_projects.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("export_projects_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ep = _load_module()


def cr(name, state="active", **spec):
    """実物に近い CR。クラスタが付ける metadata を必ず含める。"""
    return {
        "apiVersion": "autopilot.homelab.hikuohiku.dev/v1",
        "kind": "Project",
        "metadata": {
            "name": name,
            "namespace": "autopilot",
            "uid": "0d9f5b6a-{}".format(name),
            "resourceVersion": "123456",
            "creationTimestamp": "2026-08-24T00:00:00Z",
            "generation": 7,
            "managedFields": [{"manager": "heart", "operation": "Apply"}],
            "labels": {"lifecycle": "live"},
        },
        "spec": dict({"id": name, "title": "タイトル", "state": state}, **spec),
        "status": {"observed": True},
    }


class TestSanitize(unittest.TestCase):
    def test_cluster_assigned_metadata_is_dropped(self):
        out = ep.sanitize(cr("p-0001"))
        self.assertEqual(sorted(out["metadata"]), ["labels", "name", "namespace"])
        self.assertNotIn("status", out)
        self.assertEqual(out["kind"], "Project")
        self.assertEqual(out["apiVersion"], "autopilot.homelab.hikuohiku.dev/v1")

    def test_spec_survives_verbatim(self):
        """spec は projects.json の 1 エントリそのもの。ここを削ると復元できない。"""
        item = cr("p-0002", branch="feat/x", budget={"soft_cap": 4000000})
        self.assertEqual(ep.sanitize(item)["spec"], item["spec"])

    def test_apply_residue_annotation_is_dropped(self):
        item = cr("p-0003")
        item["metadata"]["annotations"] = {
            "kubectl.kubernetes.io/last-applied-configuration": "{...}",
            "autopilot/note": "残す",
        }
        self.assertEqual(ep.sanitize(item)["metadata"]["annotations"], {"autopilot/note": "残す"})

    def test_annotations_disappear_when_only_residue(self):
        item = cr("p-0004")
        item["metadata"]["annotations"] = {
            "kubectl.kubernetes.io/last-applied-configuration": "{...}"
        }
        self.assertNotIn("annotations", ep.sanitize(item)["metadata"])


class TestDeterminism(unittest.TestCase):
    def test_same_set_gives_identical_bytes_regardless_of_order(self):
        a = [cr("p-0003"), cr("p-0001"), cr("p-0002")]
        b = [cr("p-0002"), cr("p-0003"), cr("p-0001")]
        first = ep.serialize(ep.build_document(a)).encode("utf-8")
        second = ep.serialize(ep.build_document(b)).encode("utf-8")
        self.assertEqual(first, second)

    def test_key_order_inside_a_cr_does_not_matter(self):
        item = cr("p-0001", branch="feat/x", confidence="high")
        shuffled = json.loads(json.dumps(item))
        shuffled["spec"] = dict(reversed(list(shuffled["spec"].items())))
        self.assertEqual(
            ep.serialize(ep.build_document([item])),
            ep.serialize(ep.build_document([shuffled])),
        )

    def test_volatile_metadata_does_not_change_the_bytes(self):
        """毎ビート変わる resourceVersion が出力に漏れていたら、ここで落ちる。"""
        item = cr("p-0001")
        moved = json.loads(json.dumps(item))
        moved["metadata"]["resourceVersion"] = "999999"
        moved["metadata"]["uid"] = "別の uid"
        self.assertEqual(
            ep.serialize(ep.build_document([item])),
            ep.serialize(ep.build_document([moved])),
        )

    def test_items_are_sorted_by_name(self):
        doc = ep.build_document([cr("p-0003"), cr("p-0001"), cr("p-0002")])
        self.assertEqual([i["metadata"]["name"] for i in doc["items"]],
                         ["p-0001", "p-0002", "p-0003"])

    def test_document_is_an_appliable_list(self):
        doc = ep.build_document([cr("p-0001")])
        self.assertEqual((doc["apiVersion"], doc["kind"]), ("v1", "List"))

    def test_japanese_is_not_escaped(self):
        """ensure_ascii=False。読めない JSON を B2 に置いても復元時に困るだけ。"""
        self.assertIn("タイトル", ep.serialize(ep.build_document([cr("p-0001")])))


class TestFailClosed(unittest.TestCase):
    def test_zero_always_fails(self):
        """CRD の同期失敗や RBAC 事故で空になったものを正として上書きさせない。"""
        self.assertTrue(ep.check_export(0, previous=120))
        self.assertTrue(ep.check_export(0, previous=None))

    def test_sharp_drop_fails(self):
        self.assertTrue(ep.check_export(60, previous=112))
        # 境界 (10% ちょうどの減少) は通す側。1 件でも下回れば落ちる
        self.assertEqual(ep.check_export(90, previous=100), [])
        self.assertTrue(ep.check_export(89, previous=100))

    def test_growth_passes(self):
        self.assertEqual(ep.check_export(130, previous=112), [])

    def test_floor_only_applies_when_previous_is_unknown(self):
        self.assertTrue(ep.check_export(3, previous=None, minimum=100))
        # 前回が分かっているときは floor ではなく比で見る (少数から育てる場合を止めない)
        self.assertEqual(ep.check_export(3, previous=3, minimum=100), [])

    def test_previous_count_reads_a_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "previous.json"
            path.write_text(ep.serialize(ep.build_document([cr("p-0001"), cr("p-0002")])))
            self.assertEqual(ep.previous_count(str(path)), 2)

    def test_previous_count_is_unknown_when_missing_or_broken(self):
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "broken.json"
            broken.write_text("{ちぎれた")
            self.assertIsNone(ep.previous_count(str(Path(d) / "absent.json")))
            self.assertIsNone(ep.previous_count(str(broken)))


class TestListProjects(unittest.TestCase):
    def test_follows_the_continue_token(self):
        """全件のつもりで 1 ページ目だけ、を防ぐ。"""
        pages = {
            0: {"items": [cr("p-0001")], "metadata": {"continue": "tok"}},
            1: {"items": [cr("p-0002")], "metadata": {}},
        }
        seen = []

        def fake_get(path):
            seen.append(path)
            return pages[len(seen) - 1]

        items = ep.list_projects(get=fake_get, namespace="autopilot")
        self.assertEqual([i["metadata"]["name"] for i in items], ["p-0001", "p-0002"])
        self.assertIn("/namespaces/autopilot/projects?", seen[0])
        self.assertIn("continue=tok", seen[1])

    def test_api_errors_are_not_swallowed(self):
        def boom(path):
            raise OSError("connection refused")

        with self.assertRaises(OSError):
            ep.list_projects(get=boom)


if __name__ == "__main__":
    unittest.main()
