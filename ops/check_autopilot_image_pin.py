#!/usr/bin/env python3
"""apps/autopilot/deployment.yaml は images/autopilot/Dockerfile をビルドした
ghcr.io/hikuohiku/homelab-autopilot の digest を手動 pin している（.github/workflows/
build-autopilot-image.yml が main への push のたびに新しいイメージを push するが、
Deployment 側の digest はコメントで「手で更新する」運用になっている）。

images/autopilot/** を変更した PR で apps/autopilot/deployment.yaml の image 行
（digest）を変え忘れると、ワークフローは新しいイメージを push するのに Deployment は
古いイメージを参照し続け、クラスタが古い autopilot のまま気づかれずに動く。

このスクリプトは PR の base と head、両方の worktree を比較し、
images/autopilot/ 配下が変化しているのに apps/autopilot/deployment.yaml の
image 行が変化していなければ失敗する（fail-closed）。images/autopilot/ が
変化していなければ何もしない。

CI (manifest-diff job) から呼ぶ。check_manifest_deletions.py と同じく、
base/head 両方の worktree が既にチェックアウトされている前提。標準ライブラリのみ。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DEPLOYMENT_REL = "apps/autopilot/deployment.yaml"
IMAGE_DIR_REL = "images/autopilot"
IMAGE_LINE_RE = re.compile(r"^\s*image:\s*\S+\s*$", re.MULTILINE)


def snapshot_dir(root: Path, rel_dir: str) -> dict[str, bytes]:
    """rel_dir 配下の全ファイルを {相対パス: 中身} で返す。存在しなければ空。"""
    d = root / rel_dir
    if not d.exists():
        return {}
    return {
        p.relative_to(d).as_posix(): p.read_bytes()
        for p in sorted(d.rglob("*"))
        if p.is_file()
    }


def extract_image_line(root: Path) -> str:
    path = root / DEPLOYMENT_REL
    if not path.exists():
        raise ValueError(f"{DEPLOYMENT_REL} が見つからない ({root})")
    text = path.read_text()
    m = IMAGE_LINE_RE.search(text)
    if not m:
        raise ValueError(f"{DEPLOYMENT_REL}: image: 行が見つからない")
    return m.group(0).strip()


def main() -> int:
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <base-worktree> <head-worktree>")

    base_root = Path(sys.argv[1]).resolve()
    head_root = Path(sys.argv[2]).resolve()

    base_images = snapshot_dir(base_root, IMAGE_DIR_REL)
    head_images = snapshot_dir(head_root, IMAGE_DIR_REL)

    if base_images == head_images:
        print(f"{IMAGE_DIR_REL} に変更なし、digest 変更チェックは対象外")
        return 0

    try:
        base_image_line = extract_image_line(base_root)
        head_image_line = extract_image_line(head_root)
    except ValueError as e:
        print(f"::error::{e}")
        return 1

    if base_image_line == head_image_line:
        print(
            f"::error::{IMAGE_DIR_REL} が変更されていますが、"
            f"{DEPLOYMENT_REL} の image 行（digest）が変わっていません。"
        )
        print(
            f"::error::  base: {base_image_line}\n::error::  head: {head_image_line}"
        )
        print(
            "\nこの PR がマージされ build-autopilot-image.yml が新しい digest を push した後、"
            f"{DEPLOYMENT_REL} の image 行をその digest に更新する PR を別途出してください。"
        )
        return 1

    print(f"{IMAGE_DIR_REL} の変更に合わせて {DEPLOYMENT_REL} の digest も更新されています")
    return 0


if __name__ == "__main__":
    sys.exit(main())
