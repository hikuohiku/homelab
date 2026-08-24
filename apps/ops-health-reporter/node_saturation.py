#!/usr/bin/env python3
"""CPU 飽和前兆の常設計器 (P-9037)。

2026-08-24 18:18 JST、runner×2 + curriculum + heart で requests 合計
3761m/4000m になりホスト load 25、kube-apiserver も sshd も応答不能になった
(ops/rules.json の `_max_concurrent_comment`)。スケジューラは超過分を Pending に
するが、「もうすぐ沈む」を告げる計器が無い。本ツールはその計器で、CPU requests /
allocatable とホスト load を実測し、閾値超過で exit 1 を返す。

取得源 (spec dod (1)):
  - CPU requests : 全 namespace の pod spec `spec.containers[].resources.requests.cpu` の合計
    (コア API /api/v1/pods)
  - allocatable  : node status `status.allocatable.cpu` (コア API /api/v1/nodes)
  - load         : kubelet stats/summary API (`GET /api/v1/nodes/<name>/proxy/stats/summary`)
    → fallback `/proc/loadavg`。summary には host load が直接無い (P-9029 の審査指摘。
    substrate.md「観測経路」節の実測記録参照) ため、実効的には /proc/loadavg に倒れる。
    loadavg は PID namespace で仮想化されないため、node01 上の任意の pod から
    host 全体の load が読める (2026-08-24 実測)。

閾値 (rules.json の逆算を根拠に P-9029 の dod 踏襲):
  - allocatable の 90% 超 かつ/または load > vCPU 数 → warn (exit 1)

標準ライブラリのみで動く (report.py と同じく pip install 不要)。クラスタ到達は
ServiceAccount トークン (自動マウント) を使う。`--check` は同梱 fixture だけで
ネットワーク非依存に自己検査する (P-9002 の restart_wave.py --selftest と同じ思想)。

使い方:
  python3 ops/tools/node_saturation.py --check          # 自己検査 (ネットワーク非依存)
  python3 ops/tools/node_saturation.py --node node01    # 実測して exit 0/1
  python3 ops/tools/node_saturation.py --requests-m 3761 --allocatable-m 4000 \
      --load 25 --vcpus 4                               # オフライン判定 (テスト用)

apps/ops-health-reporter/ に同一内容のコピーが置いてある (reporter の
configMapGenerator が /scripts に載せ、report.py から import される)。
drift は ops/check_node_saturation_script_sync.py (CI) が検出する —
直すときは必ず両方を同じ PR で直すこと。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

# allocatable の何 % で「飽和前兆」とみなすか (rules.json の逆算を根拠に P-9029 の dod 踏襲)
REQUESTS_RATIO_WARN = 0.9

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
K8S_BASE = "https://{}:{}".format(K8S_HOST, K8S_PORT)


def parse_cpu_millicores(value):
    """K8s の CPU リソース表記 ("250m" / "1" / "1.5" / 1000) → millicores の int。

    解釈できない入力は例外ではなく None (1 件の壊れで集計を止めない)。bool は
    int の派生なので明示的に弾く (download_budget.coerce_bytes と同じ判断)。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value) * 1000))
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("m"):
        try:
            return int(s[:-1])
        except ValueError:
            return None
    try:
        return int(round(float(s) * 1000))
    except ValueError:
        return None


def sum_cpu_requests(pods_doc):
    """全 pod の `spec.containers[].resources.requests.cpu` の合計 (millicores)。

    08-24 の逆算 (rules.json の `_max_concurrent_comment`) と同じく、通常コンテナの
    requests のみを数える。initContainers は起動時のみ占有で定常の容量計算に
    混ぜない。requests が無いコンテナは 0 扱い (best-effort)。
    """
    total = 0
    for pod in pods_doc.get("items", []):
        for container in pod.get("spec", {}).get("containers", []):
            cpu = (
                container.get("resources", {})
                .get("requests", {})
                .get("cpu")
            )
            m = parse_cpu_millicores(cpu)
            if m is not None:
                total += m
    return total


def allocatable_cpu_millicores(node_doc):
    """node status の `status.allocatable.cpu` → millicores (int|None)。"""
    return parse_cpu_millicores(
        node_doc.get("status", {}).get("allocatable", {}).get("cpu")
    )


def vcpus(node_doc):
    """allocatable から vCPU 数を割り出す (int|None)。

    4 vCPU → allocatable "4" (4000m)。round して 0 にならないよう下限 1 を張る。
    """
    m = allocatable_cpu_millicores(node_doc)
    if m is None:
        return None
    return max(1, int(round(m / 1000.0)))


def load_from_summary(summary_doc):
    """kubelet stats/summary から host load を読む (P-9037 dod (1) の主経路)。

    現行 kubelet の stats/summary スキーマには host load が**無い** (P-9029 の
    審査指摘。metrics.k8s.io と同様、usage 系しか返さない)。将来 kubelet が
    load を載せるようになったときのための解釈関数で、現状は常に None を返す。
    戻り値は 1 分平均の float で、None は「取れなかった」。
    """
    if not isinstance(summary_doc, dict):
        return None
    # 想定する将来形: node.load のような場所に 1 分平均が来る
    node = summary_doc.get("node")
    if isinstance(node, dict):
        load = node.get("load") or node.get("loadavg") or node.get("load_1m")
        try:
            return float(load)
        except (TypeError, ValueError):
            return None
    return None


