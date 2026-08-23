"""毎晩 inventory 全対象の上流最新版を観測し、ops/health/latest.json の version_drift キーへ
畳み込む (P-0126)。

判定エンジンは ops/tools/version_watch.py (同ディレクトリの手動同期コピーを import。
kustomize の configMapGenerator は kustomization.yaml の置かれたディレクトリの外の
ファイルを参照できないため)。inventory は実行時に base ブランチ (main) の raw から
取りに行く — スナップショットを ConfigMap に焼くより単一の情報源を直接読む方が陳腐化しない。

書き込み先は ops-health-reporter と共有の latest.json。相手は 30 分ごとに全体上書きする
ため、こちらは GET→merge→PUT にして、Contents API の SHA 楽観排他 (409/422) で衝突されたら
再取得してリトライする。夜間 1 回対 30 分周期なので衝突確率は低いが、潰せるものは潰す。

標準ライブラリのみで動く (イメージに pip install を要求しない)。
apps/ops-health-reporter/report.py を鋳型にしている。
"""

import base64
import datetime
import functools
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import version_watch

LATEST_PATH = "ops/health/latest.json"
HISTORY_DIR = "ops/health/history"
INVENTORY_PATH = "ops/inventory.json"
# 1 リクエストあたりの上限。version_watch.http_get の既定 (30s) より短くして
# 全対象 × timeout が CronJob の activeDeadlineSeconds (600s) に収まるようにする
PER_REQUEST_TIMEOUT = 15
# SHA 衝突時のリトライ上限と待ち。衝突相手の書き込み周期は最短 30 分なので
# 数回の再取得で必ず抜けられるはず
MAX_WRITE_ATTEMPTS = 4
RETRY_WAIT_SECONDS = 10


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
            "User-Agent": "homelab-version-watcher",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


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


def observe(token, repo, ref, now):
    """inventory を読んで全対象を観測し、latest.json / history に載せる分の dict を返す。"""
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
        # 差分ありの日に briefing へ畳むのは latest.json を読む autopilot 側の仕事で、
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


def put_with_retry(token, repo, branch, path, compose, message):
    """existing bytes -> new bytes の compose を適用して PUT する (SHA 衝突時に再取得リトライ)。

    Contents API の PUT は SHA ベースの楽観排他。GET→compose→PUT の間に
    ops-health-reporter (30 分周期の全体上書き) が触れると 409/422 になるため、
    その場合だけ既存内容の取り直しからやり直す
    """
    for attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
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
            "content": base64.b64encode(compose(existing)).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        status, resp = github_request(
            "PUT", "/repos/{}/contents/{}".format(repo, path), token, payload
        )
        if status in (200, 201):
            return
        if status in (409, 422) and attempt < MAX_WRITE_ATTEMPTS:
            time.sleep(RETRY_WAIT_SECONDS)
            continue
        raise RuntimeError("{} の書き込みに失敗: {} {}".format(path, status, resp))


def main():
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ.get("GITHUB_REPO", "hikuohiku/homelab")
    branch = os.environ.get("REPORT_BRANCH", "ops-health-report")
    base_branch = os.environ.get("BASE_BRANCH", "main")

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    observation = observe(token, repo, base_branch, now)
    s = observation["summary"]
    print(
        "観測完了: total={} drifted={} errors={} uncomparable={}".format(
            s["total"], s["drifted"], s["errors"], s["uncomparable"]
        )
    )

    ensure_branch(token, repo, branch, base_branch)

    def merge_latest(existing):
        try:
            merged = json.loads(existing.decode("utf-8"))
            if not isinstance(merged, dict):
                raise ValueError("top-level が object でない")
        except (UnicodeDecodeError, ValueError):
            # latest.json が壊れていた場合は version_drift だけの新ファイルで置き換える。
            # health 部分は ops-health-reporter が 30 分以内に全体上書きして復元する
            merged = {}
        merged["version_drift"] = observation
        return json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")

    put_with_retry(
        token, repo, branch, LATEST_PATH, merge_latest, "version drift report {}".format(now)
    )
    print("{} の version_drift を更新しました ({})".format(LATEST_PATH, branch))

    line = (json.dumps(observation, ensure_ascii=False) + "\n").encode("utf-8")
    history_path = "{}/{}.jsonl".format(HISTORY_DIR, now[:10])
    put_with_retry(
        token,
        repo,
        branch,
        history_path,
        lambda existing: existing + line,
        "version drift history {}".format(now),
    )
    print("{} に追記しました".format(history_path))


if __name__ == "__main__":
    main()
