#!/usr/bin/env python3
"""issue #56 へコメントを投稿する小さなラッパー（標準ライブラリのみ）。

autopilot が issue #56 に書き込む際の credential（`AUTOPILOT_GITHUB_TOKEN`）は
リポジトリ所有者本人の個人アカウント上の PAT で、GitHub 上は人間本人と同じ
author として記録される。そのため次の起動の取り込み（ops/CHARTER.md §6 手順2）が
author では人間と autopilot を区別できず、autopilot 自身の投稿を「人間からの
新規フィードバック」として ops/feedback.json に再取り込みしてしまっていた
（T-0159）。

対処として、autopilot が投稿する本文には必ず末尾にマーカー（MARKER）を付ける。
取り込み側（CHARTER §6 手順2）はこのマーカーを含むコメントをスキップする。
CHARTER §5.5 に書かれていた「コメント投稿は curl で直接 POST する」は、
このスクリプト経由に統一する（マーカー付与漏れを防ぐため）。

Usage:
    python3 ops/post_issue_comment.py <body-file>
    echo "本文" | python3 ops/post_issue_comment.py -
    python3 ops/post_issue_comment.py <body-file> --dry-run   # 送信せず本文だけ確認する
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

REPO = "hikuohiku/homelab"
DEFAULT_ISSUE = 56
MARKER = "<!-- autopilot:self-posted -->"


def build_body(raw: str) -> str:
    """本文にマーカーが無ければ付与する。既に付いていれば二重に付けない。"""
    if MARKER in raw:
        return raw
    return raw.rstrip() + "\n\n" + MARKER


def post_comment(issue: int, body: str, token: str) -> dict:
    url = f"https://api.github.com/repos/{REPO}/issues/{issue}/comments"
    data = json.dumps({"body": body}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("body_file", help="投稿する本文が入ったファイル。- で標準入力")
    parser.add_argument("--issue", type=int, default=DEFAULT_ISSUE)
    parser.add_argument("--dry-run", action="store_true", help="送信せず、マーカー付与後の本文を標準出力に表示するだけ")
    args = parser.parse_args()

    raw = sys.stdin.read() if args.body_file == "-" else open(args.body_file, encoding="utf-8").read()
    if not raw.strip():
        print("error: 本文が空", file=sys.stderr)
        return 1

    body = build_body(raw)

    if args.dry_run:
        print(body)
        return 0

    token = os.environ.get("AUTOPILOT_GITHUB_TOKEN")
    if not token:
        print("error: AUTOPILOT_GITHUB_TOKEN が設定されていない", file=sys.stderr)
        return 1

    try:
        result = post_comment(args.issue, body, token)
    except urllib.error.HTTPError as e:
        print(f"error: HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1

    print(result.get("html_url", result.get("id")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
