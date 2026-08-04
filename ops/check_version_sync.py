#!/usr/bin/env python3
"""
リポジトリ内で二重管理されているバージョン pin が揃っているか検証する。
CI (ops job) から実行する。不一致があれば非 0 で終了する。

標準ライブラリのみ（ops/validate.py と同じ方針。実行環境に何も入っていなくても動くこと。
issue #56 2026-08-04 19:53:35 の指摘: pyyaml に依存すると autopilot のサンドボックスで
手元検証できなくなる）。

新しく二重管理の pin が見つかったら PAIRS にエントリを追加する（ops/CHARTER.md T-0002）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(path: str) -> str:
    return (ROOT / path).read_text()


def extract_yaml_helm_chart_version(path: str, chart_name: str) -> str:
    """helmCharts の `- name: <chart_name>` エントリを、次の `- name:`（または末尾）までの
    範囲に絞って version を拾う。フルパースはせず、pyyaml も使わない。"""
    text = read(path)
    entry_re = re.compile(r"^\s*-\s*name:\s*(\S+)\s*$", re.MULTILINE)
    entries = list(entry_re.finditer(text))
    for i, m in enumerate(entries):
        if m.group(1) != chart_name:
            continue
        end = entries[i + 1].start() if i + 1 < len(entries) else len(text)
        block = text[m.end() : end]
        v = re.search(r"^\s*version:\s*(\S+)\s*$", block, re.MULTILINE)
        if not v:
            raise ValueError(f"{path}: helmCharts[{chart_name}] に version が見つからない")
        return v.group(1)
    raise ValueError(f"{path}: helmCharts に {chart_name} が見つからない")


def _matching_brace(text: str, open_pos: int) -> int:
    """text[open_pos] が '{' である前提で、対応する '}' の位置を返す。"""
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("対応する '}' が見つからない（brace 不一致）")


def extract_nix_helmchart_version(path: str, chart_name: str) -> str:
    """`chart = "<chart_name>";` を含む、直近の外側の attrset（brace 単位）に version 探索を
    閉じ込める。固定文字数の window ではなく構造で範囲を決める（issue #56 2026-08-04 19:53:35
    の指摘: 同じファイルに 2 つ目の HelmChart が足されても隣の version を誤って拾わないように）。
    """
    text = read(path)
    m = re.search(rf'chart\s*=\s*"{re.escape(chart_name)}"\s*;', text)
    if not m:
        raise ValueError(f"{path}: chart = \"{chart_name}\" が見つからない")

    # chart 行を含む直近の外側 '{' を、後方に向かって depth を数えて探す
    depth = 0
    open_pos = None
    for i in range(m.start() - 1, -1, -1):
        c = text[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                open_pos = i
                break
            depth -= 1
    if open_pos is None:
        raise ValueError(f"{path}: {chart_name} を囲む attrset が見つからない")

    close_pos = _matching_brace(text, open_pos)
    block = text[open_pos : close_pos + 1]

    v = re.search(r'version\s*=\s*"([^"]+)"\s*;', block)
    if not v:
        raise ValueError(f"{path}: {chart_name} と同じ attrset 内に version が見つからない")
    return v.group(1)


PAIRS = [
    {
        "name": "argo-cd chart version (T-0002)",
        "a": ("apps/argocd/kustomization.yaml", lambda: extract_yaml_helm_chart_version(
            "apps/argocd/kustomization.yaml", "argo-cd"
        )),
        "b": ("nix/images/proxmox-cloud/k3s-manifests.nix", lambda: extract_nix_helmchart_version(
            "nix/images/proxmox-cloud/k3s-manifests.nix", "argo-cd"
        )),
    },
]


def main() -> int:
    fail = False
    for pair in PAIRS:
        a_path, a_fn = pair["a"]
        b_path, b_fn = pair["b"]
        try:
            a_ver = a_fn()
            b_ver = b_fn()
        except Exception as e:
            print(f"::error::{pair['name']}: 抽出に失敗しました: {e}")
            fail = True
            continue
        if a_ver != b_ver:
            print(
                f"::error::{pair['name']}: バージョンが一致しません "
                f"({a_path}={a_ver} != {b_path}={b_ver})"
            )
            fail = True
        else:
            print(f"ok: {pair['name']} = {a_ver}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
