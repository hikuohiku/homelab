"""事実収集。reconcile.decide() に渡す facts を組み立てる。

ここは「観測」だけを行い判断しない。観測に失敗した項目は facts に
「わからない」を正直に載せ (None / fresh=False)、decide 側が保守的に扱う。
"""

import json
from datetime import datetime, timezone

from . import gitutil, triage
from .statefiles import parse_iso


def load_health(repo_dir, health_branch):
    """ops-health-report ブランチの latest.json。(unhealthy_apps, fresh, raw)

    unhealthy_apps は Synced/Healthy でない Application 名のリスト。
    観測に失敗したら None (「全部 unhealthy」でも「全部 healthy」でもない)。
    既知の Degraded (T-0106 等) をここで隠さない — soak の合否は decide 側が
    「merge 時点の baseline から悪化したか」で判定する (レビュー指摘 [6])。
    """
    raw = gitutil.show(repo_dir, f"origin/{health_branch}", "ops/health/latest.json")
    if raw is None:
        return None, False, None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, False, None
    generated = doc.get("generated_at")
    fresh = False
    if generated:
        try:
            age = datetime.now(timezone.utc) - parse_iso(generated)
            fresh = age.total_seconds() < 3600
        except ValueError:
            pass
    unhealthy = [
        app.get("name") or "?"
        for app in doc.get("applications", [])
        if app.get("sync") != "Synced" or app.get("health") != "Healthy"
    ]
    return unhealthy, fresh, doc


def collect_jobs(k8s, namespace):
    """heart が生んだ Job の実状態。{job_name: {"active":bool,"failed":bool,"succeeded":bool}}"""
    out = {}
    for job in k8s.list_jobs(namespace, label_selector="app.kubernetes.io/managed-by=heart"):
        st = job.get("status", {})
        out[job["metadata"]["name"]] = {
            "active": bool(st.get("active")),
            "succeeded": bool(st.get("succeeded")),
            "failed": bool(st.get("failed")),
        }
    return out


def collect_results(data_dir):
    """runner が PVC に書く result.json / review.json を回収する。
    {project_id: result_dict} / {project_id: review_dict}"""
    results, reviews = {}, {}
    projects_dir = data_dir / "projects"
    if not projects_dir.is_dir():
        return results, reviews
    for pdir in projects_dir.iterdir():
        if not pdir.is_dir():
            continue
        for name, store in (("result.json", results), ("review.json", reviews)):
            path = pdir / name
            if path.exists():
                try:
                    with open(path) as f:
                        store[pdir.name] = json.load(f)
                except (OSError, ValueError):
                    continue
    return results, reviews


def collect_prs(gh, project_branches):
    """open PR と、プロジェクトブランチに紐づく merge 済み PR を観測する。
    open_prs: {number: {"head":..., "checks_green": bool}}
    merged_prs: {number: True}"""
    open_prs = {}
    for pr in gh.open_prs():
        head = pr.get("head", {}).get("ref", "")
        checks_green = None
        if head in project_branches:
            sha = pr.get("head", {}).get("sha")
            try:
                runs = gh.pr_combined_status(sha).get("check_runs", [])
                checks_green = bool(runs) and all(
                    r.get("conclusion") == "success"
                    for r in runs
                    if r.get("status") == "completed"
                ) and all(r.get("status") == "completed" for r in runs)
            except Exception:
                checks_green = None
        open_prs[pr["number"]] = {"head": head, "checks_green": checks_green}
    merged = {}
    for branch in project_branches:
        # ブランチ名で閉じた PR を引く (merged_at 非 null で判定。
        # 「merged フィールドは信用しない」— CHARTER §7.1 の実測)
        try:
            owner = gh.repo.split("/")[0]
            closed = gh.request(
                "GET",
                f"/repos/{gh.repo}/pulls?state=closed&head={owner}:{branch}&per_page=100",
            )
        except Exception:
            continue
        for pr in closed:
            if pr.get("merged_at"):
                merged[pr["number"]] = True
    return open_prs, merged


def _list_feedback_files(repo_dir, feedback_branch):
    try:
        listing = gitutil.run(
            ["ls-tree", "-r", "--name-only", f"origin/{feedback_branch}",
             "ops/feedback/inbox/"],
            cwd=repo_dir, check=False,
        )
        return [line for line in listing.splitlines() if line.strip()]
    except gitutil.GitError:
        return []


