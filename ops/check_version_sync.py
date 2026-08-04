#!/usr/bin/env python3
"""
リポジトリ内で二重管理されているバージョン pin が揃っているか検証する。
CI (ops job) から実行する。不一致があれば非 0 で終了する。

新しく二重管理の pin が見つかったら PAIRS にエントリを追加する（ops/CHARTER.md T-0002）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(path: str) -> str:
    return (ROOT / path).read_text()


def extract_yaml_helm_chart_version(path: str, chart_name: str) -> str:
    import yaml

    doc = yaml.safe_load(read(path))
    for chart in doc.get("helmCharts", []):
        if chart.get("name") == chart_name:
            return str(chart["version"])
    raise ValueError(f"{path}: helmCharts に {chart_name} が見つからない")


def extract_nix_helmchart_version(path: str, chart_name: str) -> str:
    text = read(path)
    m = re.search(rf'chart\s*=\s*"{re.escape(chart_name)}"\s*;', text)
    if not m:
        raise ValueError(f"{path}: chart = \"{chart_name}\" が見つからない")
    rest = text[m.end() : m.end() + 500]
    v = re.search(r'version\s*=\s*"([^"]+)"\s*;', rest)
    if not v:
        raise ValueError(f"{path}: {chart_name} の直後に version が見つからない")
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
