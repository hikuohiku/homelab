"""private-data 分離の exfiltration ドリル (P-0243)。

in-cluster で実際に 2 本の使い捨て Pod を立て、擬似持ち出しを試みる:

  - labeled  : ラベル private-data=true 付き → 外部 HTTPS がタイムアウト/拒否され、
               DNS 解決だけは通る (= 拒否は「名前解決不能」ではなくポリシー由来)
  - control  : ラベル無し → 同一送信が成功する (ネットワーク全体が死んでいるのでは
               なく、ポリシーが拒否したことの反証)

ドリル自体が NetworkPolicy private-data-drill を一時的に敷き、終わったら
try/finally + SIGINT/SIGTERM ハンドラで Pod と一緒に必ず消す。それでも残骸が
出た場合に備え、Pod 側にも activeDeadlineSeconds を付けてある (自死)。

stdlib のみ (kubectl バイナリ不要 — ops/heart/k8s.py と同型の薄いクライアント)。
走らせ方 (リポジトリルートから、in-cluster の writer 権限で):

  python3 ops/profiles/private-data/exfil_drill.py --report /tmp/opencode/exfil-drill.json

終了コード: 全判定 true で 0。結果 JSON のトップレベルキー:
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed
"""

import argparse
import json
import signal
import ssl
import sys
import time
import urllib.error
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"

POLICY_NAME = "private-data-drill"
POD_LABELED = "exfil-drill-labeled"
POD_CONTROL = "exfil-drill-control"

TARGET_URL = "https://example.com/"
IMAGE = "python:3.14-alpine"
HTTPS_TIMEOUT = 8          # 秒。既定拒否下は SYN 黒穴になり timeout で落ちる
POD_DEADLINE = 180         # Pod 側の自死 (activeDeadlineSeconds)
POLL_DEADLINE = 240        # ドリル全体の待ち上限
POLL_INTERVAL = 5

# Pod 内で走る観測スクリプト。DNS 解決と HTTPS GET の成否だけを JSON 1 行で吐く。
# example.com は安定した無害な的。持ち出すデータは存在しない (擬似持ち出し)。
# (str.format は JSON の波括幅と衝突するので連結で組み立てる)
PROBE = (
    "import json, socket, urllib.error, urllib.request\n"
    'out = {"dns_ok": False, "https_ok": False, "status": None, "error": None}\n'
    "try:\n"
    '    infos = socket.getaddrinfo("example.com", 443, proto=socket.IPPROTO_TCP)\n'
    "    out[\"dns_ok\"] = bool(infos)\n"
    "except Exception as e:\n"
    '    out["error"] = f"dns: {type(e).__name__}: {e}"\n'
    "if out[\"dns_ok\"]:\n"
    "    try:\n"
    "        r = urllib.request.urlopen(" + repr(TARGET_URL) + ", timeout="
    + str(HTTPS_TIMEOUT) + ")\n"
    '        out["status"] = r.status\n'
    '        out["https_ok"] = True\n'
    "        r.close()\n"
    "    except Exception as e:\n"
    '        out["error"] = f"https: {type(e).__name__}: {e}"\n'
    "print(json.dumps(out))\n"
)


class K8sError(Exception):
    def __init__(self, status, body):
        super().__init__(f"k8s API {status}: {body[:300]}")
        self.status = status


class K8s:
    """ServiceAccount トークン + urllib のみの薄いクライアント (ops/heart/k8s.py 同型)。"""

    def __init__(self, sa_dir=SA_DIR, api=API):
        with open(f"{sa_dir}/token") as f:
            self._token = f.read().strip()
        with open(f"{sa_dir}/namespace") as f:
            self.namespace = f.read().strip()
        self._ctx = ssl.create_default_context(cafile=f"{sa_dir}/ca.crt")
        self._api = api

    def request(self, method, path, body=None, raw=False):
        req = urllib.request.Request(
            self._api + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                data = resp.read()
                return data.decode(errors="replace") if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            raise K8sError(e.code, e.read().decode(errors="replace")) from e


def policy_spec(namespace):
    """ops/profiles/private-data/networkpolicy.yaml (本番 private-data-egress-lock)
    と同一意味論の一時コピー。名前だけドリル専用にして、掃除で本番物を壊さない。"""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": POLICY_NAME, "namespace": namespace,
                     "labels": {"app": "exfil-drill"}},
        "spec": {
            "podSelector": {"matchLabels": {"private-data": "true"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                {
                    "to": [{
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "kube-system"},
                        },
                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                    }],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                }
            ],
        },
    }