def collect_feedback(gh, repo_dir, cursors, rules, feedback_issue, feedback_branch):
    """issue #56 の新着コメント + ops-feedback ブランチの新着書き置きを
    triage して (vetoes, stop_all, review_needed, new_cursors) を返す。

    初回起動 (cursor 未初期化) は **過去の全履歴を triage しない** (レビュー指摘 [7])。
    issue #56 には 100 件超の過去コメントがあり、旧 CHARTER の引用等に停止キーワードが
    含まれるため、履歴を分類すると存在しないプロジェクトへの偽 stop_all を拾う。
    初回は「現在までを既読」としてカーソルを置くだけにする。
    """
    vetoes = []
    stop_all = False
    review_needed = []

    if not cursors.get("initialized"):
        new_cursors = dict(cursors)
        new_cursors["initialized"] = True
        try:
            recent = gh.issue_comments_since(feedback_issue, None)
            new_cursors["issue_comments_since"] = (
                max((c.get("created_at") or "" for c in recent), default="") or None
            )
        except Exception:
            new_cursors["issue_comments_since"] = None
        new_cursors["seen_feedback_files"] = sorted(
            _list_feedback_files(repo_dir, feedback_branch)
        )
        return [], False, [], new_cursors

    since = cursors.get("issue_comments_since")
    newest = since
    try:
        comments = gh.issue_comments_since(feedback_issue, since)
    except Exception:
        comments = []
    for c in comments:
        created = c.get("created_at")
        if since and created and created <= since:
            continue
        if newest is None or (created and created > newest):
            newest = created
        # 自分 (bot) の代送コメントを拾って veto 誤検知しないよう、
        # heart 由来のプレフィクスは除外する
        body = c.get("body", "")
        if body.startswith("(Discord 不達のため代送)"):
            continue
        verdict = triage.classify(body, rules)
        if verdict["kind"] == "veto":
            vetoes.extend(verdict["projects"])
        elif verdict["kind"] == "stop_all":
            stop_all = True
        else:
            review_needed.append({"source": f"issue-comment {c.get('id')}", "body": body})

    seen = set(cursors.get("seen_feedback_files", []))
    new_seen = set(seen)
    for path in _list_feedback_files(repo_dir, feedback_branch):
        if path in seen:
            continue
        new_seen.add(path)
        raw = gitutil.show(repo_dir, f"origin/{feedback_branch}", path)
        if raw is None:
            continue
        try:
            note = json.loads(raw)
            body = note.get("body", "")
        except ValueError:
            body = raw
        verdict = triage.classify(body, rules)
        if verdict["kind"] == "veto":
            vetoes.extend(verdict["projects"])
        elif verdict["kind"] == "stop_all":
            stop_all = True
        else:
            review_needed.append({"source": path, "body": body})

    new_cursors = dict(cursors)
    new_cursors["issue_comments_since"] = newest
    new_cursors["seen_feedback_files"] = sorted(new_seen)
    return vetoes, stop_all, review_needed, new_cursors


def load_adopted_specs(repo_dir):
    """main の archive.jsonl から採択済み spec を {id: spec} で返す (同 id は最後の行が有効)。"""
    specs = {}
    path = repo_dir / "ops" / "projects" / "archive.jsonl"
    if not path.exists():
        return specs
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("adopted") and rec.get("id"):
                specs[rec["id"]] = rec
    return specs


def collect_curriculum(data_dir, repo_dir, gh):
    """curriculum Job の結果 (/data/projects/system/result.json) と、その採択 PR の
    状態・採択 spec を観測する。無ければ None。"""
    path = data_dir / "projects" / "system" / "result.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            result = json.load(f)
    except (OSError, ValueError):
        return None
    out = {"state": result.get("state"), "pr": result.get("pr"),
           "error": result.get("error")}
    if result.get("state") != "curriculum_done":
        return out
    pr_num = result.get("pr")
    out["pr_merged"] = False
    out["pr_open"] = False
    out["checks_green"] = False
    if pr_num is not None:
        try:
            pr = gh.pr(pr_num)
            out["pr_merged"] = bool(pr.get("merged_at"))
            out["pr_open"] = pr.get("state") == "open"
            if out["pr_open"]:
                sha = pr.get("head", {}).get("sha")
                runs = gh.pr_combined_status(sha).get("check_runs", [])
                out["checks_green"] = bool(runs) and all(
                    r.get("status") == "completed"
                    and r.get("conclusion") == "success"
                    for r in runs
                )
        except Exception:
            # PR の状態が読めないビートでは merge/破棄の判断をしない
            out["pr_unknown"] = True
            return out
    if out["pr_merged"]:
        adopted_ids = result.get("adopted") or []
        specs = load_adopted_specs(repo_dir)
        out["adopted_specs"] = [specs[i] for i in adopted_ids if i in specs]
    return out
