"""採択ゲート (ops/heart/adoptgate.py) のテスト。

**ネットワークに出ない。** clone する部分 (`run_gate`) は触らず、
「与えられたディレクトリで verify を実行する部分」(`run_verify_in`) を
使い捨ての一時ディレクトリに対して実行する。spec が要求する 4 ケース
(all_fail / 一部 pass / 存在しないコマンド / タイムアウト) を実測で覆い、
加えて `classify()` の判定順を合成レコードで固定する。
"""

import tempfile
import unittest

from ops.heart import adoptgate


def rec(cmd, **kw):
    """run_one() が返す形の合成レコード。"""
    base = {"cmd": cmd, "ok": False, "rc": 1, "timeout": False,
            "syntax_error": False, "not_found": False, "output": ""}
    base.update(kw)
    return base


class TestClassify(unittest.TestCase):
    """純関数。判定順 broken_command > some_pass > all_fail を固定する。"""

    def test_all_fail(self):
        v = adoptgate.classify([rec("test -f x"), rec("grep -q z y")])
        self.assertEqual(v["verdict"], adoptgate.ALL_FAIL)
        self.assertEqual(v["passed"], [])
        self.assertEqual(v["broken"], [])

    def test_some_pass(self):
        v = adoptgate.classify([rec("test -f x"), rec("test -d .", ok=True, rc=0)])
        self.assertEqual(v["verdict"], adoptgate.SOME_PASS)
        self.assertEqual(v["passed"], ["test -d ."])

    def test_broken_beats_all_fail(self):
        v = adoptgate.classify([rec("test -f x"), rec("no_such", not_found=True, rc=127)])
        self.assertEqual(v["verdict"], adoptgate.BROKEN_COMMAND)
        self.assertEqual(v["broken"][0]["cmd"], "no_such")

    def test_broken_beats_some_pass(self):
        """壊れたコマンドが 1 本でもあれば測定自体が信用できない。pass より優先する。"""
        v = adoptgate.classify(
            [rec("test -d .", ok=True, rc=0), rec("sleep 9", timeout=True)]
        )
        self.assertEqual(v["verdict"], adoptgate.BROKEN_COMMAND)
        self.assertEqual(v["broken"][0]["reason"], "timeout")
        self.assertEqual(v["passed"], ["test -d ."])

    def test_syntax_error_is_broken(self):
        v = adoptgate.classify([rec("if [", syntax_error=True, rc=2)])
        self.assertEqual(v["verdict"], adoptgate.BROKEN_COMMAND)
        self.assertEqual(v["broken"][0]["reason"], "syntax_error")

    def test_empty_verify_is_broken(self):
        """受入基準が無い spec は all_fail ではない (完成を宣言できる者が居ない)。"""
        v = adoptgate.classify([])
        self.assertEqual(v["verdict"], adoptgate.BROKEN_COMMAND)
        self.assertEqual(v["broken"][0]["reason"], "no_verify_commands")

    def test_describe_names_the_offenders(self):
        v = adoptgate.classify([rec("test -d .", ok=True, rc=0)])
        self.assertIn("test -d .", adoptgate.describe(v))