def read_loadavg(path="/proc/loadavg"):
    """/proc/loadavg の 1 分平均を読む (float|None)。

    loadavg は PID namespace で仮想化されないため、node01 上の pod からでも
    host 全体の load が読める (2026-08-24 実測)。読めない・壊れていれば None
    (例外にしない — 「load が測れない」をデータとして報告に乗せる)。
    """
    try:
        with open(path) as f:
            fields = f.read().split()
        return float(fields[0])
    except (OSError, ValueError, IndexError):
        return None


def judge(requests_m, allocatable_m, load_1m, vcpus):
    """飽和前兆を判定する (純関数)。

    warn 条件 (rules.json の逆算を根拠に P-9029 の dod 踏襲):
      - requests_m / allocatable_m > REQUESTS_RATIO_WARN (allocatable の 90% 超)
      - load_1m > vcpus (1 分平均 load が vCPU 数を超えた)

    load が取れない (None) でも requests 比率側で鳴る。requests 側が無い
    (観測失敗) ときは load 側だけで鳴る。
    """
    reasons = []
    ratio = None
    if requests_m is not None and allocatable_m:
        ratio = requests_m / float(allocatable_m)
        if ratio > REQUESTS_RATIO_WARN:
            reasons.append("requests_ratio")
    if load_1m is not None and vcpus is not None and load_1m > vcpus:
        reasons.append("load")
    return {
        "status": "warn" if reasons else "ok",
        "reasons": reasons,
        "requests_m": requests_m,
        "allocatable_m": allocatable_m,
        "requests_ratio": round(ratio, 4) if ratio is not None else None,
        "load_1m": load_1m,
        "vcpus": vcpus,
    }


def exit_code(report):
    """判定結果から終了コードへ。warn なら 1、それ以外は 0。"""
    return 1 if report.get("status") == "warn" else 0


# ---------------------------------------------------------------------------
# k8s 到達 (report.py と同じ ServiceAccount トークン方式)
# ---------------------------------------------------------------------------

def k8s_get(path):
    req = urllib.request.Request(
        K8S_BASE + path, headers={"Authorization": "Bearer " + _sa_token()}
    )
    with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=15) as resp:
        return json.load(resp)


def _sa_token():
    with open(os.path.join(SA_DIR, "token")) as f:
        return f.read().strip()


def _ssl_ctx():
    return ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))


def fetch_kubelet_summary(node_name):
    """kubelet stats/summary を読む (nodes/proxy の RBAC が要る)。

    取れないときは None (403 / トークン無し / タイムアウトのどれでも)。主に
    将来の load 拡張を見越した主経路で、現行では summary に load が無いため
    呼び出し側は直ちに /proc/loadavg へ倒す。
    """
    if not node_name:
        return None
    try:
        return k8s_get(
            "/api/v1/nodes/{}/proxy/stats/summary".format(node_name)
        )
    except (OSError, urllib.error.HTTPError, urllib.error.URLError):
        return None


def fetch_node(node_name):
    """node を 1 台返す。node_name 指定が無ければ最初の 1 台。"""
    nodes = k8s_get("/api/v1/nodes")
    items = nodes.get("items", [])
    if not items:
        raise RuntimeError("node が見つからない")
    if node_name:
        for item in items:
            if item.get("metadata", {}).get("name") == node_name:
                return item
        raise RuntimeError("node {} が見つからない".format(node_name))
    return items[0]


def run(node_name=None, loadavg_path="/proc/loadavg"):
    """クラスタから実測して判定 report を返す (exit code は exit_code())。

    load は kubelet summary → /proc/loadavg の順に試す (spec dod (1) の既定)。
    summary に load が無い (P-9029) ため、実効的には /proc/loadavg に倒れる。
    """
    pods = k8s_get("/api/v1/pods")
    node = fetch_node(node_name)
    name = node.get("metadata", {}).get("name")
    requests_m = sum_cpu_requests(pods)
    alloc_m = allocatable_cpu_millicores(node)
    vcpus_ = vcpus(node)

    summary = fetch_kubelet_summary(name)
    load = load_from_summary(summary)
    load_source = "kubelet_summary" if load is not None else None
    if load is None:
        load = read_loadavg(loadavg_path)
        if load is not None:
            load_source = "proc_loadavg"

    report = judge(requests_m, alloc_m, load, vcpus_)
    report["node"] = name
    report["load_source"] = load_source
    return report


# ---------------------------------------------------------------------------
# --check 自己検査 (ネットワーク非依存。fixture と引数検証のみ)
# ---------------------------------------------------------------------------

