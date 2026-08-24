"""Kubernetes API の薄いクライアント。

ServiceAccount トークン + urllib のみ (kubectl バイナリ不要)。
apps/coder/workspace-home-backup-cronjob.yaml の spawn_backup_jobs.py と同型。
"""

import json
import ssl
import urllib.error
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"


class K8sError(Exception):
    def __init__(self, status, body):
        super().__init__(f"k8s API {status}: {body[:300]}")
        self.status = status


class K8s:
    def __init__(self, sa_dir=SA_DIR, api=API):
        with open(f"{sa_dir}/token") as f:
            self._token = f.read().strip()
        self._ctx = ssl.create_default_context(cafile=f"{sa_dir}/ca.crt")
        self._api = api

    def request(self, method, path, body=None, content_type="application/json"):
        req = urllib.request.Request(
            self._api + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise K8sError(e.code, e.read().decode(errors="replace")) from e

    def list_jobs(self, namespace, label_selector=None):
        path = f"/apis/batch/v1/namespaces/{namespace}/jobs"
        if label_selector:
            path += f"?labelSelector={label_selector}"
        return self.request("GET", path).get("items", [])

    def create_job(self, namespace, job):
        return self.request("POST", f"/apis/batch/v1/namespaces/{namespace}/jobs", job)

    def delete_job(self, namespace, name):
        # propagationPolicy=Foreground で Pod も一緒に消す (veto の即 kill 用)
        return self.request(
            "DELETE",
            f"/apis/batch/v1/namespaces/{namespace}/jobs/{name}"
            "?propagationPolicy=Foreground",
        )

    def get_configmap(self, namespace, name):
        return self.request("GET", f"/api/v1/namespaces/{namespace}/configmaps/{name}")

    def list_pods(self, namespace, label_selector=None):
        path = f"/api/v1/namespaces/{namespace}/pods"
        if label_selector:
            path += f"?labelSelector={label_selector}"
        return self.request("GET", path).get("items", [])

    # --- カスタムリソース (Project CR。設計 state-out-of-git Phase 4) ---
    def list_custom(self, api_version, namespace, plural):
        return self.request(
            "GET", f"/apis/{api_version}/namespaces/{namespace}/{plural}"
        ).get("items", [])

    def apply_custom(self, api_version, namespace, plural, name, body):
        """server-side apply。存在しなければ作り、あれば heart の持ち分を上書きする。

        create/replace の 2 段構えにしないのは、resourceVersion の読み合わせが
        要らず 1 往復で済むため。force=true は他の書き手と競合したときに heart を
        勝たせる — heart が唯一の書き手であることは RBAC で担保している
        """
        return self.request(
            "PATCH",
            f"/apis/{api_version}/namespaces/{namespace}/{plural}/{name}"
            "?fieldManager=heart&force=true",
            body,
            content_type="application/apply-patch+yaml",
        )
