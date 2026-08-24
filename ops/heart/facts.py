"""事実収集。reconcile.decide() に渡す facts を組み立てる。

ここは「観測」だけを行い判断しない。観測に失敗した項目は facts に
「わからない」を正直に載せ (None / fresh=False)、decide 側が保守的に扱う。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from . import dispatch, gitutil, tasks, triage
from .statefiles import parse_iso


# 健全性レポートの正の置き場 (設計 state-out-of-git Phase 5)。ops-health-reporter が
# 書き、heart と常駐コアがクラスタ内で読む。ブランチ経路は Phase 7 で消える
HEALTH_CONFIGMAP_NAMESPACE = "ops-health-reporter"
HEALTH_CONFIGMAP = "ops-health-report"
HEALTH_CONFIGMAP_KEY = "latest.json"
HEALTH_PATH = "ops/health/latest.json"
HEALTH_FRESH_SECONDS = 3600

# 「観測できなかった」を表す。unhealthy_apps=None は「全部 unhealthy」でも
# 「全部 healthy」でもない (decide 側が保守的に扱う)
HEALTH_UNKNOWN = (None, False, None)


def parse_health(raw, now=None):
    """レポート JSON 1 本を (unhealthy_apps, fresh, doc) にする (純関数)。

    unhealthy_apps は Synced/Healthy でない Application 名のリスト。
    読めなければ HEALTH_UNKNOWN。既知の Degraded (T-0106 等) をここで隠さない —
    soak の合否は decide 側が「merge 時点の baseline から悪化したか」で判定する
    (レビュー指摘 [6])。
    """
    if not raw:
        return HEALTH_UNKNOWN
    try:
        doc = json.loads(raw)
    except ValueError:
        return HEALTH_UNKNOWN
    if not isinstance(doc, dict):
        return HEALTH_UNKNOWN
    now = now or datetime.now(timezone.utc)
    generated = doc.get("generated_at")
    fresh = False
    if generated:
        try:
            age = now - parse_iso(generated)
            fresh = age.total_seconds() < HEALTH_FRESH_SECONDS
        except ValueError:
            pass
    unhealthy = [
        app.get("name") or "?"
        for app in doc.get("applications", [])
        if app.get("sync") != "Synced" or app.get("health") != "Healthy"
    ]
    return unhealthy, fresh, doc


def pick_health(primary, fallback):
    """2 経路の観測からどちらを採るか (純関数)。

    fresh な方が勝つ。両方 fresh でなければ doc を持っている方 (primary 優先) を
    fresh=False のまま返し、どちらも読めなければ HEALTH_UNKNOWN。
    **「読めなかった」を「健全」に倒さない** — 空の unhealthy_apps を捏造しない。
    """
    if primary[1]:
        return primary
    if fallback[1]:
        return fallback
    if primary[2] is not None:
        return primary
    if fallback[2] is not None:
        return fallback
    return HEALTH_UNKNOWN


def read_health_configmap(
    k8s,
    namespace=HEALTH_CONFIGMAP_NAMESPACE,
    name=HEALTH_CONFIGMAP,
    key=HEALTH_CONFIGMAP_KEY,
):
    """ConfigMap からレポートの生 JSON を取る。読めなければ None (例外は投げない)。"""
    if k8s is None:
        return None
    try:
        cm = k8s.get_configmap(namespace, name)
    except Exception:  # noqa: BLE001 — 到達不能も 403 も「読めない」に畳む
        return None
    data = cm.get("data") if isinstance(cm, dict) else None
    raw = data.get(key) if isinstance(data, dict) else None
    return raw if isinstance(raw, str) else None


def load_health(repo_dir, health_branch, k8s=None, now=None):
    """健全性レポート。(unhealthy_apps, fresh, raw)

    正はクラスタ内の ConfigMap (設計 state-out-of-git Phase 5)。読めない、あるいは
    古いときだけ ops-health-report ブランチに落ちる。ブランチ経路は Phase 7 で消える。
    """
    primary = parse_health(read_health_configmap(k8s), now)
    if primary[1]:
        return primary
    branch_raw = gitutil.show(repo_dir, f"origin/{health_branch}", HEALTH_PATH)
    return pick_health(primary, parse_health(branch_raw, now))


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


def dashboard_smoke_alert(doc):
    """latest.json から Mission Control 描画スモークの警報すべき状態を抽出する (P-0193)。

    report が作る dashboard_smoke.status のうち fail / stale のときだけ
    {status, reason, failed_checks} を返す。それ以外 (ok/no_data、latest.json 無し・
    壊れ・dashboard_smoke キー無し) は None。no_data (産出側未稼働・記録破損) を
    鳴らさないのは budget_alert() が unconfigured/no_data を沈黙させるのと同じ判断 —
    鳴らせる状態になったときにだけ既存経路に乗る。

    fail のうち tool_error を伴うものはスモーク本体自体の故障だが、人間に見せる
    べきことには変わりが無いので区別せず乗せる (reason に区別が載っている)。

    観測のみを行い判断しない (モジュール冒頭の原則)。鳴らすかどうかの繰り返し
    抑制は budget_alert_due() が担う (status/date の一般判定なので流用する)。
    """
    if not isinstance(doc, dict):
        return None
    ds = doc.get("dashboard_smoke")
    if not isinstance(ds, dict):
        return None
    status = ds.get("status")
    if status not in ("fail", "stale"):
        return None
    checks = ds.get("failed_checks")
    failed = [
        str(c["name"])
        for c in checks
        if isinstance(c, dict) and c.get("name")
    ] if isinstance(checks, list) else []
    reason = ds.get("reason")
    return {
        "status": status,
        # reporter が reason を必ず文字列で書く契約だが、壊れていたら
        # str() で捏造せず None (文面だけの欠落で警報は倒さない)
        "reason": reason if isinstance(reason, str) and reason else None,
        "failed_checks": failed,
    }


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


# 書き置きの既読を持つ鍵の形。GitHub 経路 (ops-feedback ブランチの ls-tree) が
# この接頭辞つきのパスを返すので、バス経路のファイル名もこの形に寄せる。
# 揃えることで cursors["seen_feedback_files"] 1 つで 2 経路の重複が落ちる
# (autopilot-core の seenKey と同じ判断)。
FEEDBACK_KEY_PREFIX = "ops/feedback/inbox/"


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


def _list_bus_notes(bus_dir):
    """バスのサイドカーが置いた書き置きを [(key, Path)] で返す。

    key は GitHub 経路と同じ形 ("ops/feedback/inbox/<id>.json") にする。同じ書き置きは
    両経路で同じ id を持つので、鍵を揃えれば既存の cursor がそのまま重複を落とす。

    サイドカー (apps/autopilot/bus-sidecar) が rename で置くまでファイルは現れないが、
    書きかけの一時ファイルを万一拾わないよう "." 始まりは読まない。
    ディレクトリが無い / 読めないときは「バス経路が無い」として空を返す —
    バスの不調で GitHub 経路まで止めない (停止経路を運ぶ側が可用性を下げない)。
    """
    if bus_dir is None:
        return []
    try:
        names = sorted(
            p.name for p in Path(bus_dir).iterdir()
            if p.is_file() and p.name.endswith(".json") and not p.name.startswith(".")
        )
    except OSError:
        return []
    return [(FEEDBACK_KEY_PREFIX + name, Path(bus_dir) / name) for name in names]


def collect_feedback(gh, repo_dir, cursors, rules, feedback_issue, feedback_branch,
                     bus_dir=None):
    """issue #56 の新着コメント + ops-feedback ブランチ + バス経路の新着書き置きを
    triage して (vetoes, acks, stop_all, review_needed, resume_all, task_requests,
    approves, new_cursors) を返す。

    bus_dir はイベントバス (NATS) のサイドカーが書き置きを落とすローカルディレクトリ
    (移行の段階 3)。GitHub 経路を残したまま足すのは、所有者の「止めて」が GitHub の
    可用性に依存する状態を先に解くため — 片方が死んでももう片方で届く。
    どちらから来ても鍵は同じ ("ops/feedback/inbox/<id>.json") なので、同じ書き置きが
    2 回処理されることはない。

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
    approves = []
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
        elif verdict["kind"] == "approve":
            approves.extend(verdict["projects"])
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
        # バス経路のぶんも「現在までを既読」に含める。ここを落とすと初回起動で
        # 手元に残っている過去の書き置きを一斉に triage してしまう
        new_cursors["seen_feedback_files"] = sorted(
            set(_list_feedback_files(repo_dir, feedback_branch))
            | {key for key, _ in _list_bus_notes(bus_dir)}
        )
        return [], [], False, [], False, [], [], new_cursors

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

    def handle_note(raw, key):
        """書き置き 1 件の生テキストを分類に回す。JSON note ならトップレベル kind を
        読み、そうでなければ本文そのものとして扱う (経路によらず同じ扱い)。"""
        kind = None
        try:
            note = json.loads(raw)
            body = str(note.get("body", ""))
            k = note.get("kind")
            if isinstance(k, str):
                kind = k
        except ValueError:
            body = raw
        handle(body, key, kind)

    seen = set(cursors.get("seen_feedback_files", []))
    new_seen = set(seen)
    for path in _list_feedback_files(repo_dir, feedback_branch):
        if path in seen:
            continue
        new_seen.add(path)
        raw = gitutil.show(repo_dir, f"origin/{feedback_branch}", path)
        if raw is None:
            continue
        handle_note(raw, path)

    # バス経路 (NATS → サイドカー → ローカルファイル)。GitHub 経路と同じ鍵を使うので、
    # 同じ書き置きが両方から来ても 2 回処理されない。判定に new_seen を使うのは、
    # 同一ビート内で先に GitHub 側が拾ったぶんも落とすため
    for key, path in _list_bus_notes(bus_dir):
        if key in new_seen:
            continue
        try:
            raw = path.read_text()
        except OSError:
            # 読めないものは既読にしない (次のビートで拾い直す)
            continue
        new_seen.add(key)
        handle_note(raw, key)

    new_cursors = dict(cursors)
    new_cursors["issue_comments_since"] = newest
    new_cursors["seen_feedback_files"] = sorted(new_seen)
    return vetoes, acks, stop_all, review_needed, resume_all, task_requests, approves, new_cursors


