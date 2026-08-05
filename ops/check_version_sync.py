#!/usr/bin/env python3
"""
リポジトリ内で二重管理されているバージョン pin が揃っているか検証する。
CI (ops job) から実行する。不一致があれば非 0 で終了する。

標準ライブラリのみ（ops/validate.py と同じ方針。実行環境に何も入っていなくても動くこと。
issue #56 2026-08-04 19:53:35 の指摘: pyyaml に依存すると autopilot のサンドボックスで
手元検証できなくなる）。

新しく二重管理の pin が見つかったら GROUPS にエントリを追加する（ops/CHARTER.md T-0002）。
ops/inventory.json で "mirrors" を持つ target は、ここにも対応するエントリを持つこと（T-0051）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(path: str) -> str:
    return (ROOT / path).read_text()


def extract_image_tag_all(path: str, image_prefix: str) -> str:
    """`<image_prefix><tag>` を全箇所拾い、ファイル内で全て一致することを確認して返す。
    1 ファイルに同じイメージが複数回（例: backup CronJob と retention CronJob）出てくる
    場合でも、ファイル内の割れをそのまま見逃さない（extract_all_action_tags と同じ考え方）。"""
    text = read(path)
    matches = re.findall(rf"{re.escape(image_prefix)}(\S+)", text)
    if not matches:
        raise ValueError(f"{path}: {image_prefix} が見つからない")
    uniq = sorted(set(matches))
    if len(uniq) > 1:
        raise ValueError(f"{path}: {image_prefix} のタグがファイル内で不一致 ({uniq})")
    return uniq[0]


def extract_yaml_top_level_block(path: str, top_key: str) -> str:
    """トップレベルキー（0-indent の `<top_key>:`）から、次のトップレベルキーまたは末尾までの
    範囲をブロックとして返す。同じファイル内に複数の image/tag ペアがあっても、無関係な
    トップレベルキーのものを誤って拾わないための構造的な境界（nix 側の brace 境界と同じ考え方）。
    """
    text = read(path)
    top_re = re.compile(rf"^{re.escape(top_key)}:\s*$", re.MULTILINE)
    m = top_re.search(text)
    if not m:
        raise ValueError(f"{path}: トップレベルキー {top_key}: が見つからない")
    next_top_re = re.compile(r"^\S.*:\s*$", re.MULTILINE)
    n = next_top_re.search(text, m.end())
    end = n.start() if n else len(text)
    return text[m.end() : end]


def extract_image_tag(path: str, image_prefix: str) -> str:
    """`<image_prefix><tag>` の形（例: `busybox:1.38.0`）を 1 箇所だけ含む前提で tag を拾う。"""
    text = read(path)
    m = re.search(rf"{re.escape(image_prefix)}(\S+)", text)
    if not m:
        raise ValueError(f"{path}: {image_prefix} が見つからない")
    return m.group(1)


def extract_tag_in_block(path: str, top_key: str) -> str:
    """`extract_yaml_top_level_block` で絞った範囲内の最初の `tag:` を拾う。"""
    block = extract_yaml_top_level_block(path, top_key)
    v = re.search(r"^\s*tag:\s*(\S+)\s*$", block, re.MULTILINE)
    if not v:
        raise ValueError(f"{path}: トップレベルキー {top_key}: の範囲内に tag: が見つからない")
    return v.group(1)


def extract_yaml_job_block(path: str, job_name: str) -> str:
    """`jobs:` 配下のトップレベルジョブ（2-indent の `<job_name>:`）から、次の同 indent の
    ジョブ名または末尾までの範囲をブロックとして返す。`extract_yaml_top_level_block` と同じ
    「構造で範囲を決める」考え方を、0-indent ではなく 2-indent のジョブ境界に適用したもの。
    """
    text = read(path)
    job_re = re.compile(rf"^  {re.escape(job_name)}:\s*$", re.MULTILINE)
    m = job_re.search(text)
    if not m:
        raise ValueError(f"{path}: ジョブ {job_name}: が見つからない")
    next_job_re = re.compile(r"^  \S.*:\s*$", re.MULTILINE)
    n = next_job_re.search(text, m.end())
    end = n.start() if n else len(text)
    return text[m.end() : end]


def extract_helm_setup_version(path: str, job_name: str) -> str:
    """指定したジョブ内の `azure/setup-helm@vX` ステップが `with: version:` で指定する
    helm バイナリのバージョンを拾う（T-0090。Action 自体の pin とは別物）。"""
    block = extract_yaml_job_block(path, job_name)
    m = re.search(r"azure/setup-helm@\S+\s*\n\s*with:\s*\n\s*version:\s*(\S+)", block)
    if not m:
        raise ValueError(f"{path}: ジョブ {job_name} 内に azure/setup-helm の version: が見つからない")
    return m.group(1)


def extract_kustomize_download_version(path: str, job_name: str) -> str:
    """指定したジョブ内で curl ダウンロードしている kustomize バイナリのバージョンを拾う
    （T-0090。GitHub Action ではなく GitHub Releases から直接ダウンロードしている）。"""
    block = extract_yaml_job_block(path, job_name)
    m = re.search(r"kustomize%2Fv(\S+?)/kustomize_v\S+?_linux_amd64\.tar\.gz", block)
    if not m:
        raise ValueError(f"{path}: ジョブ {job_name} 内に kustomize ダウンロード URL の version が見つからない")
    return m.group(1)


def extract_all_action_tags(path: str, action_name: str) -> str:
    """`uses: <action_name>@<tag>` を全箇所拾い、ファイル内で全て一致することを確認して返す。
    1 ファイルに同じ Action が複数回 (`uses:`) 出てくる場合（例: ci.yml の複数 job）でも、
    ファイル内の割れをそのまま見逃さない。"""
    text = read(path)
    matches = re.findall(rf"{re.escape(action_name)}@(\S+)", text)
    if not matches:
        raise ValueError(f"{path}: {action_name}@ が見つからない")
    uniq = sorted(set(matches))
    if len(uniq) > 1:
        raise ValueError(f"{path}: {action_name} のタグがファイル内で不一致 ({uniq})")
    return uniq[0]


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


# 各エントリの "targets" は 2 件以上の (path, 抽出関数) のリスト。全 target のバージョンが
# 一致することを検証する（2 件限定の PAIRS ではなく N 件のグループとして扱う。
# ops/inventory.json の各 target の "mirrors" と 1:1 で対応させる — T-0051）。
GROUPS = [
    {
        "name": "argo-cd chart version (T-0002, inventory: argocd-chart)",
        "targets": [
            ("apps/argocd/kustomization.yaml", lambda: extract_yaml_helm_chart_version(
                "apps/argocd/kustomization.yaml", "argo-cd"
            )),
            ("nix/images/proxmox-cloud/k3s-manifests.nix", lambda: extract_nix_helmchart_version(
                "nix/images/proxmox-cloud/k3s-manifests.nix", "argo-cd"
            )),
        ],
    },
    {
        "name": "busybox initContainer tag (T-0003, inventory: busybox)",
        "targets": [
            ("apps/vaultwarden/deployment.yaml", lambda: extract_image_tag(
                "apps/vaultwarden/deployment.yaml", "busybox:"
            )),
            ("apps/coder/postgres.yaml", lambda: extract_image_tag(
                "apps/coder/postgres.yaml", "busybox:"
            )),
            ("apps/immich/postgres.yaml", lambda: extract_image_tag(
                "apps/immich/postgres.yaml", "busybox:"
            )),
        ],
    },
    {
        "name": "immich server / machine-learning tag (inventory: immich-server, immich-machine-learning)",
        "targets": [
            ("apps/immich/values.yaml (controllers.main, server)", lambda: extract_tag_in_block(
                "apps/immich/values.yaml", "controllers"
            )),
            ("apps/immich/values.yaml (machine-learning)", lambda: extract_tag_in_block(
                "apps/immich/values.yaml", "machine-learning"
            )),
        ],
    },
    {
        "name": "actions/checkout tag (T-0043/T-0044, inventory: gha-actions-checkout)",
        "targets": [
            (".github/workflows/ci.yml", lambda: extract_all_action_tags(
                ".github/workflows/ci.yml", "actions/checkout"
            )),
            (".github/workflows/direct-push-guard.yml", lambda: extract_all_action_tags(
                ".github/workflows/direct-push-guard.yml", "actions/checkout"
            )),
            (".github/workflows/release-image.yml", lambda: extract_all_action_tags(
                ".github/workflows/release-image.yml", "actions/checkout"
            )),
            (".github/workflows/build-autopilot-image.yml", lambda: extract_all_action_tags(
                ".github/workflows/build-autopilot-image.yml", "actions/checkout"
            )),
        ],
    },
    {
        "name": "pvc-usage-reporter python image tag (T-0082, inventory: pvc-usage-reporter-image)",
        "targets": [
            ("apps/immich/pvc-usage-cronjob.yaml", lambda: extract_image_tag(
                "apps/immich/pvc-usage-cronjob.yaml", "image: python:"
            )),
            ("apps/coder/pvc-usage-cronjob.yaml", lambda: extract_image_tag(
                "apps/coder/pvc-usage-cronjob.yaml", "image: python:"
            )),
            ("apps/vaultwarden/pvc-usage-cronjob.yaml", lambda: extract_image_tag(
                "apps/vaultwarden/pvc-usage-cronjob.yaml", "image: python:"
            )),
            ("apps/vaultwarden/restic-backup-cronjob.yaml", lambda: extract_image_tag(
                "apps/vaultwarden/restic-backup-cronjob.yaml", "image: python:"
            )),
        ],
    },
    {
        "name": "azure/setup-helm version input (T-0090, inventory: gha-setup-helm-version)",
        "targets": [
            (".github/workflows/ci.yml (manifests job)", lambda: extract_helm_setup_version(
                ".github/workflows/ci.yml", "manifests"
            )),
            (".github/workflows/ci.yml (manifest-diff job)", lambda: extract_helm_setup_version(
                ".github/workflows/ci.yml", "manifest-diff"
            )),
        ],
    },
    {
        "name": "kustomize binary version (T-0090, inventory: kustomize-binary)",
        "targets": [
            (".github/workflows/ci.yml (manifests job)", lambda: extract_kustomize_download_version(
                ".github/workflows/ci.yml", "manifests"
            )),
            (".github/workflows/ci.yml (manifest-diff job)", lambda: extract_kustomize_download_version(
                ".github/workflows/ci.yml", "manifest-diff"
            )),
        ],
    },
    {
        "name": "coder-postgres image tag (T-0070, inventory: coder-postgres)",
        "targets": [
            ("apps/coder/postgres.yaml", lambda: extract_image_tag(
                "apps/coder/postgres.yaml", "image: postgres:"
            )),
            ("apps/coder/restic-backup-cronjob.yaml", lambda: extract_image_tag(
                "apps/coder/restic-backup-cronjob.yaml", "image: postgres:"
            )),
        ],
    },
    {
        "name": "restic/restic backup CronJob image tag (T-0098, inventory: vaultwarden-restic-image/coder-postgres-restic-image/immich-restic-image)",
        "targets": [
            ("apps/vaultwarden/restic-backup-cronjob.yaml", lambda: extract_image_tag_all(
                "apps/vaultwarden/restic-backup-cronjob.yaml", "image: restic/restic:"
            )),
            ("apps/coder/restic-backup-cronjob.yaml", lambda: extract_image_tag_all(
                "apps/coder/restic-backup-cronjob.yaml", "image: restic/restic:"
            )),
            ("apps/immich/restic-backup-cronjob.yaml", lambda: extract_image_tag_all(
                "apps/immich/restic-backup-cronjob.yaml", "image: restic/restic:"
            )),
        ],
    },
]


def main() -> int:
    fail = False
    for group in GROUPS:
        versions = []
        group_failed = False
        for path, fn in group["targets"]:
            try:
                versions.append((path, fn()))
            except Exception as e:
                print(f"::error::{group['name']}: {path}: 抽出に失敗しました: {e}")
                fail = True
                group_failed = True
        if group_failed:
            continue
        canonical = versions[0][1]
        mismatches = [(p, v) for p, v in versions if v != canonical]
        if mismatches:
            detail = ", ".join(f"{p}={v}" for p, v in versions)
            print(f"::error::{group['name']}: バージョンが一致しません ({detail})")
            fail = True
        else:
            print(f"ok: {group['name']} = {canonical}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
