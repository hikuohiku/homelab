"""GitHub REST の薄いクライアント (urllib のみ)。

apps/ops-health-reporter/report.py と同型。per_page=100 を必ず明示する
(CHARTER の教訓: 明示しない一覧は一部しか返さない)。
"""

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GhError(Exception):
    def __init__(self, status, body):
        super().__init__(f"github API {status}: {body[:300]}")
        self.status = status
        self.body = body
        # 失敗の理由は呼び出し側 (heart) が状態に刻み、reconcile が判定に使う。
        # JSON の message だけを取り出しておく (本文全体は body に残す)
        try:
            self.message = str(json.loads(body).get("message") or body)
        except Exception:
            self.message = body


class Gh:
    def __init__(self, token, repo):
        self._token = token
        self.repo = repo

    def request_bytes(self, method, path, body=None,
                      accept="application/vnd.github+json"):
        req = urllib.request.Request(
            API + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise GhError(e.code, e.read().decode(errors="replace")) from e

    def request(self, method, path, body=None, accept="application/vnd.github+json"):
        data = self.request_bytes(method, path, body, accept)
        return json.loads(data) if data else None

    def file_at_ref(self, path, ref):
        """ブランチ上のファイル 1 本を生のまま読む。無ければ None。

        git の object を落とさずに 1 リクエストで済ませる。読み手 (runner) が
        起動のたびに clone / fetch すると、その履歴のぶんだけ node の負荷が増える。
        """
        q = urllib.parse.quote
        try:
            data = self.request_bytes(
                "GET",
                f"/repos/{self.repo}/contents/{q(path)}?ref={q(ref)}",
                accept="application/vnd.github.raw",
            )
        except GhError as e:
            if e.status == 404:
                return None
            raise
        return (data or b"").decode(errors="replace")

    def open_prs(self):
        prs = []
        page = 1
        while True:
            batch = self.request(
                "GET", f"/repos/{self.repo}/pulls?state=open&per_page=100&page={page}"
            )
            prs.extend(batch)
            if len(batch) < 100:
                return prs
            page += 1

    def pr(self, number):
        return self.request("GET", f"/repos/{self.repo}/pulls/{number}")

    def pr_combined_status(self, sha):
        return self.request(
            "GET", f"/repos/{self.repo}/commits/{sha}/check-runs?per_page=100"
        )

    def merge_pr(self, number):
        # squash/rebase は無効化済みなので merge 固定 (CHARTER §4)
        return self.request(
            "PUT",
            f"/repos/{self.repo}/pulls/{number}/merge",
            {"merge_method": "merge"},
        )

    def issue_comments_since(self, issue, since_iso):
        path = f"/repos/{self.repo}/issues/{issue}/comments?per_page=100"
        if since_iso:
            path += f"&since={urllib.parse.quote(since_iso)}"
        comments = []
        page = 1
        while True:
            batch = self.request("GET", path + f"&page={page}")
            comments.extend(batch)
            if len(batch) < 100:
                return comments
            page += 1

    def comment_issue(self, issue, body):
        return self.request(
            "POST", f"/repos/{self.repo}/issues/{issue}/comments", {"body": body}
        )

    def delete_branch(self, branch):
        self.request("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}")
