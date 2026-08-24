"""rules.json の checksum.mismatch_threshold と CronJob の MISMATCH_THRESHOLD env の
同期配線 (P-0361 DoD 3) を固定する。

rules.json が単一の情報源で、CronJob は実行時に env で受け取る (rules.json を
読まない)。手で揃える運用は腐るため、ops/check_version_sync.py の GROUPS が
機械的に同期を検査する — ここではその検査が実際に両者を突き合わせることを、
GROUPS エントリと抽出関数を直接呼んで固定する。
"""

import unittest

import ops.check_version_sync as cvs
from ops.tools import immich_checksum_check as icc


class ChecksumThresholdSyncTest(unittest.TestCase):
    def test_rules_json_and_cronjob_env_agree(self):
        """rules.json の宣言値と CronJob の env 値が一致すること (配線の本体)。"""
        from_rules = cvs.extract_json_nested_value(
            "ops/rules.json", "checksum", "mismatch_threshold"
        )
        from_manifest = cvs.extract_env_value(
            "apps/immich/checksum-cronjob.yaml", "MISMATCH_THRESHOLD"
        )
        self.assertEqual(from_rules, from_manifest)
        self.assertEqual(from_rules, "1")

    def test_env_is_wired_to_runner(self):
        """checksum_runner.py が env MISMATCH_THRESHOLD を読んで build_report に渡すこと。"""
        text = (cvs.ROOT / "apps" / "immich" / "checksum-cronjob.yaml").read_text()
        self.assertIn("MISMATCH_THRESHOLD", text)
        self.assertIn('os.environ.get("MISMATCH_THRESHOLD")', text)

    def test_group_is_registered_in_groups(self):
        """GROUPS にこの配線のエントリが存在すること (足しただけで忘れない)。"""
        names = [g["name"] for g in cvs.GROUPS]
        self.assertTrue(
            any("checksum.mismatch_threshold" in n for n in names),
            f"GROUPS に checksum 閾値のエントリが無い: {names}",
        )

    def test_threshold_is_positive_int(self):
        """rules.json の閾値は icc.normalize_threshold が受け付ける形 (非負 int) であること。"""
        value = int(cvs.extract_json_nested_value(
            "ops/rules.json", "checksum", "mismatch_threshold"
        ))
        self.assertEqual(icc.normalize_threshold(value), value)


if __name__ == "__main__":
    unittest.main()