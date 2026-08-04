#!/usr/bin/env python3
"""apps/ root Application は syncPolicy.automated.prune: true で動いている。
つまり `kustomize build --enable-helm` の出力からオブジェクトが消えると、
次の ArgoCD sync でクラスタからも消える（PVC ならバックエンドの実データも道連れ）。

このスクリプトは PR の base と head、両方の worktree で全 kustomization.yaml を
render し、(kind, namespace, name) の集合を比較する。head 側で消えているものが
あれば、PR 本文に明示的な `allow-delete:` 行が無い限り失敗する（fail-closed）。

CI (manifest-diff job) から呼ぶ。標準ライブラリではなく PyYAML を使う
（.github/workflows/ci.yml の他ジョブも同様に ubuntu-latest 同梱の PyYAML に依存している。
このチェックは kustomize/helm が無いと render 自体ができず、autopilot のサンドボックスでは
そもそも手元検証できないため、stdlib のみに絞る意味が無い）。

意図的にオブジェクトを消す PR では、本文に次の形式で許可を書く:
    allow-delete: <Kind>/<namespace-or-->/<name>
例: allow-delete: PersistentVolumeClaim/immich/immich-library
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML missing")

ALLOW_RE = re.compile(r"^allow-delete:\s*(\S+)/(\S+)/(\S+)\s*$", re.MULTILINE)


def find_kustomization_dirs(root: Path) -> list[str]:
    """apps/ 配下の kustomization.yaml を持つディレクトリを、repo root からの
    相対パスで返す（root をまたいで base/head を比較できるようにするため）。"""
    apps = root / "apps"
    if not apps.exists():
        return []
    dirs = sorted(p.parent.relative_to(root).as_posix() for p in apps.rglob("kustomization.yaml"))
    return dirs


def render_objects(root: Path, rel_dir: str) -> set[tuple[str, str, str]]:
    """1 つの kustomization ディレクトリを render し、(kind, namespace, name) の集合を返す。
    render に失敗したら例外を投げて呼び出し元で fail-closed に倒す。"""
    d = root / rel_dir
    result = subprocess.run(
        ["kustomize", "build", "--enable-helm", str(d)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kustomize build --enable-helm {d} failed:\n{result.stderr}")

    objects: set[tuple[str, str, str]] = set()
    for doc in yaml.safe_load_all(result.stdout):
        if not doc or "kind" not in doc:
            continue
        metadata = doc.get("metadata") or {}
        name = metadata.get("name")
        if not name:
            continue
        namespace = metadata.get("namespace") or "-"
        objects.add((doc["kind"], namespace, name))
    return objects


def render_all(root: Path, dirs: list[str]) -> dict[str, set[tuple[str, str, str]]]:
    rendered = {}
    for rel_dir in dirs:
        if not (root / rel_dir).exists():
            # このディレクトリは反対側の ref にしか存在しない（新規追加 or 完全削除）。
            # 完全削除の場合でも、削除されたオブジェクトは反対側の render で捕捉される。
            rendered[rel_dir] = set()
            continue
        rendered[rel_dir] = render_objects(root, rel_dir)
    return rendered


def parse_allowed(pr_body: str) -> set[tuple[str, str, str]]:
    return {(k, ns, n) for k, ns, n in ALLOW_RE.findall(pr_body or "")}


def diff_and_check(
    base: dict[str, set[tuple[str, str, str]]],
    head: dict[str, set[tuple[str, str, str]]],
    allowed: set[tuple[str, str, str]],
) -> list[str]:
    """戻り値: 許可されていない削除の説明文リスト（空なら問題なし）"""
    violations = []
    all_dirs = sorted(set(base) | set(head))
    for rel_dir in all_dirs:
        removed = base.get(rel_dir, set()) - head.get(rel_dir, set())
        for obj in sorted(removed):
            if obj in allowed:
                print(f"allowed deletion: {rel_dir}: {obj[0]}/{obj[1]}/{obj[2]}")
                continue
            violations.append(f"{rel_dir}: {obj[0]}/{obj[1]}/{obj[2]}")
    return violations


def main() -> int:
    if len(sys.argv) != 4:
        sys.exit(f"usage: {sys.argv[0]} <base-worktree> <head-worktree> <pr-body-file|->")

    base_root = Path(sys.argv[1]).resolve()
    head_root = Path(sys.argv[2]).resolve()
    body_arg = sys.argv[3]
    pr_body = sys.stdin.read() if body_arg == "-" else Path(body_arg).read_text()

    dirs = sorted(set(find_kustomization_dirs(base_root)) | set(find_kustomization_dirs(head_root)))

    try:
        base_objects = render_all(base_root, dirs)
        head_objects = render_all(head_root, dirs)
    except RuntimeError as e:
        print(f"::error::{e}")
        return 1

    allowed = parse_allowed(pr_body)
    violations = diff_and_check(base_objects, head_objects, allowed)

    if violations:
        print("::error::head で消えているオブジェクトがあります（apps root Application は prune: true）")
        for v in violations:
            print(f"::error::  {v}")
        print(
            "\n意図的な削除なら PR 本文に次の形式で明示してください:\n"
            "  allow-delete: <Kind>/<namespace-or-->/<name>"
        )
        return 1

    print("no unexplained object deletions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
