#!/usr/bin/env python3
"""seeds.md の『人間の鍵作業』節を機械抽出し、{id, title, age_days} の JSON を出す (P-0272)。

なぜ要るか: Mission Control の Next.js 化 (P-0088/89) で、旧 build.py 世界が
「あなたの手が要る」節 (P-0012) として担っていた依頼面が失われた。seeds.md の
『人間の鍵作業』は器からは進められない物理・認証系の仕事で、人間が見ない限り
永遠に滞留する。このツールの出力をダッシュボードが映すことで依頼面を復活させる。

抽出の考え方:

- 見出しに「人間の鍵作業」を含む節 (レベル 1-2) だけを見る。節ベースなので
  項目の増減に自動追従する (リストの手動保守が要らない)
- 節内の行のうち、行頭が `- ` かつ `T-\\d+:` に一致する行だけを項目とする。
  同一節内には旧リスト構造の名残である番号付き行 (14.〜21.) が混在しているが、
  行頭が数字の時点で弾ける。続きのインデント行も行頭条件で弾ける
- `~~` を含む行は取り消し線 = 解消済み・却下として除外する。「解消済み」の
  単一の情報源は seeds.md の編集 (取り消し線/削除) であり、state を持たない
- age_days は ops/backlog.json の created (YYYY-MM-DD) との join で出す。
  backlog に無い id・日付として解釈できない値は 0 (最も新しい扱い) —
  滞留日数不明の項目を古いふりして上位に出さないため

出力順は古い順 (age_days 降順、同点は id 昇順)。標準ライブラリのみで動く
(autopilot イメージの py 方針と同じ)。parse は引数渡しの純関数なので
unittest から今日日付を固定して叩ける。

使い方:

    python3 ops/tools/human_tasks.py [--out PATH] [--seeds PATH] [--backlog PATH]
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEEDS = REPO_ROOT / "ops" / "projects" / "seeds.md"
DEFAULT_BACKLOG = REPO_ROOT / "ops" / "backlog.json"

# 見出しレベル 1-2 のうち「人間の鍵作業」を含むものが対象の節
SECTION_HEADING_RE = re.compile(r"^#{1,2}\s+.*人間の鍵作業.*")
# 次の同レベル以上の見出しで節は終わる
NEXT_HEADING_RE = re.compile(r"^#{1,2}\s+")
# 項目は bullet + T-NNNN: だけ。番号付き行・インデント継続行は最初から一致しない
ITEM_RE = re.compile(r"^- (T-\d+):\s*(\S.*)$")
STRIKETHROUGH_MARK = "~~"


def extract_section(seeds_text: str) -> str:
    """『人間の鍵作業』節の本文を返す。節が無ければ空文字列。"""
    lines = seeds_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if SECTION_HEADING_RE.match(line):
            start = i + 1
            break
    if start is None:
        return ""
    body = []
    for line in lines[start:]:
        if NEXT_HEADING_RE.match(line):
            break
        body.append(line)
    return "\n".join(body)


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def age_days(created: str | None, today: date) -> int:
    parsed = parse_date(created)
    if parsed is None:
        return 0
    return max(0, (today - parsed).days)


def parse_human_tasks(
    seeds_text: str,
    created_by_id: dict[str, str],
    today: date,
) -> list[dict]:
    """seeds 断片 → [{id, title, age_days, created?}]。古い順 (降順)、同点は id 昇順。"""
    tasks = []
    for line in extract_section(seeds_text).splitlines():
        if STRIKETHROUGH_MARK in line:
            continue
        match = ITEM_RE.match(line)
        if not match:
            continue
        task_id, title = match.group(1), match.group(2).strip()
        created = created_by_id.get(task_id)
        entry = {"id": task_id, "title": title, "age_days": age_days(created, today)}
        # backlog に無い id では created を載せない (偽の情報源を作らない)
        if parse_date(created) is not None:
            entry["created"] = created
        tasks.append(entry)
    tasks.sort(key=lambda entry: (-entry["age_days"], entry["id"]))
    return tasks


def created_index(backlog_text: str) -> dict[str, str]:
    """ops/backlog.json のテキスト → {id: created}。壊れていれば空 (観測は空でなく沈黙しない、は呼び出し側の責め)。"""
    try:
        doc = json.loads(backlog_text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    index = {}
    for item in doc.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        item_id, created = item.get("id"), item.get("created")
        if isinstance(item_id, str) and isinstance(created, str):
            index[item_id] = created
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="seeds.md の『人間の鍵作業』節を JSON で出す")
    parser.add_argument("--out", type=Path, default=None, help="書き先。無指定なら stdout")
    parser.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS, help=f"default: {DEFAULT_SEEDS}")
    parser.add_argument("--backlog", type=Path, default=DEFAULT_BACKLOG, help=f"default: {DEFAULT_BACKLOG}")
    args = parser.parse_args(argv)

    try:
        seeds_text = args.seeds.read_text(encoding="utf-8")
    except OSError as error:
        print(f"human_tasks: seeds を読めない ({error})", file=sys.stderr)
        return 2
    backlog_text = ""
    if args.backlog.exists():
        backlog_text = args.backlog.read_text(encoding="utf-8")

    today = datetime.now(timezone.utc).date()
    payload = {
        "tasks": parse_human_tasks(seeds_text, created_index(backlog_text), today=today),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
