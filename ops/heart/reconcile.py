"""プロジェクト状態機械。純関数のみ — I/O を書かない。

heart.py が事実 (facts) を集めてここに渡し、返った actions を実行する。
判断はすべてここに集約し、単体テストで遷移表を固定する (プラン検証 #1)。

状態: proposed → announced → active → in_review → merging → (soaking) → delivered
終端: delivered / stalled / vetoed
rework は独立した状態にせず、in_review で fail → active に戻して
review_cycles を数える (上限 rules.review.max_cycles で stalled)。

「実行経路で強制」の該当箇所:
  - merge は verdict=pass かつ CI green のときだけ action になる (LLM は納品判断に関与しない)
  - veto は他のどの遷移よりも先に評価される
  - breaker が落ちている間は新しい仕事 (announce / spawn) を一切作らない。
    走行中は殺さない (決定 #9)

観測の扱い (2026-08-07 レビューで確定した規約):
  - facts["jobs"] が None のときは「観測に失敗した」であり「Job が無い」ではない。
    job_missing 系の判定・respawn を一切しない (二重 spawn と誤 stalled の防止)
  - result.json / review.json は消費した遷移で consume_* action を出し、heart が
    ファイルを退避する。消費しないと次のビートで同じ事実を再消費して発振する
"""

from datetime import timedelta

# adoptgate は I/O も持つが、ここから呼ぶのは純関数 (classify/describe) だけ。
# 実測 (clone と verify 実行) は heart.execute() の run_adopt_gate が行う
from . import adoptgate
from .statefiles import TERMINAL_STATES, now_iso, parse_iso

# 各待ち状態の見張り時限。恒久的に黙って待つ状態を作らない (レビュー指摘 [4][11])
REVIEW_TIMEOUT_HOURS = 2
REVIEW_MAX_RETRIES = 2
MERGING_TIMEOUT_HOURS = 24
ADOPT_GATE_MAX_ATTEMPTS = 3  # 測定が書き戻されないまま回り続ける proposed を打ち切る
# 上限待ち (P-0026) も例外にしない。runner の 7200s は 1 プロセス内の上限にすぎず、
# waiting_quota → respawn → また waiting_quota の周回そのものには時限が無い。
# max_concurrent=1 では上限待ちの 1 件が他の全プロジェクトのスロットを塞ぐので、
# 連続で数えて打ち切る (FAILURE_PATTERNS の誤検知でここに落ちる可能性もある)
QUOTA_WAIT_MAX_ROUNDS = 6


def _action(kind, project_id=None, **kw):
    a = {"type": kind}
    if project_id is not None:
        a["project"] = project_id
    a.update(kw)
    return a


def _veto_deadline(project, facts, rules, now):
    """予告からの拒否権窓の期限。アイドルかつ非不可逆なら即着手 (窓 0)。
    不可逆ラベル付きは常に窓を待つ (決定 #3)。"""
    if not project.get("irreversible") and facts.get("running_runners", 0) == 0:
        return now_iso(now)
    hours = rules["veto"]["window_hours"]
    return now_iso(now + timedelta(hours=hours))


def _touches_apps(project):
    return bool(project.get("touches_apps"))


def _stall(p, actions, reason, ntype=None, text=None):
    p["state"] = "stalled"
    p["stalled_reason"] = reason
    if ntype:
        actions.append(_action("notify", p["id"], ntype=ntype, text=text))


def _register_spec(doc, spec, rules, now):
    """main の archive.jsonl で採択された spec を proposed として登録する。"""
    doc["projects"].append(
        {
            "id": spec["id"],
            "title": spec.get("title", ""),
            "state": "proposed",
            "branch": f"project/{spec['id'].lower()}",
            "irreversible": bool(spec.get("irreversible")),
            "capabilities": spec.get("capabilities", []),
            "touches_apps": bool(spec.get("touches_apps")),
            "verify": spec.get("verify", []),
            "confidence": spec.get("confidence", "unsure"),
            "budget": {
                "used_tokens": 0,
                "soft_cap": (spec.get("budget") or {}).get(
                    "soft_cap_tokens",
                    rules["runner"]["default_soft_cap_tokens"],
                ),
            },
            "created": now_iso(now)[:10],
        }
    )


