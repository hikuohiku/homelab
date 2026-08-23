"""事実収集。reconcile.decide() に渡す facts を組み立てる。

ここは「観測」だけを行い判断しない。観測に失敗した項目は facts に
「わからない」を正直に載せ (None / fresh=False)、decide 側が保守的に扱う。
"""

import json
from datetime import datetime, timezone

from . import gitutil, tasks, triage
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


def budget_alert(doc):
    """latest.json から B2 download cap の警報すべき状態を抽出する (P-0128)。

    report が作る download_budget.budget.status のうち warn / exceed のときだけ
    {status, reason, daily_avg_bytes, monthly_estimate_bytes} を返す。それ以外
    (ok/unconfigured/no_data、latest.json 無し・壊れ・download_budget キー無し)
    は None。unconfigured (cap 実値未設定) と no_data (産出側未稼働) を鳴らさない
    のは judge() 側と同じ判断 — 鳴らせる状態になったときにだけ既存経路に乗る。

    観測のみを行い判断しない (モジュール冒頭の原則)。鳴らすかどうかの繰り返し
    抑制は budget_alert_due() が担う。
    """
    if not isinstance(doc, dict):
        return None
    db = doc.get("download_budget")
    if not isinstance(db, dict):
        return None
    budget = db.get("budget")
    if not isinstance(budget, dict):
        return None
    status = budget.get("status")
    if status not in ("warn", "exceed"):
        return None

    def _num(value):
        # bool は int の派生なので明示的に弾く (download_budget.coerce_bytes と同じ)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value

    total = db.get("total") if isinstance(db.get("total"), dict) else {}
    return {
        "status": status,
        "reason": budget.get("reason"),
        "daily_avg_bytes": _num(total.get("daily_avg_bytes")),
        "monthly_estimate_bytes": _num(db.get("monthly_estimate_bytes")),
    }


