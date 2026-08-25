#!/usr/bin/env python3
"""
ops/tools/node_saturation.py (canonical) と
apps/ops-health-reporter/node_saturation.py (クラスタ内の reporter CronJob が
configMapGenerator で /scripts にマウントするコピー) の内容が一致しているか検証する。
CI (ops job) から実行する。

P-9037。kustomize は既定でディレクトリ外のファイル参照を禁じるため、クラスタ内実行用に
同一内容のコピーを apps/ 側に置いている (dashboard_smoke.py の canonical/copy 構成と同じ
考え方)。どちらか一方だけを直す drift を機械的に検出する。標準ライブラリのみで動くこと。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CANONICAL = "ops/tools/node_saturation.py"
COPY = "apps/ops-health-reporter/node_saturation.py"


def main() -> int:
    canonical = (ROOT / CANONICAL).read_bytes()
    copy = (ROOT / COPY).read_bytes()
    if canonical != copy:
        print(
            f"::error::{CANONICAL} と {COPY} の内容が一致しません"
            " (クラスタ内 reporter はコピー側を import します。必ず両方を同じ PR で直すこと)"
        )
        return 1
    print(f"ok: node_saturation.py は canonical ({CANONICAL}) と一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())