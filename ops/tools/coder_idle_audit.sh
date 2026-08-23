#!/usr/bin/env bash
#
# coder_idle_audit.sh — coder namespace の workspace Pod の実消費を測り、
#                       idle 分の解放量を ops/projects/logs/P-0143/idle-audit.json に畳む (P-0143)
#
# 使い方:
#   ops/tools/coder_idle_audit.sh [-n NAMESPACE] [-s SAMPLES] [-i INTERVAL]
#                                 [-o OUTFILE] [--no-exec] [--self-test]
#
#   -n NAMESPACE  対象 namespace (既定: coder)
#   -s SAMPLES    kubectl top のサンプル回数 (既定: ${CODER_AUDIT_SAMPLES:-5})
#   -i INTERVAL   サンプル間隔秒 (既定: ${CODER_AUDIT_INTERVAL_S:-10})
#   -o OUTFILE    出力先 JSON (既定: <repo root>/ops/projects/logs/P-0143/idle-audit.json)。
#                 --self-test のときは必須 (fixture を実成果物パスへ書かせないガードあり)
#   --no-exec     kubectl exec を試みない (pods/exec 権限の無い SA で実行するとき。
#                 PVC 実使用量は null になり collection_notes に欠損を記録する)
#   --self-test   クラスタ不要。組み込み fixture で全経路を通し、出力スキーマを
#                 自己検査する (verify コマンドと同じ条件を機械確認)
#
# 測るもの (すべて read のみ):
#   - workspace Pod 一覧・requests/limits 実値
#     (selector: app.kubernetes.io/name=coder-workspace — 制御プレーン Pod app=coder は
#      ラベル系が違うため最初から混入しない)
#   - kubectl top pod/node の実使用量を SAMPLES 回取得し平均/最大を取る
#   - 動的 PVC の要求サイズ (PVC spec) と実使用量 (Pod 内 df、要 pods/exec 権限)。
#     PVC は deployment と違い start_count=0 でも作られ続けるため、Pod の無い
#     停止中 workspace の残留 PVC も stopped 分類で数える (main.tf L208/L239 の非対称)
#   - 分類: 平均CPU < CODER_AUDIT_IDLE_CPU_M (既定 50m) かつ 最大CPU <
#     CODER_AUDIT_IDLE_CPU_MAX_M (既定 500m) → idle。metrics 不取得なら unknown、
#     Pod 無しなら stopped。判定根拠 (生サンプル・閾値) を classification_basis に残す
#
# 終了コード:
#   0  収集成功 (一部の項目が権限や metrics 不備で欠損した場合も含む。欠損は
#      JSON の collection_notes に正直に記録され、捏造はしない)
#   2  クラスタに到達できない・認証できない等、判定不能な障害
#   3  --self-test がスキーマ検査に失敗した
#   64 使い方誤り
#
# 収集可能な環境: kubeconfig または SA token を持つ環境 (runner Job 内など)。
# initializer/worker checkout のように credential が無い環境では rc=2 になる
# (2026-08-23 実測: KUBERNETES_SERVICE_HOST は設定されるが token 未マウント → 401)。

set -u

NAMESPACE="coder"
SAMPLES="${CODER_AUDIT_SAMPLES:-5}"
INTERVAL="${CODER_AUDIT_INTERVAL_S:-10}"
OUT=""
SELF_TEST=0
NO_EXEC=0

usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,11p'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -n | --namespace) NAMESPACE="$2"; shift 2 ;;
    -s | --samples) SAMPLES="$2"; shift 2 ;;
    -i | --interval) INTERVAL="$2"; shift 2 ;;
    -o | --out) OUT="$2"; shift 2 ;;
    --self-test) SELF_TEST=1; shift ;;
    --no-exec) NO_EXEC=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
CANONICAL_OUT="$REPO_ROOT/ops/projects/logs/P-0143/idle-audit.json"
if [ -z "$OUT" ]; then
  if [ -z "$REPO_ROOT" ]; then
    echo "ERROR: -o 未指定かつ git repo 外で実行された (出力先を決められない)" >&2
    exit 64
  fi
  OUT="$CANONICAL_OUT"
