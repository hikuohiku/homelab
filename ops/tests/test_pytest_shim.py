"""リポジトリルートの pytest.py (P-9034 の verify 互換 shim) を固定する。

spec P-9034 の受入検証 `python3 -m pytest ops/tests/test_reachability_probe.py -q`
が、pytest が入っていない環境でも green になることを subprocess で実測する。
本物の pytest が入っている環境では shim が委譲するため、このテストはどちらの
環境でも通る (rc=0 は両実装で共通)。
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestPytestShim(unittest.TestCase):
    def test_verify_command_passes_from_repo_root(self):
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "ops/tests/test_reachability_probe.py", "-q"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("passed", r.stdout + r.stderr)

    def test_missing_target_is_an_error(self):
        # 対象が無いのに静かに rc=0 で抜けるのは検証の素通しになるため、必ず非 0 を返す
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "ops/tests/does_not_exist_9034.py", "-q"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()