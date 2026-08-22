#!/usr/bin/env python3
"""
apps/restic-check/restic_check_runner.py が ops/restic_check_runner.py と
完全一致しているか検証する。CI (ops job) から実行する。

P-0102 で、判定ロジックの単一情報源 (ops/ 側。ops/tests/test_restic_check_runner.py
が CI で守る) を restic-check CronJob の ConfigMap へ載せるためにコピーした。
kustomize の load restrictor が kustomization 配下外のファイルを参照できないため
コピーという形を取っている (ops/check_pvc_usage_script_sync.py が守る pvc_usage.py
の 3 コピーと同じ構図)。今後どちらか一方だけ直す drift を機械的に検出する。
標準ライブラリのみで動くこと。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CANONICAL = ROOT / "ops" / "restic_check_runner.py"
DEPLOYED = ROOT / "apps" / "restic-check" / "restic_check_runner.py"


def main() -> int:
    for path in (CANONICAL, DEPLOYED):
        if not path.exists():
            print(f"::error::{path} が存在しない")
            return 1
    canonical = CANONICAL.read_text()
    deployed = DEPLOYED.read_text()
    if canonical != deployed:
        print(
            "::error::apps/restic-check/restic_check_runner.py が "
            "ops/restic_check_runner.py と一致しません "
            "(ops 側が正。判定ロジックの変更は ops 側に加え、テストを通してから"
            " apps 側へコピーしてください)"
        )
        return 1
    print(f"ok: restic_check_runner.py は一致 ({CANONICAL} が正)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
