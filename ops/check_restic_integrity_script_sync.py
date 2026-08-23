#!/usr/bin/env python3
"""
apps/{coder,immich,syncthing,vaultwarden}/restic-integrity-cronjob.yaml に埋め込まれた
スクリプトの drift を検出する (P-0187)。CI (ops job) から実行する。

2 種類のブロックを検査する:

- restic_integrity.py: 回転選択ロジック。正本 ops/tools/restic_integrity.py と
  4 ファイルすべてが一致すること。「正本とクラスタ内実行の二重管理」を最初から
  織り込むための検査 (PROJECT.md 設計方針)
- run_integrity.py: 実行ドライバ。4 ファイルですべて一致すること
  (download_ledger.py と同じ「namespace 名以外の共有元が無い構成」のため)

ops/check_download_ledger_script_sync.py 流儀の拡張。標準ライブラリのみで動く。

単体で確認するには:
    python3 ops/check_restic_integrity_script_sync.py
"""
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_download_ledger_script_sync import extract_block_scalar  # noqa: E402

PATHS = [
    "apps/coder/restic-integrity-cronjob.yaml",
    "apps/immich/restic-integrity-cronjob.yaml",
    "apps/syncthing/restic-integrity-cronjob.yaml",
    "apps/vaultwarden/restic-integrity-cronjob.yaml",
]

CANONICAL_MODULE = "ops/tools/restic_integrity.py"


def load_blocks(key):
    blocks = []
    for path in PATHS:
        try:
            raw = extract_block_scalar(path, key)
        except Exception as e:
            print(f"::error::{path}: 抽出に失敗しました: {e}")
            sys.exit(1)
        # 抽出直後は先頭に空 1 行 (キー行自身の残り) と YAML のインデントが付く
        blocks.append((path, textwrap.dedent(raw).lstrip("\n")))
    return blocks


def main() -> int:
    failures = []

    # (1) 実行ドライバ: 4 ファイルで同一
    drivers = load_blocks("run_integrity.py")
    canonical_path, canonical_driver = drivers[0]
    mismatched = [p for p, body in drivers if body != canonical_driver]
    if mismatched:
        failures.append(
            f"run_integrity.py が {canonical_path} と一致しません: {mismatched}"
        )

    # (2) 選択ロジック: 4 ファイルで同一かつ正本とも一致
    modules = load_blocks("restic_integrity.py")
    module_path, canonical_module = modules[0]
    mismatched = [p for p, body in modules if body != canonical_module]
    if mismatched:
        failures.append(
            f"restic_integrity.py が {module_path} と一致しません: {mismatched}"
        )
    source = (ROOT / CANONICAL_MODULE).read_text()
    for path, body in modules:
        if body != source:
            failures.append(
                f"{path} の restic_integrity.py が正本 {CANONICAL_MODULE} と一致しません"
                " (埋め込みコピーを再生成してください)"
            )
            break

    if failures:
        for f in failures:
            print(f"::error::{f}")
        return 1

    print(
        f"ok: restic_integrity.py / run_integrity.py は {len(PATHS)} ファイルで一致し、"
        f"選択ロジックは正本 ({CANONICAL_MODULE}) とも一致"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
