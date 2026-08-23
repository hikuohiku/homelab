#!/usr/bin/env python3
"""
ops/tools/dashboard_smoke.py (canonical) と apps/ops-dashboard/dashboard_smoke.py
(クラスタ内の dashboard-smoke CronJob が configMapGenerator でマウントするコピー) の
内容が一致しているか検証する。CI (ops job) から実行する。

P-0193。kustomize は既定でディレクトリ外のファイル参照を禁じるため、クラスタ内実行用に
同一内容のコピーを apps/ 側に置いている (download_ledger.py の複数コピー同期と同じ
考え方だが、こちらは canonical が ops/tools/ に 1 箇所あるだけの 2 ファイル構成)。
どちらか一方だけを直す drift を機械的に検出する。標準ライブラリのみで動くこと。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CANONICAL = "ops/tools/dashboard_smoke.py"
COPY = "apps/ops-dashboard/dashboard_smoke.py"


def main() -> int:
    canonical = (ROOT / CANONICAL).read_bytes()
    copy = (ROOT / COPY).read_bytes()
    if canonical != copy:
        print(
            f"::error::{CANONICAL} と {COPY} の内容が一致しません"
            " (クラスタ内 CronJob はコピー側を実行します。必ず両方を同じ PR で直すこと)"
        )
        return 1
    print(f"ok: dashboard_smoke.py は canonical ({CANONICAL}) と一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