fi
if [ "$SELF_TEST" = "1" ] && [ "$OUT" = "$CANONICAL_OUT" ]; then
  echo "ERROR: --self-test が実成果物 ($OUT) を上書きしようとしている。" >&2
  echo "       fixture データを実測として残さないため、-o \"\$(mktemp)\" を指定すること" >&2
  exit 64
fi

export PYTHONUTF8=1
export CODER_AUDIT_NAMESPACE="$NAMESPACE"
export CODER_AUDIT_SAMPLES="$SAMPLES"
export CODER_AUDIT_INTERVAL_S="$INTERVAL"
export CODER_AUDIT_OUT="$OUT"
export CODER_AUDIT_SELF_TEST="$SELF_TEST"
export CODER_AUDIT_NO_EXEC="$NO_EXEC"

exec python3 - <<'PY'
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time

NS = os.environ["CODER_AUDIT_NAMESPACE"]
SAMPLES = int(os.environ["CODER_AUDIT_SAMPLES"])
INTERVAL = float(os.environ["CODER_AUDIT_INTERVAL_S"])
OUT = os.environ["CODER_AUDIT_OUT"]
SELF_TEST = os.environ["CODER_AUDIT_SELF_TEST"] == "1"
NO_EXEC = os.environ["CODER_AUDIT_NO_EXEC"] == "1"

WS_SELECTOR = "app.kubernetes.io/name=coder-workspace"
PVC_SELECTOR = "app.kubernetes.io/name=coder-pvc"
HOME_MOUNT = "/home/coder"
IDLE_CPU_M = float(os.environ.get("CODER_AUDIT_IDLE_CPU_M", "50"))
IDLE_CPU_MAX_M = float(os.environ.get("CODER_AUDIT_IDLE_CPU_MAX_M", "500"))

notes = []


def note(msg):
    notes.append(msg)
    print(f"note: {msg}", file=sys.stderr)


def r(value):
    return round(value, 2)


def parse_quantity(q):
    """Kubernetes の数量文字列 → (cpu_millicores|None, bytes|None)。"""
    if q is None:
        return None, None
    m = re.fullmatch(r"([0-9.]+)(m|[KMGTPE]i?|Ki|Mi|Gi|Ti)?", str(q))
    if not m:
        return None, None
    num, unit = float(m.group(1)), m.group(2) or ""
    cpu = bytes_ = None
    if unit == "m":
        cpu = num
    elif unit == "":
        cpu = num * 1000
    mult = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
            "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
    if unit in mult:
        bytes_ = num * mult[unit]
    elif unit in ("m", ""):
        pass
    else:
        bytes_ = None
    return cpu, bytes_


def gib(bytes_):
    return None if bytes_ is None else bytes_ / 2**30


def run_kubectl(args):
    cmd = ["kubectl", "--request-timeout=15s"] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as err:
        return 127, "", f"{type(err).__name__}: {err}"


# --- self-test 用 fixture (--self-test 時のみ run_kubectl を差し替える) ---

NOW = datetime.datetime.now(datetime.timezone.utc)