class TestRunVerifyIn(unittest.TestCase):
    """実測。一時ディレクトリで実際に bash を回す (ネットワークには出ない)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_all_fail(self):
        results = adoptgate.run_verify_in(
            self.dir, ["test -f nonexistent_p0015", "grep -q zzz missing_p0015.txt"]
        )
        self.assertEqual(adoptgate.classify(results)["verdict"], adoptgate.ALL_FAIL)

    def test_some_pass(self):
        results = adoptgate.run_verify_in(
            self.dir, ["test -f nonexistent_p0015", "test -d ."]
        )
        v = adoptgate.classify(results)
        self.assertEqual(v["verdict"], adoptgate.SOME_PASS)
        self.assertEqual(v["passed"], ["test -d ."])

    def test_missing_command(self):
        results = adoptgate.run_verify_in(self.dir, ["no_such_cmd_xyz_p0015"])
        self.assertEqual(results[0]["rc"], 127)
        self.assertTrue(results[0]["not_found"])
        v = adoptgate.classify(results)
        self.assertEqual(v["verdict"], adoptgate.BROKEN_COMMAND)
        self.assertEqual(v["broken"][0]["reason"], "not_found (rc=127)")

    def test_unbuilt_script_is_a_legitimate_fail(self):
        """罠: `bash <まだ作っていないスクリプト>` も rc=127 を返す。

        rc だけで broken_command に落とすと「これから作る成果物を起動する verify」を
        持つ spec がゲートで恒久的に殺される (差し戻しは終端 stalled で、同じ id は
        `_register_spec` が蘇らせない)。archive.jsonl の P-0005 / P-0006 / P-0010 が
        まさにこの形。stderr は "No such file or directory" で、
        "command not found" ではない。
        """
        results = adoptgate.run_verify_in(
            self.dir, ["bash ops/drills/not_built_yet_p0015.sh"]
        )
        self.assertEqual(results[0]["rc"], 127)
        self.assertFalse(results[0]["not_found"])
        self.assertEqual(adoptgate.classify(results)["verdict"], adoptgate.ALL_FAIL)

    def test_unbuilt_relative_script_is_a_legitimate_fail(self):
        """`./scripts/new.sh` 形式も同じ (rc=127 / No such file or directory)。"""
        results = adoptgate.run_verify_in(self.dir, ["./scripts/new_p0015.sh"])
        self.assertEqual(results[0]["rc"], 127)
        self.assertFalse(results[0]["not_found"])
        self.assertEqual(adoptgate.classify(results)["verdict"], adoptgate.ALL_FAIL)

    def test_real_spec_shape_passes_the_gate(self):
        """archive.jsonl の P-0005 の verify 列そのもの。空の clone で all_fail になること
        (= 予告に進めること)。ゲートは runner より厳しくてはいけない。"""
        results = adoptgate.run_verify_in(self.dir, [
            "bash ops/drills/immich_db_restore_drill.sh",
            "python3 ops/drills/check_drill_freshness.py immich-db-restore "
            "--max-age-days 30",
            "grep -q 'immich_db_restore_drill' docs/backup.md",
        ])
        self.assertEqual(adoptgate.classify(results)["verdict"], adoptgate.ALL_FAIL)

    def test_timeout(self):
        results = adoptgate.run_verify_in(self.dir, ["sleep 5"], timeout=1)
        self.assertTrue(results[0]["timeout"])
        self.assertFalse(results[0]["ok"])
        v = adoptgate.classify(results)
        self.assertEqual(v["verdict"], adoptgate.BROKEN_COMMAND)
        self.assertEqual(v["broken"][0]["reason"], "timeout")

    def test_syntax_error_detected_before_running(self):
        results = adoptgate.run_verify_in(self.dir, ["if ["])
        self.assertTrue(results[0]["syntax_error"])
        self.assertEqual(
            adoptgate.classify(results)["verdict"], adoptgate.BROKEN_COMMAND
        )

    def test_runtime_rc2_is_not_a_syntax_error(self):
        """罠: grep はファイルが無いときも rc=2 を返す。これは正当な「まだ無い」。
        構文エラーと混同すると、真っ当な spec が軒並み差し戻される。"""
        results = adoptgate.run_verify_in(self.dir, ["grep -q x /nonexistent_p0015"])
        self.assertEqual(results[0]["rc"], 2)
        self.assertFalse(results[0]["syntax_error"])
        self.assertEqual(adoptgate.classify(results)["verdict"], adoptgate.ALL_FAIL)

    def test_runs_in_the_given_directory(self):
        """cwd が clone 先であること (verify は repo ルート相対で書かれている)。"""
        (open(f"{self.dir}/marker_p0015", "w")).close()
        results = adoptgate.run_verify_in(self.dir, ["test -f marker_p0015"])
        self.assertTrue(results[0]["ok"])

    def test_overall_timeout_skips_the_rest(self):
        """全体上限を超えたら残りは測らない。測っていないものを fail と記録しない。"""
        ticks = iter([0, 0, 999, 999])
        results = adoptgate.run_verify_in(
            self.dir, ["test -d .", "test -d ."],
            overall_timeout=10, clock=lambda: next(ticks),
        )
        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[1]["timeout"])
        self.assertIsNone(results[1]["rc"])
        self.assertEqual(
            adoptgate.classify(results)["verdict"], adoptgate.BROKEN_COMMAND
        )


if __name__ == "__main__":
    unittest.main()
