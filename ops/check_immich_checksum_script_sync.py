#!/usr/bin/env python3
"""
apps/immich/checksum-cronjob.yaml の ConfigMap に埋め込まれた immich_checksum_check.py が、
判断ロジックの正 (ops/tools/immich_checksum_check.py) と一致しているか検証する。
CI (ops job) から実行する (P-0361)。

判断ロジックは ops/tools/ に 1 か所だけ存在し、CronJob が動くときはその正確な複製を
ConfigMap に埋め込んで持ち込む (単一の共有元が git にある構成)。片側だけ直して
他を直し忘れる drift を機械的に検出する (ops/check_pvc_usage_script_sync.py と同じ
考え方。標準ライブラリのみで動くこと)。
"""
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MANIFEST = "apps/immich/checksum-cronjob.yaml"
CANONICAL = "ops/tools/immich_checksum_check.py"
KEY = "immich_checksum_check.py"


def extract_block_scalar(path: str, key: str) -> str:
    """`<key>: |` の行の次行から、インデントが戻るまでのブロックスカラー中身を返す。

    `|` で終わる行の行末改行は YAML の区切りであって中身ではないため除く。
    中身の行は埋め込み時の共通インデントを持つので、呼び出し側が textwrap.dedent で
    落とす (check_pvc_usage_script_sync.py と違い、canonical ファイルと直接比較するため)。
    """
    text = (ROOT / path).read_text()
    m = re.search(rf"^([ \t]*){re.escape(key)}:\s*\|\s*$", text, re.MULTILINE)
    if not m:
        raise ValueError(f"{path}: `{key}: |` が見つからない")
    key_indent = len(m.group(1))
    start = m.end()
    if text[start : start + 1] == "\n":
        start += 1
    lines = text[start:].splitlines(keepends=True)
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
    try:
        embedded = textwrap.dedent(extract_block_scalar(MANIFEST, KEY))
    except Exception as e:
        print(f"::error::{MANIFEST}: 抽出に失敗しました: {e}")
        return 1
    canonical = (ROOT / CANONICAL).read_text()
    # YAML の | (clip) はブロックスカラー末尾に改行を 1 つ足す。Python の意味論に
    # 影響しない末尾改行の差だけを許容する (それ以外は byte 一致を要求する)
    if embedded.rstrip("\n") != canonical.rstrip("\n"):
        print(
            f"::error::{MANIFEST} に埋め込まれた immich_checksum_check.py が {CANONICAL} と一致しません"
            " (判断ロジックの修正は ops/tools/immich_checksum_check.py を直してから"
            " apps/immich/checksum-cronjob.yaml の埋め込みも更新すること)"
        )
        return 1
    print(f"ok: {MANIFEST} の埋め込みは {CANONICAL} と一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())