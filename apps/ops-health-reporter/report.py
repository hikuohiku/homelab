"""クラスタ内から ArgoCD/Pod/PVC/Node の状態を集め、GitHub の専用ブランチへ書き戻す。

標準ライブラリのみで動く（イメージに pip install を要求しない）。
k8s API へは ServiceAccount トークン（自動マウント）、GitHub API へは
Doppler 経由の GITHUB_HEALTH_REPORTER_TOKEN（Contents: Read and write のみ）で到達する。
"""

import base64
import datetime
import json
import os
import re
import ssl
import urllib.error
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
K8S_BASE = "https://{}:{}".format(K8S_HOST, K8S_PORT)

with open(os.path.join(SA_DIR, "token")) as f:
    SA_TOKEN = f.read().strip()
SSL_CTX = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))


def k8s_get(path):
    req = urllib.request.Request(
        K8S_BASE + path, headers={"Authorization": "Bearer " + SA_TOKEN}
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
        return json.load(resp)


def k8s_get_text(path):
    # /log エンドポイントはプレーンテキストを返す（JSON ではない）
    req = urllib.request.Request(
        K8S_BASE + path, headers={"Authorization": "Bearer " + SA_TOKEN}
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def collect(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — レポートの1項目が壊れても残りは出したい
        return {"error": "{}: {}".format(type(e).__name__, e)}


def collect_applications():
    data = k8s_get("/apis/argoproj.io/v1alpha1/applications")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        out.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "sync": status.get("sync", {}).get("status"),
                "health": status.get("health", {}).get("status"),
            }
        )
    return out


def collect_pod_issues():
    data = k8s_get("/api/v1/pods")
    issues = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        phase = status.get("phase")
        cs_list = status.get("containerStatuses") or []
        restarts = sum(cs.get("restartCount", 0) for cs in cs_list)
        waiting_reasons = [
            cs["state"]["waiting"]["reason"]
            for cs in cs_list
            if "waiting" in cs.get("state", {})
        ]
        if phase not in ("Running", "Succeeded") or restarts > 3 or waiting_reasons:
            issues.append(
                {
                    "name": meta.get("name"),
                    "namespace": meta.get("namespace"),
                    "phase": phase,
                    "restarts": restarts,
                    "waiting_reasons": waiting_reasons,
                }
            )
    return issues


def collect_pvcs():
    data = k8s_get("/api/v1/persistentvolumeclaims")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        out.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "phase": status.get("phase"),
                "requested": spec.get("resources", {}).get("requests", {}).get("storage"),
                "capacity": status.get("capacity", {}).get("storage"),
            }
        )
    return out


def collect_pod_metrics():
    data = k8s_get("/apis/metrics.k8s.io/v1beta1/pods")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        out.append(
            {
                "namespace": meta.get("namespace"),
                "name": meta.get("name"),
                "containers": [
                    {
                        "name": c.get("name"),
                        "cpu": c.get("usage", {}).get("cpu"),
                        "memory": c.get("usage", {}).get("memory"),
                    }
                    for c in item.get("containers", [])
                ],
            }
        )
    return out


def collect_node_metrics():
    data = k8s_get("/apis/metrics.k8s.io/v1beta1/nodes")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        usage = item.get("usage", {})
        out.append(
            {
                "name": meta.get("name"),
                "cpu": usage.get("cpu"),
                "memory": usage.get("memory"),
            }
        )
    return out


PVC_USAGE_NAMESPACES = ["immich", "vaultwarden", "coder"]


def collect_pvc_usage():
    out = []
    for ns in PVC_USAGE_NAMESPACES:
        try:
            data = k8s_get(
                "/api/v1/namespaces/{}/configmaps/pvc-usage-report".format(ns)
            )
            report = json.loads(data.get("data", {}).get("report.json", "{}"))
            out.append(report)
        except Exception as e:  # noqa: BLE001 — namespace 側の CronJob が
            # まだ1回も走っていない（ConfigMap 未作成）場合を含め、他 namespace の
            # 収集を止めない
            out.append({"namespace": ns, "error": "{}: {}".format(type(e).__name__, e)})
    return out


def collect_nodes():
    data = k8s_get("/api/v1/nodes")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
        out.append(
            {
                "name": meta.get("name"),
                "allocatable": status.get("allocatable"),
                "capacity": status.get("capacity"),
                "ready": conditions.get("Ready"),
                "diskPressure": conditions.get("DiskPressure"),
                "memoryPressure": conditions.get("MemoryPressure"),
            }
        )
    return out


