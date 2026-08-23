"""download-ledger CronJob 埋め込みスクリプト (P-0128) の純関数を固定する。

スクリプトは apps/{immich,vaultwarden,coder,syncthing}/download-ledger-cronjob.yaml
に同一の形で埋め込まれており (CI の check_download_ledger_script_sync.py が drift を
検出)、モジュールとして import すると ServiceAccount token 読みなどの import 副作用が
あるため cluster 外からはそのままロードできない。そこで YAML から実抽出したソースの
うち、副作用を持たない関数と定数だけを AST で取り出して名前空間に入れて試す
(report.py 単一ファイルモジュールを importlib で直接ロードする
test_download_budget.py とは事情が違う)。

このファイルが固定するのはレビュー指摘で壊れ方が実証された箇所:
既存帳簿 (ConfigMap の report.json) に非 dict の壊れた記録が混入したとき、
merge_runs() は例外で終わらず落として個数を返すこと。旧実装は sort key 内の
r.get() で AttributeError になり、「失敗 Job は次回実行でも同じ要素に当たる」
ため帳簿更新が永久停止する時限爆弾だった。
"""

import ast
import datetime
import json
import sys
import textwrap
import unittest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from check_download_ledger_script_sync import (  # noqa: E402
    PATHS,
    extract_block_scalar,
)

# 副作用の無い関数と、それらが参照するモジュールレベル定数だけを抜く。
# (SA token / namespace / SSL_CTX の読み込みは import 時副作用のため対象外)
FUNCTIONS = ("parse_iso", "parse_rules", "owner_cronjob_name",
             "completed_runs", "merge_runs", "trim_runs")
CONSTANTS = ("KEEP_DAYS", "MAX_RUNS")


def load_functions():
    # 抽出直後のソースは YAML のブロックスカラーのインデント (4 空白) を保っている
    source = textwrap.dedent(extract_block_scalar(PATHS[0], "download_ledger.py"))
    tree = ast.parse(source)
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            body.append(node)
        elif isinstance(node, ast.Assign):
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in CONSTANTS:
                body.append(node)
    missing = set(FUNCTIONS + CONSTANTS) - {
        n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in body
    }
    assert not missing, f"抽出に失敗: {sorted(missing)}"
    module = ast.Module(body=body, type_ignores=[])
    ns = {"datetime": datetime, "json": json}
    exec(compile(ast.fix_missing_locations(module), "<download_ledger>", "exec"), ns)
    return ns


ledger = load_functions()

TODAY = datetime.date(2026, 8, 23)


def run(date, job, bytes_, id_=None):
    rec = {"date": date, "job": job, "bytes": bytes_}
    if id_ is not None:
        rec["id"] = id_
    return rec


class MergeRunsTest(unittest.TestCase):
    def test_merges_by_id_incoming_wins(self):
        existing = [run("2026-08-20", "j-backup", 10, id_="job-a")]
        incoming = [
            run("2026-08-23", "j-backup", 32, id_="job-b"),
            # 収集の重複: 同一 id は今回分を優先する
            run("2026-08-20", "j-backup", 99, id_="job-a"),
        ]
        out, dropped = ledger["merge_runs"](existing, incoming)
        self.assertEqual(dropped, 0)
        self.assertEqual(
            [(r["id"], r["bytes"]) for r in out],
            [("job-a", 99), ("job-b", 32)],
        )

    def test_broken_records_are_dropped_with_count_not_fatal(self):
        # レビュー指摘の実証済み崩壊入力。旧実装はここで AttributeError になり、
        # 以後毎回同じ場所で死んで帳簿更新が停止した
        existing = ["not-a-dict", None, 42, run("2026-08-20", "j", 10, id_="job-a")]
        incoming = [run("2026-08-23", "j", 32, id_="job-b")]
        out, dropped = ledger["merge_runs"](existing, incoming)
        self.assertEqual(dropped, 3)
        self.assertEqual([r["id"] for r in out], ["job-a", "job-b"])

    def test_broken_records_in_incoming_are_counted_too(self):
        out, dropped = ledger["merge_runs"]([], ["garbage"])
        self.assertEqual((out, dropped), ([], 1))

    def test_records_without_id_use_fallback_key(self):
        # id 無しの記録は (date, job, bytes) で重複排除される
        existing = [run("2026-08-20", "j", 10)]
        incoming = [run("2026-08-20", "j", 10), run("2026-08-21", "j", 5)]
        out, dropped = ledger["merge_runs"](existing, incoming)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 2)

    def test_empty_inputs(self):
        self.assertEqual(ledger["merge_runs"](None, []), ([], 0))
        self.assertEqual(ledger["merge_runs"]([], None), ([], 0))

    def test_pipeline_with_trim_drops_broken_silently_no_more(self):
        # merge → trim しても壊れた記録が帳簿に混入しない。数は merge の戻り値で
        # payload (dropped_records) へ出せる
        existing = ["garbage", run("2026-08-01", "old-job", 1, id_="old")]
        merged, dropped = ledger["merge_runs"](
            existing,
            [run(f"2026-08-{d:02d}", "vaultwarden-restic-backup", 32, id_=f"job-{d}")
             for d in range(17, 24)],
        )
        self.assertEqual(dropped, 1)
        kept = ledger["trim_runs"](merged, TODAY)
        self.assertEqual(len(kept), 7)
        self.assertTrue(all(isinstance(r, dict) for r in kept))


if __name__ == "__main__":
    unittest.main()