def iso_ago(hours):
    return (NOW - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


FIXTURES = {
    "get pods": {"kind": "List", "items": [
        {"metadata": {"name": "coder-wsa-main", "labels": {
            "app.kubernetes.io/name": "coder-workspace",
            "com.coder.workspace.id": "wsa", "com.coder.workspace.name": "agent-home",
            "com.coder.user.username": "hiku"}},
         "status": {"phase": "Running"},
         "spec": {"containers": [{"name": "dev",
                                  "resources": {"requests": {"cpu": "250m", "memory": "512Mi"},
                                                "limits": {"cpu": "2", "memory": "2Gi"}}}]},
         "container_statuses": [("dev", iso_ago(48), 0)]},
        {"metadata": {"name": "coder-wsb-main", "labels": {
            "app.kubernetes.io/name": "coder-workspace",
            "com.coder.workspace.id": "wsb", "com.coder.workspace.name": "scratch",
            "com.coder.user.username": "hiku"}},
         "status": {"phase": "Running"},
         "spec": {"containers": [{"name": "dev",
                                  "resources": {"requests": {"cpu": "250m", "memory": "512Mi"},
                                                "limits": {"cpu": "6", "memory": "6Gi"}}}]},
         "container_statuses": [("dev", iso_ago(240), 0)]},
    ]},
    "get pvcs": {"items": [
        {"metadata": {"name": "coder-wsa-home", "labels": {"com.coder.workspace.id": "wsa"}},
         "spec": {"resources": {"requests": {"storage": "10Gi"}}}},
        {"metadata": {"name": "coder-wsb-home", "labels": {"com.coder.workspace.id": "wsb"}},
         "spec": {"resources": {"requests": {"storage": "128Gi"}}}},
        # Pod の無い停止中 workspace — deployment と違い PVC は start_count=0 でも残留する
        # (main.tf L208/L239 の非対称)。stopped 経路の試験用
        {"metadata": {"name": "coder-wsc-home",
                      "labels": {"com.coder.workspace.id": "wsc",
                                 "com.coder.workspace.name": "archived",
                                 "com.coder.user.username": "hiku"}},
         "spec": {"resources": {"requests": {"storage": "64Gi"}}}},
    ]},
    "top pod": [
        "coder-wsa-main   80m   900Mi",
        "coder-wsb-main   2m    300Mi",
        "coder-wsa-main   120m  950Mi",
        "coder-wsb-main   1m    310Mi",
        "coder-wsa-main   95m   920Mi",
        "coder-wsb-main   3m    305Mi",
        "coder-wsa-main   110m  940Mi",
        "coder-wsb-main   1m    300Mi",
        "coder-wsa-main   90m   910Mi",
        "coder-wsb-main   2m    320Mi",
    ],
    "get nodes": {"items": [{"metadata": {"name": "node01"},
                             "status": {"allocatable": {"cpu": "4", "memory": "11959Mi"}}}]},
    "top node": "node01   1800m   62%   7100Mi   59%",
    "df ok": ("Filesystem     1024-blocks     Used Available Capacity Mounted on\n"
              "/dev/sda1         10485760  3145728   7340032      30% " + HOME_MOUNT),
}


def fixture_kubectl(args):
    joined = " ".join(args[:3])
    if "top" in args and "pod" in args:
        # 実 kubectl top は一致 Pod 全行を 1 回で返す — fixture も 2 行/呼び出しで模す
        lines = [FIXTURES["top pod"].pop(0) for _ in range(2) if FIXTURES["top pod"]]
        return 0, "\n".join(lines), ""
    if "top" in args:
        return 0, FIXTURES["top node"], ""
    if "get" in args and "nodes" in args:
        return 0, json.dumps(FIXTURES["get nodes"]), ""
    if "get" in args and "persistentvolumeclaims" in args:
        return 0, json.dumps(FIXTURES["get pvcs"]), ""
    if "get" in args:
        return 0, json.dumps({"kind": "List", "items": _fixture_items()}), ""
    if "exec" in args:
        pod = args[args.index("--") - 3]  # exec -n NS <pod> -c dev -- ...
        if "wsb" in pod:  # idle 側は exec 権限欠如を想定して失敗させる (欠損経路の試験)
            return 1, "", 'error: unable to upgrade connection: pods/exec is forbidden'
        return 0, FIXTURES["df ok"], ""
    return 127, "", f"fixture miss: {joined}"


def _fixture_items():
    out = []
    for item in FIXTURES["get pods"]["items"]:
        cs = [{"name": n, "restartCount": rc,
               "running": {"startedAt": st}} for (n, st, rc) in item["container_statuses"]]
        out.append({"metadata": {"name": item["metadata"]["name"],
                                 "labels": item["metadata"]["labels"]},
                    "status": {"phase": item["status"]["phase"], "containerStatuses": cs},
                    "spec": item["spec"]})
    return out


if SELF_TEST:
    run_kubectl = fixture_kubectl  # noqa: F841


# --- 収集 ---

def get_json(kind):
    plural = {"pods": "pods", "pvcs": "persistentvolumeclaims", "nodes": "nodes"}[kind]
    args = ["get", plural, "-o", "json"]
    if kind != "nodes":
        args = ["-n", NS] + args
    if kind in ("pods", "pvcs"):
        sel = WS_SELECTOR if kind == "pods" else PVC_SELECTOR
        args += ["-l", sel]
    rc, out, err = run_kubectl(args)
    if rc != 0:
        note(f"kubectl get {plural} 失敗 (rc={rc}): {err[:300]}")
        return None
    return json.loads(out)


def sample_top():
    """kubectl top pod を SAMPLES 回取り、pod 名 → {cpu_m: [...], mem: [...]} へ畳む。"""
    series = {}
    fails = []
    for i in range(SAMPLES):
        if i:
            time.sleep(INTERVAL)
        rc, out, err = run_kubectl(["top", "pod", "-n", NS, "-l", WS_SELECTOR, "--no-headers"])
        if rc != 0:
            fails.append(err[:200])
            continue
        for line in out.splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            name, cpu_s, mem_s = parts
            cpu_m, _ = parse_quantity(cpu_s)
            _, mem_b = parse_quantity(mem_s)
            slot = series.setdefault(name, {"cpu_m": [], "mem_b": []})
            if cpu_m is not None:
                slot["cpu_m"].append(cpu_m)
            if mem_b is not None:
                slot["mem_b"].append(mem_b)
    if fails:
        note(f"kubectl top pod の失敗 {len(fails)}/{SAMPLES} 回 (ok={SAMPLES - len(fails)}): {fails[0]}")
    if not series:
        note("kubectl top pod から有効なサンプル無し — CPU/メモリ実使用量は欠損 (分類は unknown)")
    return series


def node_context():
    js = get_json("nodes")
    if not js or not js.get("items"):
        return None
    item = js["items"][0]
    alloc = item["status"].get("allocatable", {})
    cpu_m, _ = parse_quantity(alloc.get("cpu"))
    _, mem_b = parse_quantity(alloc.get("memory"))
    ctx = {"name": item["metadata"]["name"],
           "allocatable_cpu_m": int(cpu_m) if cpu_m is not None else None,
           "allocatable_memory_gib": r(gib(mem_b)) if mem_b else None,
           "top": None}
    rc, out, err = run_kubectl(["top", "node", "--no-headers"])
    if rc == 0 and out.split():
        parts = out.split()
        # 列: NAME CPU(cores) CPU% MEMORY(bytes) MEMORY%
        _, mem_b2 = parse_quantity(parts[3]) if len(parts) > 3 else (None, None)
        ctx["top"] = {"raw_line": out.splitlines()[0],
                      "memory_gib": r(gib(mem_b2)) if mem_b2 else None}
    else:
        note(f"kubectl top node 失敗 (rc={rc}): {err[:200]}")
    return ctx


def df_home(pod):
    """Pod 内 df で home PVC の実使用量 (KiB ブロック) を得る。権限が無ければ None。"""
    if NO_EXEC:
        return None, "skipped (--no-exec)"
    rc, out, err = run_kubectl(["exec", "-n", NS, pod, "-c", "dev", "--",
                                "df", "-k", "-P", HOME_MOUNT])
    if rc != 0:
        return None, f"exec failed (rc={rc}): {err[:200]}"
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 6 and cols[5] == HOME_MOUNT:
            try:
                return int(cols[2]) * 1024, "df"
            except ValueError:
                break
    return None, f"unparseable df output: {out[:200]}"


def classify(cpu_series):
    """(classification, basis) を返す。根拠を残すので再判定は人間も後からできる。"""
    if not cpu_series:
        return "unknown", "kubectl top のサンプル無し (metrics 不取得)"
    mean_m = sum(cpu_series) / len(cpu_series)
    max_m = max(cpu_series)
    if mean_m < IDLE_CPU_M and max_m < IDLE_CPU_MAX_M:
        verdict = "idle"
    else:
        verdict = "active"
    basis = (f"samples={len(cpu_series)} mean={mean_m:.0f}m max={max_m:.0f}m "
             f"vs 閾値 mean<{IDLE_CPU_M:.0f}m 且つ max<{IDLE_CPU_MAX_M:.0f}m")
    return verdict, basis


def main():
    generated_at = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    pods_js = get_json("pods")
    if pods_js is None:
        print("ERROR: workspace Pod 一覧が取れない — クラスタ到達性/認証を確認", file=sys.stderr)
        return 2
    pvcs_js = get_json("pvcs") or {"items": []}
    pvc_by_ws = {}
    for item in pvcs_js["items"]:
        lbl = item["metadata"].get("labels", {})
        wid = lbl.get("com.coder.workspace.id")
        if wid:
            _, req_b = parse_quantity(
                item["spec"].get("resources", {}).get("requests", {}).get("storage"))
            pvc_by_ws.setdefault(wid, []).append({
                "name": item["metadata"]["name"],
                "requested_gib": r(gib(req_b)) if req_b else None,
                "used_gib": None, "used_source": None,
                # stopped 判定時に workspaces 側へ移す属性 (running 経路では使わない)
                "ws_name": lbl.get("com.coder.workspace.name"),
                "user": lbl.get("com.coder.user.username")})

    series = sample_top()
    node = node_context()
    workspaces = []
    for item in pods_js.get("items", []):
        meta = item["metadata"]
        labels = meta.get("labels", {})
        spec_c = item["spec"]["containers"][0]
        res = spec_c.get("resources", {})
        req_cpu_m, _ = parse_quantity(res.get("requests", {}).get("cpu"))
        _, req_mem_b = parse_quantity(res.get("requests", {}).get("memory"))
        lim_cpu_m, _ = parse_quantity(res.get("limits", {}).get("cpu"))
        _, lim_mem_b = parse_quantity(res.get("limits", {}).get("memory"))
        cstat = next(iter(item.get("status", {}).get("containerStatuses", [])), {})
        pod = meta["name"]
        cpu_samples = series.get(pod, {}).get("cpu_m", [])
        mem_samples = series.get(pod, {}).get("mem_b", [])
        cls, basis = classify(cpu_samples)
        pvcs = pvc_by_ws.get(labels.get("com.coder.workspace.id"), [])
        if pvcs:
            used_b, src = df_home(pod)
            if used_b is None:
                note(f"{pod}: home PVC 実使用量を取得できず ({src}) — used_gib=null のまま残す")
            else:
                for p in pvcs:
                    p["used_gib"], p["used_source"] = r(gib(used_b)), src
        ws = {
            "name": labels.get("com.coder.workspace.name"),
            "user": labels.get("com.coder.user.username"),
            "pod": pod,
            "phase": item.get("status", {}).get("phase"),
            "started_at": cstat.get("running", {}).get("startedAt"),
            "restarts": cstat.get("restartCount", 0),
            "requests": {"cpu_m": int(req_cpu_m) if req_cpu_m is not None else None,
                         "memory_gib": r(gib(req_mem_b)) if req_mem_b else None},
            "limits": {"cpu_m": int(lim_cpu_m) if lim_cpu_m is not None else None,
                       "memory_gib": r(gib(lim_mem_b)) if lim_mem_b else None},
            "pvc": pvcs,
            "cpu_usage": {"source": "kubectl_top", "samples_m": [int(x) for x in cpu_samples],
                          "mean_m": r(sum(cpu_samples) / len(cpu_samples)) if cpu_samples else None,
                          "max_m": int(max(cpu_samples)) if cpu_samples else None}
                      if cpu_samples else {"source": "kubectl_top", "samples_m": [],
                                           "mean_m": None, "max_m": None, "error": "no samples"},
            "memory_usage": {"source": "kubectl_top",
                             "samples_gib": [r(gib(b)) for b in mem_samples],
                             "mean_gib": r(gib(sum(mem_samples) / len(mem_samples))) if mem_samples else None}
                            if mem_samples else {"source": "kubectl_top", "samples_gib": [],
                                                 "mean_gib": None, "error": "no samples"},
            "classification": cls,
            "classification_basis": basis,
        }
        ws["reclaim_if_idle"] = {
            "requests": {"cpu_m": ws["requests"]["cpu_m"] if cls == "idle" else 0,
                         "memory_gib": ws["requests"]["memory_gib"] if cls == "idle" else 0.0},
            "usage": {"cpu_m": ws["cpu_usage"]["mean_m"] if cls == "idle" else None,
                      "memory_gib": ws["memory_usage"]["mean_gib"] if cls == "idle" else None},
            "pvc_gib": sum(p["requested_gib"] or 0 for p in pvcs) if cls == "idle" else 0.0,
        }
        workspaces.append(ws)

    # Pod の無い workspace PVC (停止中 workspace) — CPU 観測は不可能だがディスクは
    # 残り続けるので、stopped として workspaces に畳む (捏造せず実使用量は null)
    seen_wids = {
        item["metadata"].get("labels", {}).get("com.coder.workspace.id")
        for item in pods_js.get("items", [])
    }
    for wid, pvcs in pvc_by_ws.items():
        if wid in seen_wids:
            continue
        total_gib = r(sum(p["requested_gib"] or 0 for p in pvcs))
        missing_id = "" if wid is not None else " (com.coder.workspace.id ラベル無し)"
        note(f"{pvcs[0]['name']}: Pod 無しの workspace PVC — stopped として数える"
             f" (実使用量は計測不可)")
        workspaces.append({
            "name": pvcs[0]["ws_name"],
            "user": pvcs[0]["user"],
            "pod": None, "phase": None, "started_at": None, "restarts": None,
            "requests": {"cpu_m": 0, "memory_gib": 0.0},
            "limits": {"cpu_m": None, "memory_gib": None},
            "pvc": [{k: p[k] for k in ("name", "requested_gib", "used_gib", "used_source")}
                    for p in pvcs],
            "cpu_usage": {"source": "none", "samples_m": [], "mean_m": None,
                          "max_m": None, "error": "pod 無し"},
            "memory_usage": {"source": "none", "samples_gib": [], "mean_gib": None,
                             "error": "pod 無し"},
            "classification": "stopped",
            "classification_basis": (
                f"Pod が存在しない (deployment 停止{missing_id})。CPU は観測対象外で "
                f"PVC {total_gib} GiB のみ残留"),
            "reclaim_if_idle": {
                "requests": {"cpu_m": 0, "memory_gib": 0.0},
                "usage": {"cpu_m": None, "memory_gib": None},
                "pvc_gib": total_gib},
        })

    idle = [w for w in workspaces if w["classification"] == "idle"]
    stopped = [w for w in workspaces if w["classification"] == "stopped"]
    report = {
        "schema": "coder-idle-audit/v1",
        "generated_at": generated_at,
        "namespace": NS,
        "sampling": {"samples": SAMPLES, "interval_s": INTERVAL,
                     "thresholds": {"idle_cpu_mean_m": IDLE_CPU_M, "idle_cpu_max_m": IDLE_CPU_MAX_M}},
        "node": node,
        "collection_notes": notes,
        "workspaces": workspaces,
        "reclaimable": {
            "total_workspaces": len(workspaces),
            "idle_count": len(idle),
            "stopped_count": len(stopped),
            "unknown_count": sum(1 for w in workspaces if w["classification"] == "unknown"),
            "requests_based": {
                "cpu_m": sum(w["reclaim_if_idle"]["requests"]["cpu_m"] or 0 for w in idle),
                "memory_gib": r(sum(w["reclaim_if_idle"]["requests"]["memory_gib"] or 0 for w in idle)),
                "pvc_gib": r(sum(w["reclaim_if_idle"]["pvc_gib"] for w in idle)),
            },
            # 停止中 workspace の残留 PVC。CPU/メモリの解放は既に起きているが
            # ディスクは確保されたまま — 削除は不可逆なので requests_based には合算しない
            "stopped_pvc_gib": r(sum(w["reclaim_if_idle"]["pvc_gib"] for w in stopped)),
            "usage_based": {
                "cpu_m": r(sum(w["reclaim_if_idle"]["usage"]["cpu_m"] or 0 for w in idle)),
                "memory_gib": r(sum(w["reclaim_if_idle"]["usage"]["memory_gib"] or 0 for w in idle)),
            },
            "note": ("requests_based は scheduler の capacity 差分、usage_based は観測窓での実解放量。"
                     "stopped_pvc_gib は Pod 無しの停止済み workspace の PVC (CPU/メモリは既に解放済み)。"
                     "PVC は Pod を止めても消えない (local-path)。ディスクを実際に空けるには "
                     "PVC 削除が前提で、これは不可逆なため別判断"),
        },
    }

    out_dir = os.path.dirname(os.path.abspath(OUT))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".idle-audit.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, OUT)
    except BaseException:
        os.unlink(tmp_path)
        raise
    print(f"OK: {OUT} へ書き出し "
          f"({len(workspaces)} workspaces / idle {len(idle)} / stopped {len(stopped)})")
    return 0


rc = main()
if SELF_TEST and rc == 0:
    with open(OUT) as fh:
        d = json.load(fh)
    problems = []
    if not d.get("workspaces"):
        problems.append("workspaces が空")
    for w in d["workspaces"]:
        if "cpu_usage" not in w:
            problems.append(f"{w.get('pod')}: cpu_usage 無し")
        if "classification" not in w:
            problems.append(f"{w.get('pod')}: classification 無し")
    if "reclaimable" not in d:
        problems.append("reclaimable 無し")
    by_name = {w["name"]: w for w in d["workspaces"]}
    if by_name.get("agent-home", {}).get("classification") != "active":
        problems.append("fixture agent-home が active にならない")
    if by_name.get("scratch", {}).get("classification") != "idle":
        problems.append("fixture scratch が idle にならない")
    rec = d["reclaimable"]
    if rec["idle_count"] != 1 or rec["requests_based"]["pvc_gib"] != 128.0 \
            or rec["requests_based"]["cpu_m"] != 250 or rec["requests_based"]["memory_gib"] != 0.5:
        problems.append(f"reclaimable の合計が想定外: {rec}")
    if rec.get("stopped_count") != 1 or rec.get("stopped_pvc_gib") != 64.0:
        problems.append(f"stopped (Pod 無し PVC) の集計が想定外: "
                        f"count={rec.get('stopped_count')} gib={rec.get('stopped_pvc_gib')}")
    stopped_ws = [w for w in d["workspaces"] if w["classification"] == "stopped"]
    if len(stopped_ws) == 1:
        s = stopped_ws[0]
        if s["name"] != "archived" or s["pod"] is not None \
                or any(p["used_gib"] is not None for p in s["pvc"]):
            problems.append(f"stopped エントリの中身が想定外: {s}")
    else:
        problems.append(f"stopped エントリが 1 件でない: {len(stopped_ws)} 件")
    scratch = by_name.get("scratch", {})
    if any(p["used_gib"] is not None for p in scratch.get("pvc", [])):
        problems.append("exec 失敗の workspace で used_gib を捏造している")
    if problems:
        print("FAIL self-test:", *problems, sep="\n  ", file=sys.stderr)
        sys.exit(3)
    print("OK: self-test 通過 (スキーマ + 分類 + 解放量合計 + 欠損の扱い)")
sys.exit(rc)
PY
