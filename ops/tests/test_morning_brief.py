"""apps/openclaw/morning_brief.py (P-0174, 朝 Telegram brief) の純関数を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_morning_brief`。
morning_brief.py は ConfigMap から直接起動される単一ファイルのためパッケージではなく、
テストからは importlib で実ファイルをロードする (test_download_budget.py・
test_openclaw_bridge.py と同じ形)。import 副作用を持たないので cluster 外でも
安全にロードできる (IO は main() に閉じ込めてある)。

固定する契約:
- spec DoD (2) の 2 本柱: 「データが空でも壊れない」「3 行を超えない」
- 欠けた情報源の行は省く (正直に減らす)。全ソース欠損で空文字列になり、
  送信側はそれを見て送信を諦める
- JST の日境界: [day 00:00, day+1 00:00)。UTC と日付がずれるコミットも
  JST で正しい日に落ちる
- 前日の最終状態: history ファイル名は reporter が UTC 日付で付けるため、
  ファイルの最終行ではなく「JST 日境界より前の最新スナップショット」を
  generated_at で選ぶ (最終行を使うと朝実行では約 30 分前との比較になる)
- backup 鮮度: 複数 listing から最新 1 本。未来 mtime は負の年齢を作らない
"""

import datetime
import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "apps" / "openclaw" / "morning_brief.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("morning_brief_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mb = _load_module()

DAY = datetime.date(2026, 8, 22)
NOW = datetime.datetime(2026, 8, 23, 0, 30, tzinfo=mb.JST)


def commit(message, committed_at):
    return {"message": message, "committed_at": committed_at}


def merge(branch, committed_at):
    return commit("Merge pull request #519 from hikuohiku/{}\n\n本文".format(branch),
                  committed_at)


def app(name, health, sync="Synced"):
    return {"name": name, "namespace": "argocd", "sync": sync, "health": health}


def listing(namespace, mtimes, path="/mnt/backups"):
    return {
        "namespace": namespace,
        "usage": [{"pvc": "data", "bytes": 1}],
        "backup_listing": {
            "dir": path,
            "files": [
                {"name": "backup-{}.sql.gz".format(i), "bytes": 10, "mtime": m}
                for i, m in enumerate(mtimes)
            ],
        },
    }


def snap(iso, names):
    """history jsonl の 1 行 (latest.json 同形スナップショット) を最小構成で作る。"""
    return {"generated_at": iso,
            "applications": [app(n, "Healthy") for n in names]}


class CountMergesTest(unittest.TestCase):
    def test_empty_and_garbage_commits(self):
        self.assertEqual(mb.count_merges(None, DAY), {"total": 0, "project": 0})
        garbage = [None, "not-a-dict", {}, commit("x", None), merge("project/p-1", "壊れた時刻")]
        self.assertEqual(mb.count_merges(garbage, DAY), {"total": 0, "project": 0})

    def test_jst_day_window_boundaries(self):
        # JST で見ると日付が変わる UTC コミットが、JST 側の正しい日に落ちること。
        # 2026-08-22T15:30Z = JST 2026-08-23 00:30 → 翌日は窓外
        commits = [
            merge("project/p-a", "2026-08-21T23:59:59+09:00"),   # 前日 23:59 → 窓外
            merge("project/p-b", "2026-08-22T00:00:00+09:00"),   # 開始境界 → 含む
            merge("chore/x", "2026-08-22T06:50:35+09:00"),
            merge("heart/curriculum", "2026-08-22T14:42:43Z"),   # = JST 23:42 → 含む
            merge("project/p-c", "2026-08-22T15:30:00Z"),        # = JST 08-23 00:30 → 窓外
            merge("project/p-d", "2026-08-22T23:59:59+09:00"),   # 終了直前 → 含む
            commit("fix: 直接 push (merge ではない)", "2026-08-22T12:00:00+09:00"),
        ]
        self.assertEqual(
            mb.count_merges(commits, DAY),
            {"total": 4, "project": 2},
        )

    def test_non_merge_message_is_ignored_even_in_window(self):
        commits = [
            commit("Release v1.2.3 from template", "2026-08-22T12:00:00+09:00"),
            commit("", "2026-08-22T12:00:01+09:00"),
        ]
        self.assertEqual(mb.count_merges(commits, DAY), {"total": 0, "project": 0})

    def test_project_branch_requires_path_prefix(self):
        # project/ という語が branch 名の途中 (例: notaproject/x) では数えない
        commits = [
            merge("project/p-1", "2026-08-22T01:00:00+09:00"),
            merge("notaproject/x", "2026-08-22T02:00:00+09:00"),
        ]
        self.assertEqual(mb.count_merges(commits, DAY), {"total": 2, "project": 1})