# loop.sh (apps/autopilot/loop.sh) の log() が書く心拍行だけを抜き出す正規表現。
# ここにマッチした行から取り出した値（timestamp/iteration/exit_code/elapsed_seconds）
# だけを report に載せる。生ログはこの関数の外に一切出さない — claude の出力を
# git 管理のブランチにそのまま持ち出す経路を作らないため（T-0110）。
# exit=124 (timeout) の行は "exit=124 (timed out after Ns) elapsed=Ns" と exit code の後に
# 括弧書きが挟まるので、その部分を任意にして両方の形式を通す
HEARTBEAT_RE = re.compile(
    r"^\[autopilot\] (\S+) iteration #(\d+) (?:start|end exit=(-?\d+)(?: \([^)]*\))? elapsed=(\d+)s)"
)


def parse_heartbeat(raw_log):
    last_start = None
    last_end = None
    for line in raw_log.splitlines():
        m = HEARTBEAT_RE.match(line.strip())
        if not m:
            continue
        ts, iteration, exit_code, elapsed = m.groups()
        if exit_code is None:
            last_start = {"timestamp": ts, "iteration": int(iteration)}
        else:
            last_end = {
                "timestamp": ts,
                "iteration": int(iteration),
                "exit_code": int(exit_code),
                "elapsed_seconds": int(elapsed),
            }
    return {"last_start": last_start, "last_end": last_end}


AUTOPILOT_NAMESPACE = "autopilot"


def collect_autopilot_health():
    result = {}

    try:
        dep = k8s_get(
            "/apis/apps/v1/namespaces/{}/deployments/autopilot".format(AUTOPILOT_NAMESPACE)
        )
        status = dep.get("status", {})
        result["deployment"] = {
            "replicas": status.get("replicas", 0),
            "readyReplicas": status.get("readyReplicas", 0),
            "unavailableReplicas": status.get("unavailableReplicas", 0),
        }
    except Exception as e:  # noqa: BLE001
        result["deployment"] = {"error": "{}: {}".format(type(e).__name__, e)}

    pod_name = None
    try:
        pods = k8s_get(
            "/api/v1/namespaces/{}/pods?labelSelector=app%3Dautopilot".format(
                AUTOPILOT_NAMESPACE
            )
        )
        items = pods.get("items", [])
        pod_list = []
        for item in items:
            meta = item.get("metadata", {})
            pstatus = item.get("status", {})
            cs = (pstatus.get("containerStatuses") or [{}])[0]
            pod_list.append(
                {
                    "name": meta.get("name"),
                    "phase": pstatus.get("phase"),
                    "restartCount": cs.get("restartCount"),
                }
            )
        result["pods"] = pod_list
        running = [i for i in items if i.get("status", {}).get("phase") == "Running"]
        pick = running[0] if running else (items[0] if items else None)
        if pick:
            pod_name = pick.get("metadata", {}).get("name")
    except Exception as e:  # noqa: BLE001
        result["pods"] = {"error": "{}: {}".format(type(e).__name__, e)}

    if pod_name:
        try:
            # tailLines=200 は不十分だった（2026-08-06 run #89 で last_start/last_end が
            # 両方 null になる事象を観測）。render.py (apps/autopilot/render.py) は
            # claude -p の stream-json イベント（tool_use/text/result）を 1 行ずつ出すため、
            # 込み入ったイテレーション 1 回だけで 200 行を超え、直近の
            # "iteration #N start" 行が窓の外に押し出されうる。行数ではなく時間で窓を取り、
            # ITERATION_TIMEOUT_SECONDS（apps/autopilot/deployment.yaml, 現行 3600s）
            # 1 周分 + 余裕を確実にカバーする。deployment.yaml 側の値を大きく変えたら
            # ここも合わせて見直すこと
            raw = k8s_get_text(
                "/api/v1/namespaces/{}/pods/{}/log?sinceSeconds=7200".format(
                    AUTOPILOT_NAMESPACE, pod_name
                )
            )
            result["heartbeat"] = parse_heartbeat(raw)
        except Exception as e:  # noqa: BLE001
            result["heartbeat"] = {"error": "{}: {}".format(type(e).__name__, e)}
    else:
        result["heartbeat"] = {"error": "app=autopilot の pod が見つからない"}

    return result


def github_request(method, path, token, body=None):
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "homelab-ops-health-reporter",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def ensure_branch(token, repo, branch, base_branch):
    status, _ = github_request("GET", "/repos/{}/git/ref/heads/{}".format(repo, branch), token)
    if status == 200:
        return
    status, base = github_request(
        "GET", "/repos/{}/git/ref/heads/{}".format(repo, base_branch), token
    )
    if status != 200:
        raise RuntimeError("base branch ref の取得に失敗: {} {}".format(status, base))
    base_sha = base["object"]["sha"]
    status, resp = github_request(
        "POST",
        "/repos/{}/git/refs".format(repo),
        token,
        {"ref": "refs/heads/{}".format(branch), "sha": base_sha},
    )
    if status not in (200, 201):
        raise RuntimeError("branch 作成に失敗: {} {}".format(status, resp))


