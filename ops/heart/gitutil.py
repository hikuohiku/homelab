"""git 操作のヘルパ。

heart は 2 つの checkout を持つ:
  - repo_dir:  main の読み取り専用ミラー (毎ビート hard reset。loop.sh と同じ規律)
  - state_dir: ops-state ブランチの checkout (heart が唯一の書き手。直 push)

ops-state を main と別ブランチにするのは ops-health-report と同じ理由
(main は ruleset で直 push 不可、かつ 120s ごとの状態更新を PR にすると
main の履歴とレビュー窓が運用ノイズで埋まるため)。
"""

import subprocess
from pathlib import Path

# clone は blobless (partial clone) で打つ。状態ブランチ 4 本の履歴で .git が 124MB あり、
# 素の clone はこの回線で 65s、ops-state 1 本でも 54s かかる (2026-08-24 実測)。採択ゲートの
# clone はこれで 120s 上限を越えて落ちていた。--filter=blob:none なら 2s / 9.2MB。
# **shallow (--depth=1) にはしない。** ここの checkout は push もするし merge-base も要る。
# --single-branch は remote.origin.fetch を 1 本に固定し、以後 fetch しても
# origin/ops-state が生えない (P-0014, ops/memory/substrate.md)。
# blob は使うときに 1 つずつ取りに行く = clone 後の操作にネットワークが要ることに注意。
BLOBLESS = "--filter=blob:none"


class GitError(Exception):
    pass


def clone_args(repo_url, dest, branch=None, single_branch=False):
    """clone のコマンド列を組み立てる (純関数。テストがネットワークに出ずに検査する)。

    branch を渡すとその ref を checkout する。single_branch は「そのブランチしか
    見ない」と言い切れる用途 (state_dir) だけ。
    """
    args = ["clone", "--quiet", BLOBLESS]
    if branch:
        args += ["--branch", branch]
    if single_branch:
        args.append("--single-branch")
    return [*args, repo_url, str(dest)]


def run(args, cwd=None, check=True):
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def sync_main(repo_dir, repo_url):
    """repo_dir を origin/main の最新に一致させる。返り値は main の SHA。"""
    repo_dir = Path(repo_dir)
    if not (repo_dir / ".git").is_dir():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        run(clone_args(repo_url, repo_dir))
    run(["fetch", "--prune", "--quiet", "origin"], cwd=repo_dir)
    run(["checkout", "--quiet", "-B", "main", "origin/main"], cwd=repo_dir)
    run(["reset", "--hard", "--quiet", "origin/main"], cwd=repo_dir)
    run(["clean", "-fdq"], cwd=repo_dir)
    return run(["rev-parse", "origin/main"], cwd=repo_dir)


def show(repo_dir, ref, path):
    """git show ref:path。無ければ None。"""
    try:
        return run(["show", f"{ref}:{path}"], cwd=repo_dir)
    except GitError:
        return None


def ls_remote_branch(repo_dir, branch):
    out = run(["ls-remote", "--heads", "origin", branch], cwd=repo_dir, check=False)
    return bool(out.strip())


def sync_state_branch(state_dir, repo_url, branch):
    """ops-state ブランチの checkout を最新化する。ブランチは呼び出し側で
    ensure_branch (API) 済みであること。

    reset --hard の前に、前回の push が失敗して local が origin より進んでいれば
    まず push を試みる。commit 済み・push 前に落ちた状態を黙って巻き戻さない
    (レビュー指摘 [8] の salvage。単一書き手なので push が競合することはない)。"""
    state_dir = Path(state_dir)
    if not (state_dir / ".git").is_dir():
        state_dir.parent.mkdir(parents=True, exist_ok=True)
        run(clone_args(repo_url, state_dir, branch=branch, single_branch=True))
    run(["fetch", "--quiet", "origin", branch], cwd=state_dir)
    ahead = run(
        ["rev-list", "--count", f"origin/{branch}..HEAD"], cwd=state_dir, check=False
    )
    if ahead and ahead != "0":
        run(["push", "--quiet", "origin", f"HEAD:{branch}"], cwd=state_dir, check=False)
        run(["fetch", "--quiet", "origin", branch], cwd=state_dir)
    run(["checkout", "--quiet", "-B", branch, f"origin/{branch}"], cwd=state_dir)
    run(["reset", "--hard", "--quiet", f"origin/{branch}"], cwd=state_dir)


def commit_and_push_state(state_dir, branch, message):
    """変更があれば commit して push。単一書き手なので non-fast-forward は
    起きない前提だが、起きたら例外を投げて次のビートに任せる (上書きしない)。"""
    run(["add", "-A"], cwd=state_dir)
    if not run(["status", "--porcelain"], cwd=state_dir):
        return False
    run(["commit", "--quiet", "-m", message], cwd=state_dir)
    run(["push", "--quiet", "origin", f"HEAD:{branch}"], cwd=state_dir)
    return True