class HealthChangesTest(unittest.TestCase):
    def test_no_data_is_empty_not_crash(self):
        self.assertEqual(mb.health_changes(None, None), [])
        self.assertEqual(mb.health_changes([], []), [])

    def test_detects_transitions_and_ignores_unchanged(self):
        current = [app("coder", "Degraded"), app("immich", "Healthy"),
                   app("dex", "Healthy")]
        prev = [app("coder", "Healthy"), app("immich", "Healthy"),
                app("dex", "Healthy")]
        self.assertEqual(
            mb.health_changes(current, prev),
            [{"name": "coder", "from": "Healthy", "to": "Degraded"}],
        )

    def test_added_and_removed_apps_are_changes(self):
        current = [app("new-app", "Healthy")]
        prev = [app("old-app", "Healthy")]
        self.assertEqual(
            mb.health_changes(current, prev),
            [
                {"name": "new-app", "from": None, "to": "Healthy"},
                {"name": "old-app", "from": "Healthy", "to": None},
            ],
        )

    def test_garbage_entries_are_dropped(self):
        current = [None, "x", {}, app("coder", "Degraded")]
        prev = ["y"]
        self.assertEqual(
            mb.health_changes(current, prev),
            [{"name": "coder", "from": None, "to": "Degraded"}],
        )

    def test_output_is_sorted_by_name(self):
        current = [app("zzz", "Degraded"), app("aaa", "Degraded")]
        prev = [app("zzz", "Healthy"), app("aaa", "Healthy")]
        names = [c["name"] for c in mb.health_changes(current, prev)]
        self.assertEqual(names, ["aaa", "zzz"])


class BackupFreshnessTest(unittest.TestCase):
    def test_none_when_no_listings(self):
        self.assertIsNone(mb.backup_freshness(None, now=NOW))
        self.assertIsNone(mb.backup_freshness([], now=NOW))
        self.assertIsNone(mb.backup_freshness([{"namespace": "vaultwarden",
                                                "usage": []}], now=NOW))

    def test_picks_newest_file_across_namespaces(self):
        usage = [
            listing("immich", ["2026-08-21T02:00:00Z", "2026-08-22T02:00:00Z"]),
            listing("coder", ["2026-08-20T02:00:00Z"]),
        ]
        fresh = mb.backup_freshness(usage, now=NOW)
        self.assertEqual(fresh["namespace"], "immich")
        self.assertEqual(fresh["mtime"], mb.parse_iso("2026-08-22T02:00:00Z"))
        # 08-22 02:00Z → 08-23 00:30JST (= 08-22 15:30Z) はちょうど 13.5 時間
        self.assertAlmostEqual(fresh["age_hours"], 13.5)

    def test_garbage_files_are_skipped(self):
        usage = [{"namespace": "immich",
                  "backup_listing": {"files": [None, "x", {},
                                               {"name": "a", "mtime": "壊れ"},
                                               {"name": "b", "mtime": "2026-08-22T02:00:00Z"}]}}]
        fresh = mb.backup_freshness(usage, now=NOW)
        self.assertEqual(fresh["file"], "b")

    def test_future_mtime_does_not_make_negative_age(self):
        usage = [listing("immich", ["2026-08-24T00:00:00Z"])]  # now より未来
        fresh = mb.backup_freshness(usage, now=NOW)
        self.assertEqual(fresh["age_hours"], 0.0)

    def test_now_as_iso_string(self):
        fresh = mb.backup_freshness(
            [listing("immich", ["2026-08-22T02:00:00Z"])], now="2026-08-22T03:00:00Z")
        self.assertAlmostEqual(fresh["age_hours"], 1.0)
        with self.assertRaises(ValueError):
            mb.backup_freshness([listing("immich", ["2026-08-22T02:00:00Z"])],
                                now="garbage")


class JstDayEndUtcTest(unittest.TestCase):
    def test_boundary_is_1500z_of_previous_day(self):
        # JST 2026-08-23 00:00 = 2026-08-22 15:00Z。history ファイル名が UTC 日付
        # (実測: report.py が generated_at[:10] を使う) なので、この変換が
        # 「前日の最終状態」探索の基準になる
        self.assertEqual(
            mb.jst_day_end_utc(datetime.date(2026, 8, 22)),
            datetime.datetime(2026, 8, 22, 15, 0, tzinfo=datetime.timezone.utc),
        )


class JsonlDocsTest(unittest.TestCase):
    def test_parses_lines_in_order(self):
        raw = b'{"v": 1}\n{"v": 2}\n'
        self.assertEqual(mb.jsonl_docs(raw), [{"v": 1}, {"v": 2}])

    def test_blank_broken_and_non_dict_lines_are_skipped(self):
        raw = b'\n{"a": 1}\n{"torn"\n[1, 2]\n42\n{"b": 2}\n'
        self.assertEqual(mb.jsonl_docs(raw), [{"a": 1}, {"b": 2}])

    def test_empty_or_all_broken_input(self):
        self.assertEqual(mb.jsonl_docs(b""), [])
        self.assertEqual(mb.jsonl_docs(None), [])
        self.assertEqual(mb.jsonl_docs(b"not json\n"), [])


