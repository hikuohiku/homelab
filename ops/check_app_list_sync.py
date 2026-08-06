#!/usr/bin/env python3
"""
apps/kustomization.yaml の resources と、人間向けドキュメント（CLAUDE.md /
apps/README.md）のアプリ一覧が乖離していないか検証する。CI (ops job) から実行する。

同型の drift（T-0057/T-0089/T-0094/T-0134）を4回起票・手直ししており、
T-0156 でようやく機械検証を足す。apps/kustomization.yaml の resources から
app ディレクトリ名を機械的に抽出し、各ドキュメントの本文にその名前が
一度も出現しない場合だけを検出する（自由記述の順序・表現までは強制しない）。
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML missing")

ROOT = Path(__file__).resolve().parent.parent

# アプリ名がドキュメント中に出現していることを要求するファイル。
DOC_PATHS = [
    "CLAUDE.md",
    "apps/README.md",
]

RESOURCE_RE = re.compile(r"^([a-zA-Z0-9_-]+)/application\.yaml$")


def extract_app_names(kustomization_path: Path) -> list:
    data = yaml.safe_load(kustomization_path.read_text())
    resources = data.get("resources", [])
    names = []
    for r in resources:
        m = RESOURCE_RE.match(r.strip())
        if m:
            names.append(m.group(1))
    return names


def main() -> int:
    kustomization_path = ROOT / "apps" / "kustomization.yaml"
    if not kustomization_path.exists():
        print(f"::error::{kustomization_path} が見つからない", file=sys.stderr)
        return 1

    app_names = extract_app_names(kustomization_path)
    if not app_names:
        print("::error::apps/kustomization.yaml から app 名を1つも抽出できなかった", file=sys.stderr)
        return 1

    errors = []
    for rel_path in DOC_PATHS:
        doc_path = ROOT / rel_path
        if not doc_path.exists():
            errors.append(f"{rel_path}: ファイルが存在しない")
            continue
        text = doc_path.read_text()
        for app in app_names:
            if app not in text:
                errors.append(f"{rel_path}: app `{app}` への言及が無い")

    if errors:
        for e in errors:
            print(f"::error::{e}")
        print(
            f"\n{len(errors)} 件の drift。apps/kustomization.yaml の app: {sorted(app_names)}",
            file=sys.stderr,
        )
        return 1

    print(f"ok: app {len(app_names)} 件、ドキュメント {len(DOC_PATHS)} 件を確認")
    return 0


if __name__ == "__main__":
    sys.exit(main())