def pod_spec(k8s, name, labeled):
    labels = {"app": "exfil-drill", "exfil-drill/run-role": "labeled" if labeled else "control"}
    if labeled:
        # 本体。この 1 行で NetworkPolicy の選択対象になる
        labels["private-data"] = "true"
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": k8s.namespace, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "activeDeadlineSeconds": POD_DEADLINE,
            "terminationGracePeriodSeconds": 5,
            "containers": [{
                "name": "probe",
                "image": IMAGE,
                "command": ["python3", "-c", PROBE],
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
            }],
        },
    }


def delete_ignore_404(k8s, path):
    try:
        k8s.request("DELETE", path + "?propagationPolicy=Foreground&gracePeriodSeconds=0")
    except K8sError as e:
        if e.status != 404:
            raise


def ensure_policy(k8s):
    delete_ignore_404(
        k8s, f"/apis/networking.k8s.io/v1/namespaces/{k8s.namespace}/networkpolicies/{POLICY_NAME}"
    )
    for _ in range(10):  # 削除反映待ち (409 再挑戦)
        try:
            k8s.request("POST",
                        f"/apis/networking.k8s.io/v1/namespaces/{k8s.namespace}/networkpolicies",
                        policy_spec(k8s.namespace))
            return
        except K8sError as e:
            if e.status == 409:
                time.sleep(1)
                continue
            raise
    raise RuntimeError("drill NetworkPolicy の作成が 409 を抜けられなかった")


def pod_phase(k8s, name):
    p = k8s.request("GET", f"/api/v1/namespaces/{k8s.namespace}/pods/{name}")
    return p.get("status", {}).get("phase", ""), p


def wait_terminal(k8s, names):
    deadline = time.monotonic() + POLL_DEADLINE
    done = {}
    while len(done) < len(names):
        for n in names:
            if n in done:
                continue
            phase, pod = pod_phase(k8s, n)
            if phase in ("Succeeded", "Failed"):
                cs = (pod.get("status", {}).get("containerStatuses") or [{}])[0]
                exit_code = ((cs.get("state") or {}).get("terminated") or {}).get("exitCode")
                done[n] = {"phase": phase, "exit_code": exit_code}
        if len(done) < len(names):
            if time.monotonic() > deadline:
                break
            time.sleep(POLL_INTERVAL)
    return done