def decide(doc, facts, rules, now):
    """(projects doc, facts) -> (新 doc, actions)。doc は破壊的に更新して返す。"""
    actions = []
    vetoes = set(facts.get("vetoes", []))
    stop_all = facts.get("stop_all", False)
    breaker = facts.get("breaker_tripped", False)
    jobs = facts.get("jobs")  # None = 観測失敗 (「無い」と区別する)
    results = facts.get("results", {})
    reviews = facts.get("reviews", {})
    prs = facts.get("open_prs", {})
    merged_prs = facts.get("merged_prs", {})
    unhealthy = facts.get("unhealthy_apps")  # None = 観測失敗

    running = sum(
        1 for p in doc["projects"] if p["state"] in ("active", "in_review", "merging")
    )

    # --- 採択の正は main の archive.jsonl。projects に無い採択 spec を登録する ---
    # curriculum の PR 経由でも人間の手動採択でも、「main に載れば動き出す」で意味論を
    # 統一する (2026-08-08 パイロット準備で発覚: curriculum result 経由の登録しか無く、
    # 手動採択が永遠に放置される欠落があった)。終端 (delivered/stalled/vetoed) の
    # エントリも projects に残るため、済んだ spec がここで蘇ることはない。
    # projects.json の終端エントリを将来間引くときは、登録済み id の記録を別に持つこと
    existing_ids = {p["id"] for p in doc["projects"]}
    for spec in facts.get("adopted_specs") or []:
        if spec.get("id") and spec["id"] not in existing_ids:
            _register_spec(doc, spec, rules, now)
            existing_ids.add(spec["id"])

    for p in doc["projects"]:
        state = p["state"]
        if state in TERMINAL_STATES:
            continue
        pid = p["id"]

        # --- 人間の停止意思は他のどの遷移よりも先 ---
        if stop_all or pid in vetoes:
            if p.get("job"):
                actions.append(_action("kill_job", pid, job=p["job"]))
            p["state"] = "stalled" if stop_all else "vetoed"
            if stop_all:
                p["stalled_reason"] = "human_stop"
            actions.append(
                _action(
                    "notify",
                    pid,
                    ntype="notify",
                    text=f"{pid} を{'停止' if stop_all else 'veto により中止'}しました",
                )
            )
            continue

        if state == "proposed":
            # breaker 中は新しい仕事を作らない (走行中は別条項で守る)
            if breaker:
                continue
            # --- 採択ゲート: 予告の前に、新品 clone で verify を実測する (P-0015) ---
            # 壊れた spec (開始前に pass する / コマンドが壊れている) を予告の前に殺す。
            # ここで殺せば announce も veto 窓も Job も一切消費しない。
            # 測定は I/O なので heart.execute() が run_adopt_gate action で行い、
            # 生レコードを p["adopt_gate"]["verify"] に書き戻す。判定 (classify) は
            # 純関数なのでここで導く — 信念でなく実測レコードから毎回導き直す
            gate = p.get("adopt_gate")
            if not gate:
                # 測るまで進めない。このビートは proposed のまま次を待つ
                # (ゲートは spec 1 件につき 1 回。毎ビート clone しない)。
                # ただし**この待ちにも見張り時限を置く** (冒頭の不変条件)。
                # clone 失敗・/tmp の枯渇・git の timeout が続くと adopt_gate が
                # 永久に書き戻されず、proposed は非終端なので non_terminal が空に
                # ならず curriculum_idle も False に固定される = ビートは回っている
                # のに仕事が一切進まない沈黙状態になる。試行を数えて人間に渡す
                attempts = p.get("adopt_gate_attempts", 0)
                if attempts >= ADOPT_GATE_MAX_ATTEMPTS:
                    # 測れないのは spec の不良ではなく仕組みの故障。incident で渡す
                    _stall(
                        p, actions, "adopt_gate_unmeasurable", "incident",
                        f"{pid} の採択ゲートが {ADOPT_GATE_MAX_ATTEMPTS} 回続けて"
                        "測定できませんでした (新品 clone か verify 実行が失敗している)。"
                        "heart の audit.jsonl に例外が残っています",
                    )
                    continue
                p["adopt_gate_attempts"] = attempts + 1
                actions.append(_action("run_adopt_gate", pid))
                continue
            verdict = adoptgate.classify(gate.get("verify", []))
            if verdict["verdict"] != adoptgate.ALL_FAIL:
                # incident ではなく「採択の不良」。spec の直しを促す question で渡す。
                # 理由の実体は p["adopt_gate"] に残る = projects.json に残る
                _stall(
                    p, actions, "adopt_gate_" + verdict["verdict"], "question",
                    f"{pid} を予告せず差し戻しました "
                    f"(新品 clone での verify 実測 = {verdict['verdict']})。"
                    f"{adoptgate.describe(verdict)}。"
                    "spec を直して新しい id で採択し直してください",
                )
                continue
            p["state"] = "announced"
            p["veto_deadline"] = _veto_deadline(p, facts, rules, now)
            actions.append(_action("announce", pid))

        elif state == "announced":
            if parse_iso(p["veto_deadline"]) > now:
                # 窓の繰り上げ: 予告時に「他が走行中」だったために窓が付いた可逆案は、
                # アイドルになった時点で即着手してよい (窓の基準は安全性でなく稼働率 —
                # 決定 #3「何もしてない時間が嫌」)。不可逆案は繰り上げない
                if (
                    p.get("irreversible")
                    or facts.get("running_runners", 0) > 0
                    or running > 0
                ):
                    continue
                p["veto_deadline"] = now_iso(now)
            if breaker:
                continue
            if running >= rules["runner"]["max_concurrent"]:
                continue
            p["state"] = "active"
            running += 1
            actions.append(_action("spawn_runner", pid))

        elif state == "active":
            # --- 上限待ち (P-0026) は停滞ではないので stalled にしない ---
            # runner が waiting_quota で rc=0 終了した後の待ち。Job は succeeded で
            # 残る (active でも failed でもない) ため、下の job 梯子に入れると
            # 「消えた Job」扱いで即 respawn したり drift を数えたりしてしまう。
            # 時刻が来るまでここで止める
            wait_until = p.get("quota_wait_until")
            if wait_until:
                if parse_iso(wait_until) > now:
                    continue
                if breaker:
                    # breaker 中は新しい仕事を作らない。待ち札は持ったまま、
                    # 復帰したビートで再開する
                    continue
                # max_concurrent は見ない: このプロジェクトは active のまま
                # スロットを占めており、再開しても同時実行数は増えない
                p.pop("quota_wait_until", None)
                actions.append(_action("spawn_runner", pid, respawn=True))
                continue

            result = results.get(pid)
            job = jobs.get(p.get("job", ""), None) if jobs is not None else None
            if result and result.get("state") != "waiting_quota":
                # 上限以外の結果が返ってきた = 上限は明けてセッションが動いた。
                # 連続待ちの数え直し (数えるのは「連続」でなければ意味がない)
                p.pop("quota_wait_count", None)
            if result and result.get("state") == "ready_for_review":
                if result.get("pr") is not None:
                    prs_list = p.setdefault("prs", [])
                    if not prs_list or prs_list[-1] != result["pr"]:
                        prs_list.append(result["pr"])
                if not p.get("prs"):
                    # PR 番号が無いままレビューに進むと merging で必ず詰む。
                    # runner 側の契約違反としてここで止め、人間に見せる
                    actions.append(_action("consume_result", pid))
                    _stall(
                        p, actions, "no_pr_reported", "incident",
                        f"{pid}: runner が ready_for_review を報告したが PR 番号が無い",
                    )
                    continue
                if job and job.get("active"):
                    actions.append(_action("kill_job", pid, job=p["job"]))
                p["state"] = "in_review"
                p["review_requested_at"] = now_iso(now)
                p["review_retries"] = 0
                actions.append(_action("consume_result", pid))
                actions.append(_action("spawn_reviewer", pid))
            elif result and result.get("state") == "budget_exhausted":
                actions.append(_action("consume_result", pid))
                _stall(
                    p, actions, "budget_exhausted", "question",
                    f"{pid} が予算 (soft cap) を使い切りました。"
                    "継続する価値があれば予算を積んで再開を指示してください",
                )
            elif result and result.get("state") == "waiting_quota":
                # アカウントの利用上限は器の外側の事実であって、プロジェクトの停滞
                # ではない。通知も出さない (障害ではない)。projects.json の state は
                # active のまま、resume_after まで待って runner を出し直す。
                # **ただし無限には待たない** — 冒頭の不変条件「恒久的に黙って待つ状態を
                # 作らない」はこの待ちにも掛かる
                actions.append(_action("consume_result", pid))
                p["quota_wait_count"] = p.get("quota_wait_count", 0) + 1
                if p["quota_wait_count"] > QUOTA_WAIT_MAX_ROUNDS:
                    # 上限が明けないまま周回し続けている。器の側では直せない
                    # (待つ以外に手が無い) ので、budget_exhausted と同じ流儀で
                    # 人間に判断を渡す。スロットもここで解放される。
                    # 札と回数は落とす — 残すと人間が active に戻した次の
                    # waiting_quota で即また stalled になる (再開できない停止)
                    rounds = p.pop("quota_wait_count")
                    p.pop("quota_wait_until", None)
                    _stall(
                        p, actions, "quota_wait_exhausted", "question",
                        f"{pid} がアカウントの利用上限で {rounds} 回続けて"
                        "待機に入りました。上限が明けていないか、死因の判定が"
                        "誤っています。再開の判断をください",
                    )
                    continue
                p["quota_wait_until"] = result.get("resume_after") or now_iso(now)
            elif result and result.get("state") in (
                "spec_error", "error", "stalled_inactive"
            ):
                actions.append(_action("consume_result", pid))
                _stall(
                    p, actions, result["state"], "incident",
                    f"{pid} の runner が {result['state']} で終了: "
                    f"{str(result.get('error', ''))[:200]}",
                )
            elif jobs is None:
                # 観測失敗。Job の生死が分からないビートでは何もしない
                continue
            elif not p.get("job"):
                # spawn が失敗した (execute が job 名を記録できなかった)。
                # spawn は 409 冪等なので再発行してよい (レビュー指摘 [4])
                actions.append(_action("spawn_runner", pid, respawn=True))
            elif job is None:
                # 信念と実測の乖離: 走っているはずの Job が居ない
                p["drift_count"] = p.get("drift_count", 0) + 1
                actions.append(_action("record_drift", pid, reason="job_missing"))
                if p["drift_count"] > 2:
                    _stall(
                        p, actions, "job_missing", "incident",
                        f"{pid} の runner Job が繰り返し消失。stalled にしました",
                    )
                else:
                    actions.append(_action("spawn_runner", pid, respawn=True))
            elif job.get("active"):
                p["drift_count"] = 0
            elif job.get("failed"):
                p["restart_count"] = p.get("restart_count", 0) + 1
                if p["restart_count"] > 3:
                    _stall(
                        p, actions, "runner_crash_loop", "incident",
                        f"{pid} の runner が {p['restart_count']} 回連続で異常終了",
                    )
                else:
                    actions.append(_action("spawn_runner", pid, respawn=True))

        elif state == "in_review":
            review = reviews.get(pid)
            if review:
                actions.append(_action("consume_review", pid))
                if review.get("verdict") == "pass":
                    p["state"] = "merging"
                    p["merging_since"] = now_iso(now)
                else:
                    p["review_cycles"] = p.get("review_cycles", 0) + 1
                    if p["review_cycles"] >= rules["review"]["max_cycles"]:
                        _stall(
                            p, actions, "review_rejected", "review",
                            f"{pid} がレビューを {p['review_cycles']} 回通らず停滞。"
                            "指摘一覧を確認してください",
                        )
                    else:
                        p["state"] = "active"
                        actions.append(
                            _action(
                                "spawn_runner", pid,
                                findings=review.get("findings", []),
                            )
                        )
            else:
                # reviewer が黙って死んだ/spawn に失敗したケースの見張り
                requested = p.get("review_requested_at")
                if requested and (
                    now - parse_iso(requested)
                ) > timedelta(hours=REVIEW_TIMEOUT_HOURS):
                    if p.get("review_retries", 0) < REVIEW_MAX_RETRIES:
                        p["review_retries"] = p.get("review_retries", 0) + 1
                        p["review_requested_at"] = now_iso(now)
                        actions.append(
                            _action("spawn_reviewer", pid, retry=p["review_retries"])
                        )
                    else:
                        _stall(
                            p, actions, "review_timeout", "incident",
                            f"{pid} のレビューが {REVIEW_TIMEOUT_HOURS}h × "
                            f"{REVIEW_MAX_RETRIES + 1} 回応答しません",
                        )

        elif state == "merging":
            pr_num = (p.get("prs") or [None])[-1]
            if pr_num is None:
                _stall(
                    p, actions, "no_pr_to_merge", "incident",
                    f"{pid} が merging に入ったが PR 番号の記録が無い",
                )
                continue
            if pr_num in merged_prs:
                if _touches_apps(p):
                    p["state"] = "soaking"
                    p["soak"] = {
                        "until": now_iso(
                            now + timedelta(minutes=rules["soak"]["minutes"])
                        ),
                        # soak の合否は絶対値でなく「merge 時点から悪化したか」。
                        # 既知の Degraded (T-0106 等) を green と嘘をつかずに扱う
                        "baseline_unhealthy": sorted(unhealthy or []),
                    }
                else:
                    p["state"] = "delivered"
                    actions.append(_action("deliver", pid))
            elif pr_num in prs:
                if prs[pr_num].get("checks_green"):
                    actions.append(_action("merge_pr", pid, pr=pr_num))
                elif (
                    now - parse_iso(p.get("merging_since", now_iso(now)))
                ) > timedelta(hours=MERGING_TIMEOUT_HOURS):
                    _stall(
                        p, actions, "merge_timeout", "question",
                        f"{pid} の PR #{pr_num} が {MERGING_TIMEOUT_HOURS}h 経っても "
                        "merge できません (CI 未 green か保護パス)。判断をください",
                    )
                # checks が green でない間は待つ (CI が唯一のゲート)
            else:
                # open にも merged にも居ない = merge されずに close された
                _stall(
                    p, actions, "pr_closed", "question",
                    f"{pid} の PR #{pr_num} が merge されずに close されています",
                )

        elif state == "soaking":
            if parse_iso(p["soak"]["until"]) > now:
                continue
            if unhealthy is None or not facts.get("health_fresh"):
                # 観測できないまま soak を判定しない。次のビートまで待つ
                continue
            baseline = set(p["soak"].get("baseline_unhealthy", []))
            worse = [a for a in unhealthy if a not in baseline]
            if not worse:
                p["state"] = "delivered"
                actions.append(_action("deliver", pid))
            else:
                _stall(
                    p, actions, "soak_failed", "incident",
                    f"{pid} の merge 後に {', '.join(worse)} が unhealthy になりました。"
                    "ロールバックの判断が要ります",
                )

    # --- curriculum: 立案結果の取り込みと次の立案 ---
    cur = facts.get("curriculum")  # {"state","pr","adopted_specs","pr_merged","pr_open","checks_green","pr_unknown"}
    curriculum_pending = False
    if cur and cur.get("state") == "curriculum_done":
        curriculum_pending = True
        if cur.get("pr_unknown"):
            pass  # PR の状態が観測できないビートでは判断しない
        elif cur.get("pr_merged"):
            # 登録自体は上の「archive.jsonl reconcile」が担う (merge されれば main の
            # archive に載っているので、次のビートまでに必ず登録される)。ここでは
            # 消費の後始末だけを行う
            existing = {p["id"] for p in doc["projects"]}
            for spec in cur.get("adopted_specs", []):
                if spec.get("id") and spec["id"] not in existing:
                    _register_spec(doc, spec, rules, now)
                    existing.add(spec["id"])
            actions.append(_action("consume_curriculum"))
            curriculum_pending = False
        elif cur.get("pr_open") and cur.get("checks_green"):
            actions.append(_action("merge_pr", "system", pr=cur["pr"]))
        elif cur.get("pr") is None or (
            not cur.get("pr_open") and not cur.get("pr_merged")
        ):
            # PR が無い、または merge されずに close された (人間の拒否)。結果を破棄する
            actions.append(_action("consume_curriculum"))
            curriculum_pending = False
    elif cur and cur.get("state") == "error":
        actions.append(_action("consume_curriculum"))
        actions.append(
            _action(
                "notify", "system", ntype="incident",
                text=f"curriculum Job がエラー終了: {str(cur.get('error', ''))[:200]}",
            )
        )
        curriculum_pending = False

    non_terminal = [p for p in doc["projects"] if p["state"] not in TERMINAL_STATES]
    curriculum_idle = not non_terminal and not curriculum_pending
    last_curriculum = doc.get("last_curriculum_at")
    min_gap = timedelta(hours=rules["curriculum"].get("min_interval_hours", 6))
    gap_ok = last_curriculum is None or (now - parse_iso(last_curriculum)) >= min_gap
    if curriculum_idle and gap_ok and not breaker and not stop_all:
        doc["last_curriculum_at"] = now_iso(now)
        actions.append(_action("spawn_curriculum"))

    return doc, actions