def put_file(token, repo, branch, path, content_bytes, message):
    status, existing = github_request(
        "GET", "/repos/{}/contents/{}?ref={}".format(repo, path, branch), token
    )
    sha = existing.get("sha") if status == 200 and isinstance(existing, dict) else None
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    status, resp = github_request(
        "PUT", "/repos/{}/contents/{}".format(repo, path), token, payload
    )
    if status not in (200, 201):
        raise RuntimeError("{} の書き込みに失敗: {} {}".format(path, status, resp))


def get_raw_content(token, repo, path, ref):
    # Contents API の JSON レスポンスは 1MB を超えると content フィールドを返さない。
    # raw メディアタイプで直接バイト列を取得すればこの上限を回避できる
    req = urllib.request.Request(
        "https://api.github.com/repos/{}/contents/{}?ref={}".format(repo, path, ref),
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github.raw+json",
            "User-Agent": "homelab-ops-health-reporter",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def append_line(token, repo, branch, path, line_bytes, message):
    status, meta = github_request(
        "GET", "/repos/{}/contents/{}?ref={}".format(repo, path, branch), token
    )
    if status == 200 and isinstance(meta, dict):
        sha = meta.get("sha")
        raw_status, existing = get_raw_content(token, repo, path, branch)
        if raw_status != 200:
            raise RuntimeError("{} の既存内容取得に失敗: {}".format(path, raw_status))
    elif status == 404:
        sha, existing = None, b""
    else:
        raise RuntimeError("{} のメタデータ取得に失敗: {} {}".format(path, status, meta))

    payload = {
        "message": message,
        "content": base64.b64encode(existing + line_bytes).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    status, resp = github_request(
        "PUT", "/repos/{}/contents/{}".format(repo, path), token, payload
    )
    if status not in (200, 201):
        raise RuntimeError("{} への追記に失敗: {} {}".format(path, status, resp))


def main():
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ.get("GITHUB_REPO", "hikuohiku/homelab")
    branch = os.environ.get("REPORT_BRANCH", "ops-health-report")
    base_branch = os.environ.get("BASE_BRANCH", "main")

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "generated_at": generated_at,
        "applications": collect(collect_applications),
        "pod_issues": collect(collect_pod_issues),
        "pvcs": collect(collect_pvcs),
        "nodes": collect(collect_nodes),
        "pod_metrics": collect(collect_pod_metrics),
        "node_metrics": collect(collect_node_metrics),
        "pvc_usage": collect(collect_pvc_usage),
        "autopilot": collect(collect_autopilot_health),
        "notes": (
            "コンテナ/ノードの実メモリ・CPU 使用量は metrics-server (metrics.k8s.io) から取得 "
            "(pod_metrics/node_metrics)。PVC の実ディスク使用量は namespace ごとの pvc-usage-reporter "
            "CronJob が ConfigMap に書いた値を pvc_usage として集約（immich/vaultwarden/coder のみ。"
            "Coder workspace ごとの動的 PVC は対象外、T-0078 参照）。RBAC は get/list/(configmaps のみ get) "
            "で、write 系の verb は含まない。このファイルは最新1点のみで上書きされる。ピーク値の傾向を"
            "見るには ops/health/history/YYYY-MM-DD.jsonl（1行1回分、このレポートと同じ内容）を辿ること"
            "（T-0083）。 nodes[].allocatable/capacity の ephemeral-storage は kubelet が一時ストレージ "
            "監視用に計算する値であり、ルートファイルシステムの全体サイズとは別物（node01 の root "
            "ファイルシステムは実際には約252GiBあるが、この値は約48.9GiBしか出ない）。ディスク容量の "
            "確認にこの値を使わないこと（T-0079, issue #56 2026-08-05 15:45:04 参照）。"
            " autopilot キーは autopilot 自身（namespace autopilot）の健全性。"
            "heartbeat.last_end が無い/古い、または exit_code が非 0 なら前回のイテレーションが"
            "異常終了またはハング中の疑い（T-0110）。pods/log は autopilot namespace に閉じた"
            "Role でのみ許可し、心拍行だけを正規表現で抽出している。生ログはここに含まれない。"
        ),
    }

    ensure_branch(token, repo, branch, base_branch)
    put_file(
        token,
        repo,
        branch,
        "ops/health/latest.json",
        json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
        "health report {}".format(generated_at),
    )
    print("ops/health/latest.json を {} ブランチへ書き込みました ({})".format(branch, generated_at))

    history_path = "ops/health/history/{}.jsonl".format(generated_at[:10])
    append_line(
        token,
        repo,
        branch,
        history_path,
        (json.dumps(report, ensure_ascii=False) + "\n").encode("utf-8"),
        "health report history {}".format(generated_at),
    )
    print("{} に追記しました ({})".format(history_path, generated_at))


if __name__ == "__main__":
    main()
