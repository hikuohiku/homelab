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


class Gh:
    def __init__(self, token, repo):
        self._token = token
        self.repo = repo

    def request(self, method, path, body=None, accept="application/vnd.github+json"):
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
                data = resp.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as e:
            raise GhError(e.code, e.read().decode(errors="replace")) from e

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

    def ensure_branch(self, branch, base="main"):
        try:
            self.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
            return False
        except GhError as e:
            if e.status != 404:
                raise
        base_ref = self.request("GET", f"/repos/{self.repo}/git/ref/heads/{base}")
        self.request(
            "POST",
            f"/repos/{self.repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_ref["object"]["sha"]},
        )
        return True

    def delete_branch(self, branch):
        self.request("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}")
