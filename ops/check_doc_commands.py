#!/usr/bin/env python3
"""
ドキュメントが案内する `just <recipe>` が justfile に実在するか検証する。CI (ops job) から実行する。

T-0095（apps/README.md が存在しない `just inject-secrets` を案内していた）と同型の drift は、
既存の CI (check_version_sync.py / validate.py) では検出できない。ドキュメント内の
`` `just <recipe>` `` を機械的に抜き出し、justfile の実際の recipe 名と突き合わせる。
標準ライブラリのみで動く。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ドキュメントとして扱うファイル。ops/journal/ は起動ごとの履歴記録であり、
# 当時実在した recipe が後で改名/削除されても「事実の記録」として直さないため対象外。
DOC_PATHS = [
    "README.md",
    "CLAUDE.md",
    "Maintenance.md",
    "Plans.md",
    "apps/README.md",
    "ops/CHARTER.md",
    "ops/VISION.md",
    *sorted(str(p.relative_to(ROOT)) for p in ROOT.glob("docs/*.md")),
    *sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(".claude/commands/*.md")),
]

# `just <recipe>` の出現を拾う。バッククォート越しでもそうでなくても検出する。
# recipe 名だけを group(1) で取る（引数・文言はここでは見ない）。
JUST_CALL_RE = re.compile(r"\bjust\s+([a-zA-Z_][a-zA-Z0-9_-]*)")

# justfile の recipe 定義行。`name param1 param2:` の形で、変数代入 (`name := value`) とは
# `:=` の有無で区別する。
RECIPE_DEF_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)(?:\s+[a-zA-Z0-9_+*=\"'-]+)*\s*:(?!=)")


def extract_recipe_names(justfile_text: str) -> set:
    names = set()
    for line in justfile_text.splitlines():
        if line.startswith((" ", "\t", "#", "@")):
            continue
        m = RECIPE_DEF_RE.match(line)
        if m:
            names.add(m.group(1))
    return names


def extract_doc_calls(doc_path: Path) -> list:
    text = doc_path.read_text()
    calls = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in JUST_CALL_RE.finditer(line):
            recipe = m.group(1)
            if recipe == "*":
                # `just *` はワイルドカードの案内（CLAUDE.md の許可リスト表記）であり、
                # 特定の recipe を指していない
                continue
            calls.append((lineno, recipe))
    return calls


def main() -> int:
    justfile_path = ROOT / "justfile"
    if not justfile_path.exists():
        print(f"::error::justfile が見つからない: {justfile_path}", file=sys.stderr)
        return 1
    recipes = extract_recipe_names(justfile_path.read_text())
    if not recipes:
        print("::error::justfile から recipe を1つも抽出できなかった", file=sys.stderr)
        return 1

    errors = []
    for rel_path in DOC_PATHS:
        doc_path = ROOT / rel_path
        if not doc_path.exists():
            continue
        for lineno, recipe in extract_doc_calls(doc_path):
            if recipe not in recipes:
                errors.append(f"{rel_path}:{lineno}: `just {recipe}` は justfile に存在しない")

    if errors:
        for e in errors:
            print(f"::error::{e}")
        print(f"\n{len(errors)} 件の drift。justfile の recipe: {sorted(recipes)}", file=sys.stderr)
        return 1

    print(f"ok: justfile recipe {len(recipes)} 件、ドキュメント {len(DOC_PATHS)} 件を確認")
    return 0


if __name__ == "__main__":
    sys.exit(main())