def collect_commands(command_dir):
    """常駐コア発の command (設計 D3/D7/D21) を名前順に返す。

    経路: コア (MCP の request_task) → NATS events.heart.> → 同居サイドカー
    (apps/autopilot/bus-sidecar) → <command_dir>/<command_id>.json → ここ。

    **書き置き (feedback) とは別ディレクトリ**にしてある。混ぜると triage が
    「人間の発話」として分類し、依頼が briefing に埋もれる。ここは triage を
    通さず、reconcile.decide() が種別で分岐する。

    重複排除はここではやらない。台帳 (tasks.COMMAND_LEDGER_FILE) を facts として
    decide に渡し、判断は純関数側に置く。

    ディレクトリが無い / 読めないときは空を返す — バスの不調で heart を止めない。
    壊れたファイル・必須項目 (command_id / type / body) を欠くものは黙って飛ばす。
    サイドカーが入口で捨てているはずのものが届いたということなので、ここで
    例外にしてビートを落とす方が害が大きい。
    """
    if command_dir is None:
        return []
    try:
        paths = sorted(
            p for p in Path(command_dir).iterdir()
            if p.is_file() and p.name.endswith(".json") and not p.name.startswith(".")
        )
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            command = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(command, dict):
            continue
        cid = str(command.get("command_id") or "").strip()
        ctype = str(command.get("type") or "").strip()
        body = str(command.get("body") or "").strip()
        if not cid or not ctype or not body:
            continue
        out.append({
            "command_id": cid,
            "type": ctype,
            "source": str(command.get("source") or "core"),
            "issued_at": str(command.get("issued_at") or ""),
            "title": str(command.get("title") or "").strip(),
            "body": body,
        })
    return out