class PrevApplicationsTest(unittest.TestCase):
    def test_picks_latest_snapshot_before_boundary_across_files(self):
        # 実データ (history/2026-08-22.jsonl は UTC 08-22 の 48 行を持ち、JST 昨日の
        # 終わり 15:00Z はその途中) を縮約した回帰。ファイル順と無関係に
        # 「境界より前の最新」を選ぶ。境界ちょうど (今日の最初の瞬間) は除外
        docs = [
            snap("2026-08-22T14:30:05Z", ["immich"]),   # 境界直前 → 採用
            snap("2026-08-22T15:00:05Z", ["after"]),    # 境界後 → 除外
            snap("2026-08-21T23:30:04Z", ["older"]),    # もっと古い
        ]
        boundary = mb.jst_day_end_utc(datetime.date(2026, 8, 22))
        result = mb.prev_applications(docs, boundary)
        self.assertEqual([a["name"] for a in result], ["immich"])

    def test_unusable_rows_are_not_candidates(self):
        # 境界前で最新の行が比較不能でも、その前の完全なスナップショットを使える
        docs = [
            {"generated_at": "2026-08-22T14:30:00Z", "applications": None},
            {"generated_at": "2026-08-22T12:00:00Z"},          # applications 無し → 対象外
            {"applications": [app("x", "Healthy")]},           # generated_at 無し → 対象外
            {"generated_at": "壊れ", "applications": []},      # 時刻壊れ → 対象外
            snap("2026-08-22T09:00:00Z", ["keeper"]),
        ]
        boundary = mb.jst_day_end_utc(datetime.date(2026, 8, 22))
        result = mb.prev_applications(docs, boundary)
        self.assertEqual([a["name"] for a in result], ["keeper"])

    def test_nothing_usable_returns_none(self):
        boundary = mb.jst_day_end_utc(datetime.date(2026, 8, 22))
        after_boundary_only = [snap("2026-08-22T15:00:05Z", ["after"])]
        self.assertIsNone(mb.prev_applications(after_boundary_only, boundary))
        self.assertIsNone(mb.prev_applications([], boundary))
        self.assertIsNone(mb.prev_applications(None, boundary))