def budget_alert_due(alert, prev, today):
    """警報を今日新規に積むべきか。(alert, cursors の前回記録, today=YYYY-MM-DD)。

    同じ status で同じ日内の再通知を落とす (heart は 120s ビートで回るため、
    抑制しないと briefing-queue.jsonl と Discord 予算を 1 日で使い潰す)。
    status が変わったら (warn→exceed への悪化など) その日は再度鳴らす。
    alert=None は常に False。
    """
    if alert is None:
        return False
    if not isinstance(prev, dict):
        return True
    return not (prev.get("status") == alert.get("status") and prev.get("date") == today)


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
    triage して (vetoes, stop_all, review_needed, resume_all, task_requests,
    new_cursors) を返す。

    初回起動 (cursor 未初期化) は **過去の全履歴を triage しない** (レビュー指摘 [7])。
    issue #56 には 100 件超の過去コメントがあり、旧 CHARTER の引用等に停止キーワードが
    含まれるため、履歴を分類すると存在しないプロジェクトへの偽 stop_all を拾う。
    初回は「現在までを既読」としてカーソルを置くだけにする。

    task_requests は構造化タスク依頼 (note のトップレベル kind == "task-request"、
    P-0090/P-0091) の未処理キュー分。triage の fall-through で briefing に落とす前に
    分流する (P-0091)。
    """
    vetoes = []
    acks = []
    stop_all = False
    resume_all = False
    review_needed = []
    task_requests = []

    def handle(body, source, kind=None):
        """1 件のフィードバックを分類して振り分ける。停止系は task-request より先
        (「止めて」が依頼本文に混ざっていても決定論パススルーは譲らない — P-0090
        の絶対条件)。task-request は review_needed に落ちる直前で分流する。"""
        nonlocal stop_all, resume_all
        verdict = triage.classify(body, rules)
        if verdict["kind"] == "veto":
            vetoes.extend(verdict["projects"])
        elif verdict["kind"] == "ack":
            acks.extend(verdict["projects"])
        elif verdict["kind"] == "stop_all":
            stop_all = True
        elif verdict["kind"] == "resume_all":
            resume_all = True
        elif kind == tasks.KIND_TASK_REQUEST:
            task_requests.append({"source": source, "body": body})
        else:
            review_needed.append({"source": source, "body": body})

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
        return [], [], False, [], False, [], new_cursors

    # issue コメントは自由文 (kind を持たない) なので通常経路。
    # JSON note のみトップレベル kind を読む
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
        handle(body, f"issue-comment {c.get('id')}")

    seen = set(cursors.get("seen_feedback_files", []))
    new_seen = set(seen)
    for path in _list_feedback_files(repo_dir, feedback_branch):
        if path in seen:
            continue
        new_seen.add(path)
        raw = gitutil.show(repo_dir, f"origin/{feedback_branch}", path)
        if raw is None:
            continue
        kind = None
        try:
            note = json.loads(raw)
            body = str(note.get("body", ""))
            k = note.get("kind")
            if isinstance(k, str):
                kind = k
        except ValueError:
            body = raw
        handle(body, path, kind)

    new_cursors = dict(cursors)
    new_cursors["issue_comments_since"] = newest
    new_cursors["seen_feedback_files"] = sorted(new_seen)
    return vetoes, acks, stop_all, review_needed, resume_all, task_requests, new_cursors


def progress_path(project_id):
    """プロジェクト文脈 (PROGRESS.md) のリポジトリ内パス。runner と同じ位置。"""
    return f"ops/projects/logs/{project_id}/PROGRESS.md"


def collect_continuation(repo_dir, doc, results):
    """P-0182: budget_exhausted 判定中の active プロジェクトについて、
    「プロジェクトブランチに続きがあるか」を観測する。

    返すのは {project_id: True / False / None}:
      True  = ブランチ上に PROGRESS.md がある (checkpoint を書いて止まった
              worker ループの予算死 = 継続の候補)
      False = ブランチまたは PROGRESS.md が無い (initializer 中の予算死など。
              継続するものが何も無い)
      None  = 観測に失敗した (「無い」と区別する。decide 側はこのビートでは
              判断しない)

    runner は checkpoint セッションの push **後**に result.json を書くため、
    ビート冒頭の sync_main の fetch がその push に追いつかないことがある。
    証拠取り逃しによる誤 stalled を防ぐため、判定対象ブランチだけ取り直して
    から読む。
    """
    out = {}
    for p in doc["projects"]:
        pid = p["id"]
        result = results.get(pid)
        if (
            p["state"] != "active"
            or not result
            or result.get("state") != "budget_exhausted"
        ):
            continue
        branch = p.get("branch", "")
        if not branch:
            out[pid] = False
            continue
        try:
            gitutil.run(
                ["fetch", "--quiet", "origin", branch], cwd=repo_dir, check=False
            )
            if not gitutil.ls_remote_branch(repo_dir, branch):
                out[pid] = False
                continue
            out[pid] = (
                gitutil.show(repo_dir, f"origin/{branch}", progress_path(pid))
                is not None
            )
        except Exception:
            out[pid] = None
    return out


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


def collect_critic(data_dir):
    """critic Job の結果 (/data/projects/critic/result.json) を観測する。無ければ None。

    curriculum (= "system") とは別ディレクトリにしてある (spawn.build_job の
    project_id)。同居させると critic のエラーが curriculum のエラーとして
    処理されるため。collect_results() もこのディレクトリを拾うが、id "critic" の
    プロジェクトは projects.json に存在しないので decide 側では無視される。
    """
    path = data_dir / "projects" / "critic" / "result.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            result = json.load(f)
    except (OSError, ValueError):
        # 書きかけ・壊れた result は「観測できなかった」でなく「異常終了」として
        # 扱う。放置すると消費されずに毎ビート読み直すファイルが残り続ける
        return {"state": "error", "error": "result.json が読めない"}
    return {"state": result.get("state"), "error": result.get("error")}


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