def fetch_result(k8s, name):
    """Pod ログから PROBE の JSON 1 行を拾う。"""
    try:
        log = k8s.request("GET", f"/api/v1/namespaces/{k8s.namespace}/pods/{name}/log", raw=True)
    except K8sError as e:
        return {"dns_ok": False, "https_ok": False, "error": f"log: {e}"}
    for line in reversed(log.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    tail = "\n".join(log.splitlines()[-5:])
    return {"dns_ok": False, "https_ok": False, "error": f"no probe json; tail:\n{tail}"}


def cleanup(k8s, report):
    """Pod とドリル用 NP を消し、消えたことを 404 で確認してから cleaned_up を出す。"""
    base = f"/api/v1/namespaces/{k8s.namespace}/pods"
    paths = [
        f"{base}/{POD_LABELED}",
        f"{base}/{POD_CONTROL}",
        f"/apis/networking.k8s.io/v1/namespaces/{k8s.namespace}/networkpolicies/{POLICY_NAME}",
    ]
    for p in paths:
        delete_ignore_404(k8s, p)
    remaining = list(paths)
    deadline = time.monotonic() + 60
    while remaining and time.monotonic() < deadline:
        time.sleep(2)
        left = []
        for p in remaining:
            try:
                k8s.request("GET", p)
                left.append(p)  # まだ見える
            except K8sError as e:
                if e.status != 404:
                    raise
        remaining = left
    report["cleaned_up"] = not remaining
    if remaining:
        report["cleanup_remaining"] = remaining


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", help="結果 JSON の書き出し先")
    args = ap.parse_args()

    report = {
        "drill": "private-data exfiltration drill (P-0243)",
        "target": TARGET_URL,
        "image": IMAGE,
        "policy": POLICY_NAME,
        "labeled_blocked": False,
        "unlabeled_allowed": False,
        "dns_ok_labeled": False,
        "dns_ok_control": False,
        "cleaned_up": False,
        "all_passed": False,
    }

    def bail(signum, _frame):
        raise SystemExit(f"signal {signum} 受信 — finally 掃除に倒れる")

    signal.signal(signal.SIGINT, bail)
    signal.signal(signal.SIGTERM, bail)

    rc = 2
    k8s = None
    try:
        k8s = K8s()
        report["namespace"] = k8s.namespace
        report["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ensure_policy(k8s)
        k8s.request("POST", f"/api/v1/namespaces/{k8s.namespace}/pods",
                    pod_spec(k8s, POD_LABELED, labeled=True))
        k8s.request("POST", f"/api/v1/namespaces/{k8s.namespace}/pods",
                    pod_spec(k8s, POD_CONTROL, labeled=False))

        statuses = wait_terminal(k8s, [POD_LABELED, POD_CONTROL])
        labeled = fetch_result(k8s, POD_LABELED)
        control = fetch_result(k8s, POD_CONTROL)
        report["pods"] = {
            "labeled": dict(statuses.get(POD_LABELED, {}), **{"probe": labeled}),
            "control": dict(statuses.get(POD_CONTROL, {}), **{"probe": control}),
        }

        # 判定本体。DNS が通って初めて「ポリシーによる拒否」と言える
        # (P-0224/P-0233 知見)。対照群の成功が「ネットワーク全体でなくポリシー由来」の反証
        report["dns_ok_labeled"] = labeled.get("dns_ok") is True
        report["labeled_blocked"] = (
            report["dns_ok_labeled"] and labeled.get("https_ok") is not True
        )
        report["dns_ok_control"] = control.get("dns_ok") is True
        report["unlabeled_allowed"] = (
            report["dns_ok_control"] and control.get("https_ok") is True
        )

        checks = all(report[k] for k in (
            "labeled_blocked", "unlabeled_allowed", "dns_ok_labeled", "dns_ok_control",
        )) and all(s.get("phase") == "Succeeded" and s.get("exit_code") == 0
                  for s in (report["pods"]["control"],))
        report["probes_conclusive"] = checks
    except Exception as e:  # noqa: BLE001 — 最後まで掃除してから死ぬのが仕事
        report["error"] = f"{type(e).__name__}: {e}"
        print(f"drill error: {report['error']}", file=sys.stderr)
    finally:
        if k8s is not None:
            try:
                cleanup(k8s, report)
            except Exception as e:  # noqa: BLE001
                report["cleanup_error"] = f"{type(e).__name__}: {e}"
                report["cleaned_up"] = False
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        report["all_passed"] = all(report[k] for k in (
            "labeled_blocked", "unlabeled_allowed", "dns_ok_labeled",
            "dns_ok_control", "cleaned_up",
        ))
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
                f.write("\n")
        print(json.dumps({k: report[k] for k in (
            "labeled_blocked", "unlabeled_allowed", "dns_ok_labeled",
            "dns_ok_control", "cleaned_up", "all_passed")}, ensure_ascii=False))
        rc = 0 if report["all_passed"] else 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