def _selfcheck():
    """同梱 fixture で判定ロジックを固定する。失敗したら例外を投げる。"""
    def expect(cond, message):
        if not cond:
            raise AssertionError(message)

    # parse_cpu_millicores の表記解釈
    expect(parse_cpu_millicores("250m") == 250, '"250m" → 250')
    expect(parse_cpu_millicores("1") == 1000, '"1" → 1000')
    expect(parse_cpu_millicores("1.5") == 1500, '"1.5" → 1500')
    expect(parse_cpu_millicores(2) == 2000, "2 → 2000")
    expect(parse_cpu_millicores("") is None, '空文字 → None')
    expect(parse_cpu_millicores("abc") is None, '"abc" → None')
    expect(parse_cpu_millicores(True) is None, "True → None")

    # 08-24 の実測値 (rules.json `_max_concurrent_comment`): 3761m/4000m・load 25
    report = judge(3761, 4000, 25.0, 4)
    expect(
        report["status"] == "warn",
        "08-24 実測値 (3761m/4000m・load 25) で warn が出ない",
    )
    expect(
        set(report["reasons"]) == {"requests_ratio", "load"},
        "08-24 実測値で requests_ratio と load の両方が鳴るべき",
    )
    expect(exit_code(report) == 1, "warn は exit 1")

    # 余裕のある平常時は ok
    ok_report = judge(1800, 4000, 2.0, 4)
    expect(ok_report["status"] == "ok", "平常値で ok でない")
    expect(exit_code(ok_report) == 0, "ok は exit 0")

    # requests 比率のみ (load が取れない観測失敗時) でも鳴る
    only_ratio = judge(3700, 4000, None, 4)
    expect(
        only_ratio["status"] == "warn" and only_ratio["reasons"] == ["requests_ratio"],
        "load None でも requests 比率側で warn が要る",
    )

    # 境界: ちょうど 90% は ok、超えたら warn
    expect(judge(3600, 4000, None, 4)["status"] == "ok", "90% ちょうどは ok")
    expect(judge(3601, 4000, None, 4)["status"] == "warn", "90% 超で warn")

    # sum_cpu_requests の fixture (2 pod / 3 container)
    pods = {
        "items": [
            {
                "spec": {
                    "containers": [
                        {"resources": {"requests": {"cpu": "1"}}},
                        {"resources": {"requests": {"cpu": "250m"}}},
                    ]
                }
            },
            {
                "spec": {
                    "containers": [
                        {"resources": {"requests": {"cpu": "500m"}}},
                        {"name": "no-request"},
                    ]
                }
            },
        ]
    }
    expect(sum_cpu_requests(pods) == 1750, "sum_cpu_requests の fixture")

    # read_loadavg: 一時ファイルから 1 分平均を読む
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".loadavg", delete=False) as f:
        f.write("25.0 12.3 10.1 5/2015 696\n")
        path = f.name
    try:
        expect(read_loadavg(path) == 25.0, "read_loadavg が 1 分平均を返す")
    finally:
        os.unlink(path)
    expect(read_loadavg("/nonexistent/loadavg") is None, "読めない loadavg は None")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="node_saturation.py",
        description="CPU 飽和前兆の常設計器 (P-9037)。閾値超過で exit 1。",
    )
    parser.add_argument("--check", action="store_true", help="自己検査 (ネットワーク非依存)")
    parser.add_argument("--node", help="読み取る node 名 (既定: 最初の 1 台)")
    parser.add_argument("--requests-m", type=int, help="requests 合計 (millicores) を指定 (オフライン)")
    parser.add_argument("--allocatable-m", type=int, help="allocatable (millicores) を指定 (オフライン)")
    parser.add_argument("--load", type=float, help="1 分平均 load を指定 (オフライン)")
    parser.add_argument("--vcpus", type=int, help="vCPU 数を指定 (オフライン)")
    parser.add_argument("--loadavg-path", default="/proc/loadavg", help="loadavg の代替パス (テスト用)")
    parser.add_argument("--json", action="store_true", help="結果を JSON で出力")
    args = parser.parse_args(argv)

    if args.check:
        try:
            _selfcheck()
        except AssertionError as e:
            print("node_saturation --check FAILED: {}".format(e), file=sys.stderr)
            return 1
        print("node_saturation --check ok")
        return 0

    offline = (
        args.requests_m is not None
        or args.allocatable_m is not None
        or args.load is not None
        or args.vcpus is not None
    )
    if offline:
        # オフライン判定 (テスト・fixture 用)。指定の無い値は None のまま judge へ
        report = judge(args.requests_m, args.allocatable_m, args.load, args.vcpus)
    else:
        try:
            report = run(node_name=args.node, loadavg_path=args.loadavg_path)
        except Exception as e:  # noqa: BLE001 — 観測失敗は warn でなく失敗として報告
            print("node_saturation: 観測失敗: {}: {}".format(type(e).__name__, e), file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "status={} requests_m={} allocatable_m={} ratio={} load_1m={} vcpus={} load_source={} node={}".format(
                report["status"],
                report["requests_m"],
                report["allocatable_m"],
                report["requests_ratio"],
                report["load_1m"],
                report["vcpus"],
                report.get("load_source"),
                report.get("node"),
            )
        )
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())