class BriefContractTest(unittest.TestCase):
    """spec DoD (2) の 2 本柱 + 行ごとの省略規則。"""

    def test_all_sources_missing_composes_without_error(self):
        text = mb.compose_brief()
        self.assertEqual(text, "")
        self.assertEqual(mb.brief_lines(), [])

    def test_never_exceeds_three_lines(self):
        rich = dict(
            merges={"total": 50, "project": 20},
            current_apps=[app("app{}".format(i), "Degraded") for i in range(30)],
            prev_apps=[app("app{}".format(i), "Healthy") for i in range(30)],
            backup={"namespace": "immich", "age_hours": 100.0},
        )
        lines = mb.brief_lines(**rich)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(lines))
        # 全組み合わせ (各ソースの有無 2^4) でも超えないことを機械的に確認
        keys = list(rich)
        for mask in range(16):
            kwargs = {k: rich[k] for i, k in enumerate(keys) if mask >> i & 1}
            self.assertLessEqual(len(mb.brief_lines(**kwargs)), 3)

    def test_delivered_line_variants(self):
        self.assertIsNone(mb.line_delivered(None))                       # 取得失敗 → 省略
        self.assertEqual(mb.line_delivered({"total": 0, "project": 0}),
                         "納品: なし (merge 0 件)")
        line = mb.line_delivered({"total": 8, "project": 2})
        self.assertIn("プロジェクト 2 件", line)
        self.assertIn("merge 8 件", line)
        # 壊れた counter でも壊れない (project 欠損は 0 扱い)
        self.assertIn("プロジェクト 0 件", mb.line_delivered({"total": 3}))

    def test_health_line_summary_when_prev_missing(self):
        apps = [app("coder", "Degraded"), app("dex", "Healthy"), app("immich", "Healthy")]
        self.assertEqual(
            mb.line_health([], current_apps=apps, prev_available=False),
            "健全性: 3 アプリ中 1 が非 Healthy",
        )
        all_healthy = [app("coder", "Healthy")]
        self.assertEqual(
            mb.line_health([], current_apps=all_healthy, prev_available=False),
            "健全性: 1 アプリすべて Healthy",
        )
        self.assertIsNone(mb.line_health([], current_apps=None, prev_available=False))

    def test_health_line_no_change_with_prev(self):
        apps = [app("coder", "Healthy")]
        self.assertEqual(
            mb.line_health([], current_apps=apps, prev_available=True),
            "健全性: 変化なし (1 アプリ)",
        )

    def test_health_change_line_lists_names_and_folds_rest(self):
        changes = [{"name": "app{}".format(i), "from": "Healthy", "to": "Degraded"}
                   for i in range(5)]
        line = mb.line_health(changes, current_apps=[app("x", "Degraded")] * 5,
                              prev_available=True)
        self.assertTrue(line.startswith("健全性: app0 Healthy→Degraded"))
        self.assertNotIn("app3", line)
        self.assertIn("(他 2 件)", line)

    def test_health_transition_marks_missing_side(self):
        line = mb.line_health([{"name": "new-app", "from": None, "to": "Healthy"}],
                              current_apps=[app("new-app", "Healthy")],
                              prev_available=False)
        self.assertEqual(line, "健全性: new-app ?→Healthy")

    def test_missing_prev_falls_back_to_summary_not_pseudo_additions(self):
        # 前日 history が無い日は「比較不能」で summary 表示に落ちる。
        # prev=None を素通しすると全アプリが「?→X」の擬似新規出現になり、
        # 障害でも無い日に変化を誇張して見せることになる (session 3 で実測した bug)
        apps = [app("coder", "Healthy"), app("immich", "Degraded")]
        for prev in (None, []):  # 空リストも「比較できるデータ無し」と同義
            lines = mb.brief_lines(current_apps=apps, prev_apps=prev)
            health = [line for line in lines if line.startswith("健全性")]
            self.assertEqual(len(health), 1)
            self.assertEqual(health[0], "健全性: 2 アプリ中 1 が非 Healthy")
            self.assertNotIn("?→", health[0])
        all_healthy = [app("coder", "Healthy")]
        lines = mb.brief_lines(current_apps=all_healthy, prev_apps=None)
        self.assertIn("健全性: 1 アプリすべて Healthy", lines)

    def test_non_list_prev_is_treated_as_missing(self):
        # prev_apps が list 以外 (壊れた形状。fetch 側では起こらないが契約として
        # 「壊れない」) でも「?→X」の疑似追加を作らない
        apps = [app("coder", "Healthy")]
        for prev in ({"applications": []}, "garbage"):
            lines = mb.brief_lines(current_apps=apps, prev_apps=prev)
            health = [line for line in lines if line.startswith("健全性")]
            self.assertEqual(health, ["健全性: 1 アプリすべて Healthy"])

    def test_backup_line_variants(self):
        self.assertIsNone(mb.line_backup(None))
        line = mb.line_backup({"namespace": "immich", "age_hours": 13.5})
        self.assertEqual(line, "backup: immich 14時間前")  # round(13.5) は偶数丸めで 14
        stale = mb.line_backup({"namespace": "immich", "age_hours": 45.0})
        self.assertIn("45時間前", stale)
        self.assertIn("(古い)", stale)
        fresh_boundary = mb.line_backup({"namespace": "immich",
                                         "age_hours": mb.DEFAULT_STALE_HOURS})
        # 境界ちょうどは鳴らさない (> のみで古い判定。誤報より沈黙を嫌うのは逆方向:
        # ここは「毎日走っている backup が 36h 空いた」時点で十分異常なので、
        # 余裕を見て厳密超過でのみ鳴らす)
        self.assertNotIn("(古い)", fresh_boundary)
        old_days = mb.line_backup({"namespace": "immich", "age_hours": 96.0})
        self.assertIn("4日前", old_days)
        # age が壊れている行は省略 (壊れた数字を見せない)
        self.assertIsNone(mb.line_backup({"namespace": "immich", "age_hours": "x"}))


class ComposeIntegrationTest(unittest.TestCase):
    def test_realistic_shapes_compose(self):
        # latest.json / history / commits API の実shapeに寄せた通し確認
        latest_apps = [app("coder", "Healthy"), app("immich", "Healthy"),
                       app("openclaw", "Healthy")]
        prev_apps = [app("coder", "Degraded"), app("immich", "Healthy")]
        merges = {"total": 5, "project": 1}
        backup = mb.backup_freshness(
            [listing("immich", ["2026-08-22T02:00:00Z"])], now=NOW)
        text = mb.compose_brief(merges=merges, current_apps=latest_apps,
                                prev_apps=prev_apps, backup=backup)
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("納品: プロジェクト 1 件 / merge 5 件", lines[0])
        self.assertIn("coder Degraded→Healthy", lines[1])
        self.assertIn("backup: immich", lines[2])


if __name__ == "__main__":
    unittest.main()