def collect_dispatches(data_dir):
    """即時 dispatch (設計 rev3 Phase D) の結末を名前順に返す。

    書き手は同じプロセスの gate スレッド (ops/heart/gate.py)。gate は採択ゲートの
    実測と Job 作成まで済ませてから <data_dir>/dispatch/inbox/<id>.json を置く。
    ここで読んだものを reconcile.decide() が projects.json に折り込み、
    execute() が consume_dispatch でファイルを退避する。

    collect_commands と同じ規約: 読めない・壊れているものは黙って飛ばす
    (ビートを落とす方が害が大きい)。
    """
    if data_dir is None:
        return []
    inbox = Path(data_dir) / dispatch.DISPATCH_DIR / dispatch.INBOX
    try:
        paths = sorted(
            p for p in inbox.iterdir()
            if p.is_file() and p.name.endswith(".json") and not p.name.startswith(".")
        )
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if not record.get("dispatch_id") or not record.get("project_id"):
            continue
        out.append(record)
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
    # 採択 spec は result.json に載っている (設計 rev3 D32)。**PR の状態を見る前**に
    # 拾うのが要点で、着手はもう main への PR / CI / merge を待たない。
    # PR の状態が読めないビート (pr_unknown) でも登録だけは進む
    out["adopted_specs"] = [
        s for s in (result.get("adopted_specs") or []) if isinstance(s, dict) and s.get("id")
    ]
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
    if out["pr_merged"] and not out["adopted_specs"]:
        # 後方互換: adopted_specs を持たない古い result.json (D32 より前の
        # curriculum Job が書いたもの) は、従来どおり merge 後の台帳から読む
        adopted_ids = result.get("adopted") or []
        specs = load_adopted_specs(repo_dir)
        out["adopted_specs"] = [specs[i] for i in adopted_ids if i in specs]
    return out
