#!/usr/bin/env python3
"""P-0192 の問いかけ 1 通をクラスタ内の 1 回限り Job から送るランナー。

worker サンドボックスには Telegram / cluster の credential が無い (2026-08-23
実測。ops/projects/logs/P-0192/PROGRESS.md セッション 1・2) ため、送信本体は
autopilot ns の Secret `telegram-adapter-credentials` を mount したこの Job が
担う。設計は PROJECT.md 作り方 1・2 のとおり:

- 送信文・応答処理は ops/tools/wish_seeds.py (ConfigMap 内の同内容コピー) を
  import して使う。本文の単一ソースはそちら
- **追加送信なし** (spec 予算規則) の担保は 3 層:
  1. 証跡 ask-evidence.json が main / project ブランチのどこかに既にある → 送らない
  2. pending マーカー ask-pending.json が既にある (= 過去に送信を試みた) → 送らない
     (送信直前に書くので、「送信したが証跡を書く前に死んだ」再実行でも二重送信しない。
      fail-safe 側に倒す。この状態は rc=1 で騒ぐ。証跡が無いまま黙るのは DoD 未達の隠蔽)
  3. 書き込み先ブランチが存在しない (= 証跡を記録できる保証が無い) → 送らないで rc=1
- 証跡と pending は Contents API で project ブランチ (merge 後の再実行時は main に
  証跡があるのでそこで止まる) へ書き戻す。message_id は Telegram 応答の実測値

HTTP 層は urlopen 注入で差し替え可能。固定テストは
ops/tests/test_wish_seeds_job.py (ネットワークに出ない)。
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wish_seeds import build_evidence, compose_ask, send_telegram  # noqa: E402

GITHUB_API = "https://api.github.com"

# 環境変数の既定値。EVIDENCE_BRANCH は merge 前の実行 (preview 由来) を想定した
# 書き込み先。merge 後の再実行では main の証跡を見つけて何もしないので使われない
REPO_DEFAULT = "hikuohiku/homelab"
BRANCH_DEFAULT = "project/p-0192"
EVIDENCE_PATH_DEFAULT = "ops/projects/logs/P-0192/ask-evidence.json"
PENDING_PATH_DEFAULT = "ops/projects/logs/P-0192/ask-pending.json"


def now_iso():
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def github_request(method, repo, api_path, token, payload=None, query="",
                   urlopen=None):
    """GitHub API 1 リクエスト。(status, body dict) を返す。

    api_path は /repos/{repo}/ の後ろ全体 (例: contents/<path>、branches/<ref>)。
    urlopen 注入は test_wish_seeds.py と同じ流儀。404 は「無い」を表す正常系として
    status で返す (raise しない)。それ以外の HTTPError と URLError は握らない
    (Job として落ちて可視化する)。
    """
    if urlopen is None:
        urlopen = urllib.request.urlopen
    url = "{}/repos/{}/{}{}".format(GITHUB_API, repo, api_path, query)
    data = None
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Accept": "application/vnd.github+json",
        "User-Agent": "wish-seeds-p0192-job",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, {}
        raise


def file_exists(repo, ref, path, token, urlopen=None):
    """ref (branch 名) 上に path があるか。404 以外の異常は握らない。"""
    status, _ = github_request(
        "GET", repo, "contents/{}".format(path), token,
        query="?ref={}".format(ref), urlopen=urlopen,
    )
    return status == 200


def put_file(repo, branch, path, token, content_bytes, message, urlopen=None):
    """branch 上の path にファイルを 1 個作成 (新規前提。sha 付き上書きも対応)。"""
    api_path = "contents/{}".format(path)
    status, existing = github_request(
        "GET", repo, api_path, token,
        query="?ref={}".format(branch), urlopen=urlopen,
    )
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
        "branch": branch,
    }
    if status == 200 and existing.get("sha"):
        payload["sha"] = existing["sha"]
    status, body = github_request(
        "PUT", repo, api_path, token, payload=payload, urlopen=urlopen,
    )
    if status not in (200, 201):
        raise RuntimeError(
            "Contents API PUT が {} を返した: {}".format(status, body)
        )
    return body


def decide_send(repo, refs, evidence_path, pending_path, token, urlopen=None):
    """送ってよいかを判定する (純関数寄り。副作用なし)。

    戻り値は (decision, detail):
    - ("skip", ...)   既に送信済みの証拠がある。何もしないのが正。rc=0
    - ("abort", ...)  証跡を記録できない/過去の送信が未記録。送ってはいけない。rc=1
    - ("send", "")    送信してよい
    refs は先頭から見る (例: ["main", branch])。
    """
    for ref in refs:
        for kind, path in (("evidence", evidence_path), ("pending", pending_path)):
            if file_exists(repo, ref, path, token, urlopen=urlopen):
                if kind == "evidence":
                    return (
                        "skip",
                        "{} に証跡 {} がある。1 通きりの募集なので送らない".format(
                            ref, path
                        ),
                    )
                return (
                    "abort",
                    "{} に pending {} がある (= 送信を試みた形跡) が証跡がない。"
                    "二重送信を避けて送らない。Telegram の送信履歴で実施確認の上、"
                    "証跡を手で補うこと".format(ref, path),
                )
    write_ref = refs[-1]
    # ブランチの存在確認 (refs[-1] = 書き込み先)。refs には main も入るので
    # 「main の GET」ではなく branches API を見る
    status, _ = github_request(
        "GET", repo, "branches/{}".format(write_ref), token,
        urlopen=urlopen,
    )
    if status != 200:
        return (
            "abort",
            "書き込み先ブランチ {} が存在しない (merge 済み・削除済み?)。"
            "証跡を記録できる保証が無いため送らない".format(write_ref),
        )
    return "send", ""


def read_env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise SystemExit("{} 未設定。Job の env を確認すること".format(name))
    return value


def main(argv=None, urlopen=None):
    repo = os.environ.get("GITHUB_REPO", "").strip() or REPO_DEFAULT
    branch = os.environ.get("EVIDENCE_BRANCH", "").strip() or BRANCH_DEFAULT
    evidence_path = (
        os.environ.get("EVIDENCE_PATH_IN_REPO", "").strip() or EVIDENCE_PATH_DEFAULT
    )
    pending_path = (
        os.environ.get("PENDING_PATH_IN_REPO", "").strip() or PENDING_PATH_DEFAULT
    )
    token = os.environ.get("AUTOPILOT_GITHUB_TOKEN", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("AUTOPILOT_GITHUB_TOKEN", token),
            ("TELEGRAM_BOT_TOKEN", tg_token),
            ("TELEGRAM_ALLOWED_USER_ID", chat_id),
        )
        if not value
    ]
    if missing:
        print(
            "env 未設定: {} (Secret telegram-adapter-credentials 由来)".format(
                ", ".join(missing)
            ),
            file=sys.stderr,
        )
        return 1

    decision, detail = decide_send(
        repo, ["main", branch], evidence_path, pending_path, token,
        urlopen=urlopen,
    )
    if decision != "send":
        print("{}: {}".format(decision, detail))
        return 0 if decision == "skip" else 1

    # 送信直前に pending を書く (クラッシュしても次の再実行が二重送信しない)
    pending_body = {
        "project": "P-0192",
        "started_at": now_iso(),
        "note": "送信試行中のマーカー。完了後も残す (二重送信の歯止め)",
    }
    put_file(
        repo, branch, pending_path, token,
        json.dumps(pending_body, ensure_ascii=False, indent=2).encode() + b"\n",
        "P-0192: 問いかけ送信の開始マーカー ({})".format(pending_body["started_at"]),
        urlopen=urlopen,
    )

    payload = send_telegram(tg_token, chat_id, compose_ask(), urlopen=urlopen)
    message_id = (payload.get("result") or {}).get("message_id")
    evidence = build_evidence(message_id=message_id, chat_id=chat_id)
    evidence["via"] = "job"
    evidence["evidence_branch"] = branch
    evidence_body = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"

    commit = put_file(
        repo, branch, evidence_path, token,
        evidence_body.encode(),
        "P-0192: 問いかけ送信の証跡 (message_id={})".format(message_id),
        urlopen=urlopen,
    )
    print(
        "送信した: message_id={} -> {}/{}/{} (commit {})".format(
            message_id, repo, branch, evidence_path,
            (commit.get("commit") or {}).get("sha", "?")[:12],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
