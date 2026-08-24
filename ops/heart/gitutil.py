"""git 操作のヘルパ。

heart が持つ checkout は 1 つだけになった (設計 state-out-of-git 4b-2b):
  - repo_dir: main の**読み取り専用**ミラー (毎ビート hard reset。loop.sh と同じ規律)

**ここに書き込みの口は無い。** ops-state ブランチへ commit / push する
sync_state_branch / commit_and_push_state は 4b-2b で消した。プロジェクトの正は
Project CR に移り、heart の作業ファイルは PVC に居る。「機械は git に定期
コミットを打たない」(設計の原則 3) を、規律ではなく**関数が無いこと**で守る。
"""

import subprocess
from pathlib import Path

# clone は blobless (partial clone) で打つ。状態ブランチ 4 本の履歴で .git が 124MB あり、
# 素の clone はこの回線で 65s、ops-state 1 本でも 54s かかる (2026-08-24 実測)。採択ゲートの
# clone はこれで 120s 上限を越えて落ちていた。--filter=blob:none なら 2s / 9.2MB。
# **shallow (--depth=1) にはしない。** merge-base を要る経路 (採択ゲートの verify) がある。
# blob は使うときに 1 つずつ取りに行く = clone 後の操作にネットワークが要ることに注意。
BLOBLESS = "--filter=blob:none"


class GitError(Exception):
    pass


def clone_args(repo_url, dest):
    """clone のコマンド列を組み立てる (純関数。テストがネットワークに出ずに検査する)。

    --single-branch は付けない。remote.origin.fetch を 1 本に固定してしまい、
    以後 fetch しても他の ref が生えない (P-0014)。
    """
    return ["clone", "--quiet", BLOBLESS, repo_url, str(dest)]


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
