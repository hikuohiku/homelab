#!/usr/bin/env python3
"""
apps/{immich,vaultwarden,coder}/pvc-usage-cronjob.yaml に埋め込まれた pvc_usage.py
スクリプトが3ファイルとも一致しているか検証する。CI (ops job) から実行する。

T-0080 でこの3ファイルに同一のスクリプトをコピーして持ち込んだ（1ファイルにまとめる
共有元が無い構成のため）。今後どれか1つだけを直して他を直し忘れる drift を機械的に
検出する（ops/check_version_sync.py と同じ考え方。標準ライブラリのみで動くこと）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATHS = [
    "apps/immich/pvc-usage-cronjob.yaml",
    "apps/vaultwarden/pvc-usage-cronjob.yaml",
    "apps/coder/pvc-usage-cronjob.yaml",
]


def extract_block_scalar(path: str, key: str) -> str:
    """`<key>: |` の行から、その行より深いインデントが続く範囲（ブロックスカラーの中身）を
    そのまま返す。インデントが戻る最初の行（次の兄弟キーや `---` 区切り）で打ち切る。
    """
    text = (ROOT / path).read_text()
    m = re.search(rf"^([ \t]*){re.escape(key)}:\s*\|\s*$", text, re.MULTILINE)
    if not m:
        raise ValueError(f"{path}: `{key}: |` が見つからない")
    key_indent = len(m.group(1))
    lines = text[m.end() :].splitlines(keepends=True)
    body = []
    for line in lines:
        stripped = line.strip("\n")
        if stripped == "":
            body.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= key_indent:
            break
        body.append(line)
    if not body:
        raise ValueError(f"{path}: `{key}: |` の中身が空")
    return "".join(body)


def main() -> int:
    scripts = []
    for path in PATHS:
        try:
            scripts.append((path, extract_block_scalar(path, "pvc_usage.py")))
        except Exception as e:
            print(f"::error::{path}: 抽出に失敗しました: {e}")
            return 1

    canonical_path, canonical = scripts[0]
    mismatches = [path for path, body in scripts if body != canonical]
    if mismatches:
        print(
            f"::error::pvc_usage.py が {canonical_path} と一致しません: {mismatches}"
            " (apps/{immich,vaultwarden,coder}/pvc-usage-cronjob.yaml の埋め込みスクリプトは"
            " 全て同一である必要があります)"
        )
        return 1

    print(f"ok: pvc_usage.py は {len(scripts)} ファイルで一致 ({canonical_path} が正)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
