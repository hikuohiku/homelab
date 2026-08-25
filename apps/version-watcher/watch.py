"""毎晩 inventory 全対象の上流最新版を観測し、ConfigMap `version-watcher/version-drift`
の report.json キーへ書く (P-0126)。

判定エンジンは ops/tools/version_watch.py (同ディレクトリの手動同期コピーを import。
kustomize の configMapGenerator は kustomization.yaml の置かれたディレクトリの外の
ファイルを参照できないため)。inventory は実行時に base ブランチ (main) の raw から
取りに行く — スナップショットを ConfigMap に焼くより単一の情報源を直接読む方が陳腐化しない。
GitHub を読むのはこの 1 点だけで、書き込みは一切しない。

書き先が git だった名残: 以前は健全性レポートと同じ ops-health-report ブランチの
latest.json に GET→merge→PUT で相乗りしていた。レポートが ConfigMap へ移った
(state-out-of-git Phase 5) 時点でその latest.json は誰にも読まれなくなり、
version_drift だけが行き先の無い枝に積もっていた。産出側が自 namespace の専用
ConfigMap に書き、ops-health-reporter (cluster-wide read-only) が集約する形
(pvc-usage-report / download-budget / dashboard-smoke と同じ分離) に揃える。

標準ライブラリのみで動く (イメージに pip install を要求しない)。
apps/ops-health-reporter/report.py を鋳型にしている。
"""

import datetime
import functools
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import version_watch

INVENTORY_PATH = "ops/inventory.json"
# 書き先。自 namespace の専用 ConfigMap 1 個だけ (RBAC もこの名前に絞ってある)。
# ArgoCD には宣言しない — 宣言すると selfHeal が毎回書き戻して綱引きになる
# (report.py の put_configmap 参照)
REPORT_CONFIGMAP = "version-drift"
REPORT_KEY = "report.json"
# 1 リクエストあたりの上限。version_watch.http_get の既定 (30s) より短くして
# 全対象 × timeout (dockerhub は対象あたり 2 リクエスト。合計 49) が CronJob の
# activeDeadlineSeconds (900s) に収まるようにする
PER_REQUEST_TIMEOUT = 15

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
K8S_BASE = "https://{}:{}".format(K8S_HOST, K8S_PORT)


def k8s_request(method, path, body=None):
    with open(os.path.join(SA_DIR, "token")) as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        K8S_BASE + path,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def put_configmap(namespace, name, data, request=None):
    """GET → resourceVersion 付き PUT、無ければ POST 作成 (report.py と同じ形)。"""
    call = request or k8s_request
    path = "/api/v1/namespaces/{}/configmaps/{}".format(namespace, name)
    status, existing = call("GET", path)
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace},
        "data": data,
    }
    if status == 200 and isinstance(existing, dict):
        body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        status, resp = call("PUT", path, body)
    else:
        status, resp = call(
            "POST", "/api/v1/namespaces/{}/configmaps".format(namespace), body
        )
    if status not in (200, 201):
        raise RuntimeError("configmap 書き込みに失敗: {} {}".format(status, resp))


def get_raw_content(token, repo, path, ref):
    # Contents API の JSON レスポンスは 1MB を超えると content フィールドを返さない。
    # raw メディアタイプで直接バイト列を取得すればこの上限を回避できる
    req = urllib.request.Request(
        "https://api.github.com/repos/{}/contents/{}?ref={}".format(repo, path, ref),
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github.raw+json",
            "User-Agent": "homelab-version-watcher",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def observe(token, repo, ref, now):
    """inventory を読んで全対象を観測し、ConfigMap に載せる dict を返す。"""
    status, raw = get_raw_content(token, repo, INVENTORY_PATH, ref)
    if status != 200:
        raise RuntimeError("{} の取得に失敗: {} (ref={})".format(INVENTORY_PATH, status, ref))
    targets = json.loads(raw.decode("utf-8"))["targets"]
    fetch = functools.partial(version_watch.http_get, timeout=PER_REQUEST_TIMEOUT)
    results = version_watch.check_all(targets, fetch)
    return {
        "generated_at": now,
        "summary": version_watch.summarize(results),
        # drift の列は PROJECT.md 既定の id / current / latest / upstream の 4 キー。
        # 差分ありの日に briefing へ畳むのは健全性レポートを読む autopilot 側の仕事で、
        # watcher は記録まで
        "drifted": [
            {k: r[k] for k in ("id", "current", "latest", "upstream")}
            for r in results
            if r.get("drifted")
        ],
        # 個別 target の取得失敗も隠さず載せる (「エラー 0 件」を見せかけない)
        "errors": [
            {"id": r["id"], "error": r["error"]}
            for r in results
            if r["status"] == "error"
        ],
    }


def main():
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ.get("GITHUB_REPO", "hikuohiku/homelab")
    base_branch = os.environ.get("BASE_BRANCH", "main")
    with open(os.path.join(SA_DIR, "namespace")) as f:
        namespace = f.read().strip()

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    observation = observe(token, repo, base_branch, now)
    s = observation["summary"]
    print(
        "観測完了: total={} drifted={} errors={} uncomparable={}".format(
            s["total"], s["drifted"], s["errors"], s["uncomparable"]
        )
    )

    put_configmap(
        namespace,
        REPORT_CONFIGMAP,
        {REPORT_KEY: json.dumps(observation, ensure_ascii=False, indent=2)},
    )
    print(
        "{}/{} の {} を更新しました ({})".format(
            namespace, REPORT_CONFIGMAP, REPORT_KEY, now
        )
    )


if __name__ == "__main__":
    main()
