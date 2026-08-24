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
  - 消費量 (金額・トークン) を理由に仕事を止めることはしない。2026-08-24 に
    サーキットブレーカーと soft cap を廃止した。歯止めは max_concurrent /
    max_sessions_per_project / 無活動 kill / 連続エラー / review_cycles が担う

観測の扱い (2026-08-07 レビューで確定した規約):
  - facts["jobs"] が None のときは「観測に失敗した」であり「Job が無い」ではない。
    job_missing 系の判定・respawn を一切しない (二重 spawn と誤 stalled の防止)
  - result.json / review.json は消費した遷移で consume_* action を出し、heart が
    ファイルを退避する。消費しないと次のビートで同じ事実を再消費して発振する
"""

from datetime import timedelta

# adoptgate は I/O も持つが、ここから呼ぶのは純関数 (classify/describe) だけ。
# 実測 (clone と verify 実行) は heart.execute() の run_adopt_gate が行う
from . import adoptgate, dispatch, tasks
from .statefiles import TERMINAL_STATES, now_iso, parse_iso

# 各待ち状態の見張り時限。恒久的に黙って待つ状態を作らない (レビュー指摘 [4][11])
REVIEW_TIMEOUT_HOURS = 2
REVIEW_MAX_RETRIES = 2
MERGING_TIMEOUT_HOURS = 24
# merge の失敗のうち、再試行では絶対に直らないもの = コンフリクト。
# 24h の見張り時限だけでは毎分 API を叩き続ける (2026-08-23 に P-0216 で実測)
MERGE_CONFLICT_STATUS = 405
MERGE_CONFLICT_MARKER = "merge conflict"
ADOPT_GATE_MAX_ATTEMPTS = 3  # 測定が書き戻されないまま回り続ける proposed を打ち切る
# 上限待ち (P-0026) も例外にしない。runner の待機予算は 1 プロセス内の上限にすぎず、
# waiting_quota → respawn → また waiting_quota の周回そのものには時限が無い。
# 2026-08-24: 予算方式の廃止で「プロバイダ側のレート上限で待つ」は異常ではなく
# 通常の運転状態になった。ラウンド数はビート周期に依存して意味を持たない
# (毎分のビートなら 6 回は数分) ので、**連続して待ち続けた実時間**で打ち切る。
QUOTA_WAIT_MAX_HOURS = 24
# 自己観測 (critic) の間隔。指標 (状態別滞留・アイドル率) は日次の粒度で足り、
# それより短くしても同じ 24h の窓を読み直すだけになる (P-0045)
CRITIC_INTERVAL_HOURS = 24
# 1 回の curriculum PR に載せる台帳の遅延追記 (backfill) の上限。env 経由で
# 渡すので上限を置く。溢れた分は次の curriculum が拾う
ARCHIVE_BACKFILL_LIMIT = 20

# 「まだ一度も記録していない」と「None を記録した」を区別する番人
_UNSET = object()

# --- 即時 dispatch の admission gate (設計 rev3 Phase D) ---
#
# コアが `dispatch_task` で「いま着手してほしい」と同期で要求する経路の判定。
# **強制は heart に残す**という設計判断 1 の実体がここで、判定は既にある不変条件
# (stop_engaged / max_concurrent / capability の宣言連鎖) をそのまま使う。
# 閾値を rules.json でなくモジュール定数に置くのは adoptgate と同じ流儀
# (rules.json は人間レビュー必須パスで、触ると auto-merge が止まる)。
DISPATCH_RATE_LIMIT = 10  # この件数を
DISPATCH_RATE_WINDOW_MINUTES = 60  # この窓で。コアの連打が Job を作り続けない歯止め
# heart のビート結果 (走行数・stop_engaged) がこれより古ければ受け付けない。
# 古い写しで判断すると、直前に来た「止めて」を見落として着手しうる。
# 既定ビート 60s に対して十分な余裕を取りつつ、詰まった heart では必ず閉じる
DISPATCH_SNAPSHOT_MAX_AGE_SECONDS = 600
# gate が作った Job が k8s の観測に載るまでの猶予。短すぎると「消えた Job」と
# 誤読して 2 本目を立てる。長すぎると本当に消えた Job の検知が遅れるだけ
JOB_OBSERVATION_GRACE_SECONDS = 300

ADMIT_ACCEPTED = "accepted"
ADMIT_DUPLICATE = "duplicate"
ADMIT_DENIED = "denied"


def admit(request, snapshot, rules, now, *, inflight=(), recent=()):
    """即時 dispatch 要求の可否を導く純関数。I/O を書かない。

    引数:
      request  {"title", "body", "capabilities"} — コアが投げた生の要求
      snapshot heart が直近のビートで公開した状態の写し。None は「まだ写しが無い」
               {"at", "stop_engaged", "running", "dispatch_ids"}
      inflight 受理済みでまだ snapshot に反映されていない dispatch_id の集合。
               走行数に足して数える (ビートを待たずに上限を超えないため)
      recent   直近の受理時刻 (ISO) の並び。レート制限に使う

    返り値: {"status", "reason", "message", "dispatch_id"}
      status は accepted / duplicate / denied。duplicate は失敗ではない
      (同じ要求の再送 = 冪等に 1 件へ畳んだ、という応答)。
    """
    title, body = dispatch.normalize(request)
    did = dispatch.dispatch_id(title, body)

    def out(status, reason, message):
        return {
            "status": status, "reason": reason, "message": message, "dispatch_id": did,
        }

    # 写しが無い = heart がまだ 1 ビートも回っていない。stop_engaged すら読めないので閉じる
    if not snapshot:
        return out(ADMIT_DENIED, "heart_not_ready",
                   "heart がまだ状態を公開していません。少し待ってから頼み直してください")

    # --- 人間の停止意思は他のどの判定よりも先 ---
    if snapshot.get("stop_engaged"):
        return out(ADMIT_DENIED, "stop_engaged",
                   "人間が全停止を指示しています。「再開」と言われるまで新しい着手はしません")

    # 写しが古いと、直前に来た停止を見落としうる。分からないときは着手しない
    age = (now - parse_iso(snapshot["at"])).total_seconds()
    if age > DISPATCH_SNAPSHOT_MAX_AGE_SECONDS:
        return out(ADMIT_DENIED, "state_stale",
                   f"heart の状態が {int(age)} 秒前のもので古すぎます "
                   "(ビートが詰まっている可能性)。安全のため受け付けません")

    if snapshot.get("shadow"):
        return out(ADMIT_DENIED, "shadow_mode",
                   "heart が shadow モードで動いていて、Job を作りません")

    # --- 要求そのものの検査 ---
    if not title:
        return out(ADMIT_DENIED, "invalid", "title が空です。何をするのか 1 行で書いてください")
    if not body:
        return out(ADMIT_DENIED, "invalid", "body が空です。何をどうしたいかを書いてください")
    if len(title) > dispatch.MAX_TITLE_CHARS:
        return out(ADMIT_DENIED, "invalid", f"title が長すぎます ({len(title)} 文字)")
    if len(body) > dispatch.MAX_BODY_CHARS:
        return out(ADMIT_DENIED, "invalid", f"body が長すぎます ({len(body)} 文字)。要点に絞ってください")

    # --- capability の宣言連鎖 (決定 #5) ---
    # spec が宣言し、予告に載ったものだけが write SA を得る。即時 dispatch は
    # 予告を経ないので、**capability は一切名乗れない**。要求ごと弾く
    if request.get("capabilities"):
        return out(ADMIT_DENIED, "capability_not_declared",
                   "即時 dispatch では capability (kubectl-write 等) を要求できません。"
                   "spec と予告の宣言連鎖を通る通常の採択に回してください")

    # --- 冪等: 同じ内容は何度投げても 1 件 ---
    known = snapshot.get("dispatch_ids") or {}
    if did in known:
        return out(ADMIT_DUPLICATE, "already_dispatched",
                   f"同じ要求は既に受理済みです ({known[did]})。二重には着手しません")
    if did in set(inflight):
        return out(ADMIT_DUPLICATE, "in_flight",
                   "同じ要求を受理済みで、採択ゲートを実行中です")

    # --- レート制限: コアが連打しても暴走しない ---
    window = now - timedelta(minutes=DISPATCH_RATE_WINDOW_MINUTES)
    fresh = [t for t in recent if parse_iso(t) > window]
    if len(fresh) >= DISPATCH_RATE_LIMIT:
        return out(ADMIT_DENIED, "rate_limited",
                   f"直近 {DISPATCH_RATE_WINDOW_MINUTES} 分の即時 dispatch が上限 "
                   f"{DISPATCH_RATE_LIMIT} 件に達しています")

    # --- 並列上限: ノードの物理的な容量 (rules.json の max_concurrent) ---
    limit = rules["runner"]["max_concurrent"]
    running = int(snapshot.get("running", 0)) + len(
        [d for d in set(inflight) if d not in known]
    )
    if running >= limit:
        return out(ADMIT_DENIED, "capacity",
                   f"同時走行の上限 {limit} 本に達しています ({running} 本走行中)。"
                   "空くまで着手できません")

    return out(ADMIT_ACCEPTED, "", "受理しました")


def _too_fresh_to_be_missing(project, now):
    """作られたばかりの Job を「消えた」と誤読しないための猶予。

    job_created_at を持つのは gate が作った Job だけ (heart 自身の spawn は
    execute() の中で作るので、次のビートの Job 収集より必ず先にある)。
    """
    created = project.get("job_created_at")
    if not created:
        return False
    return (now - parse_iso(created)) < timedelta(seconds=JOB_OBSERVATION_GRACE_SECONDS)


def _fold_dispatches(doc, facts, now):
    """gate が inbox に置いた dispatch の結末を projects.json に取り込む。

    Job は既に走っている (or 走らなかった) ので、ここは登録と記録だけをする。
    同じ dispatch_id / project id は 2 度登録しない (再実行しても増えない)。
    """
    actions = []
    known_ids = {p["id"] for p in doc["projects"]}
    known_dispatch = {p.get("dispatch_id") for p in doc["projects"] if p.get("dispatch_id")}
    for record in facts.get("dispatches") or []:
        did = record.get("dispatch_id")
        pid = record.get("project_id")
        if not did or not pid:
            continue
        if did not in known_dispatch and pid not in known_ids:
            project = dispatch.to_project(record, now)
            doc["projects"].append(project)
            known_ids.add(pid)
            known_dispatch.add(did)
            if project["state"] == "stalled":
                actions.append(
                    _action("notify", pid, ntype="question",
                            text=f"{pid}: コアの即時 dispatch は着手しませんでした "
                                 f"({project.get('stalled_reason')})。{record.get('detail', '')}".strip())
                )
            else:
                actions.append(
                    _action("notify", pid, ntype="announce",
                            text=f"{pid}: {project['title']}\n"
                                 f"コアの要求で即時に着手しました (job={project.get('job')})。\n"
                                 f"検証: {'; '.join(project['verify'])}",
                            requested_by="core")
                )
        actions.append(
            _action("consume_dispatch", pid, dispatch_id=did,
                    audit=dispatch.audit_lines(record, now), requested_by="core")
        )
    return actions



def merge_conflict(err):
    """merge_pr の失敗記録がコンフリクトか (再試行で直らない失敗か) を判定する純関数。

    GitHub の merge API は 405 を複数の理由で返す ("Base branch was modified"、
    必須チェック未達など)。それらは再試行で直るので、**理由の文字列まで見て**
    コンフリクトだけを選り分ける。ネットワーク断や 5xx は従来どおり再試行する。
    """
    if not isinstance(err, dict):
        return False
    try:
        status = int(err.get("status"))
    except (TypeError, ValueError):
        return False
    if status != MERGE_CONFLICT_STATUS:
        return False
    return MERGE_CONFLICT_MARKER in str(err.get("reason", "")).lower()


def _action(kind, project_id=None, **kw):
    a = {"type": kind}
    if project_id is not None:
        a["project"] = project_id
    a.update(kw)
    return a


def _veto_deadline(project, facts, rules, now):
    """予告からの拒否権窓の期限。空きスロットがあり非不可逆なら即着手 (窓 0)。
    不可逆ラベル付きは常に窓を待つ (決定 #3)。

    「アイドル (走行 0)」でなく「空きスロット」基準 (2026-08-10): 完全アイドル基準だと
    merging 詰まり 1 件が全案件を 24h 窓に落とす渋滞が実際に起きた (main の同修正を
    P-0026 merge 時に消失させないこと)。"""
    if not project.get("irreversible") and facts.get("running_runners", 0) < rules[
        "runner"
    ]["max_concurrent"]:
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


def _register_spec(doc, spec, now):
    """採択された spec を proposed として projects.json に登録する。

    spec 全文を **projects.json に持たせる** (設計 rev3 D32)。ここが runner の
    読み先になり、着手が main への PR / CI / merge を待たなくなる。ops-state の
    書き手は heart だけなので、正を移しても改竄耐性は落ちない。
    """
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
            # 消費量は計測として持つだけ (上限は無い)
            "budget": {"used_tokens": 0},
            "created": now_iso(now)[:10],
            # runner が読む spec の正 (dispatch 経路の to_project() と同じ形)
            "spec": dict(spec),
        }
    )


def _archive_backfill(doc, facts, limit=ARCHIVE_BACKFILL_LIMIT):
    """main の archive.jsonl にまだ載っていない採択 spec を返す (純関数)。

    dispatch の正が ops-state に移った結果、採択は台帳への追記を待たずに
    動き出す (D32)。台帳が欠落しないよう、次の curriculum Job にまとめて
    渡して同じ PR で追記させる。即時 dispatch の P-9NNN もここで拾われる。
    """
    in_archive = {
        s.get("id") for s in (facts.get("adopted_specs") or []) if isinstance(s, dict)
    }
    out = []
    for p in doc["projects"]:
        spec = p.get("spec")
        if not isinstance(spec, dict) or not spec.get("id"):
            continue
        if spec["id"] in in_archive:
            continue
        out.append(spec)
    return out[:limit]


def _critic_due(doc, now):
    """日次の自己観測 (critic Job) を spawn してよいか。純関数。

    条件は 2 つとも要る:
      (a) 前回 spawn から CRITIC_INTERVAL_HOURS 経過している (初回は無条件)
      (b) 前回 spawn 以降に活動があった (actions のあるビートが 1 度でもあった)

    (b) が無いと、何も動いていない器を毎日読ませてトークンだけ燃やす。
    刻むのは **spawn した時刻** (完了時刻ではない) — curriculum と同じ流儀で、
    Job の完了を待つ間に二重 spawn しない。

    活動の記録 (doc["last_activity_at"]) には critic 自身が生んだ action を
    数えない (decide の末尾で、critic の action を積む **前** に刻む)。
    数えると critic が自分で自分の due 条件を成立させ続ける自励発振になる。
    """
    activity = doc.get("last_activity_at")
    if not activity:
        return False
    last = doc.get("last_critic_at")
    if last is None:
        return True  # 一度も観測していない。活動が記録され次第すぐ見る
    if (now - parse_iso(last)) < timedelta(hours=CRITIC_INTERVAL_HOURS):
        return False
    return parse_iso(activity) > parse_iso(last)


def decide(doc, facts, rules, now):
    """(projects doc, facts) -> (新 doc, actions)。doc は破壊的に更新して返す。"""
    actions = []
    vetoes = set(facts.get("vetoes", []))
    # 「止めて」は受信したビートだけでなく、人間が「再開」と言うまで効き続ける。
    # stop_all はコメント既読カーソルの進みとともに次ビートで False に戻るため、
    # そのままでは全 stalled 化 → 全部終端 → アイドル判定 → curriculum が再点火して
    # 器全体が勝手に走り直す (2026-08-10 の全停止要求で実際に起きかけた)。
    # doc に永続化し、同ビートに両方来たら停止を優先する
    stop_all = facts.get("stop_all", False)
    if facts.get("resume_all") and not stop_all:
        doc["stop_engaged"] = False
    if stop_all:
        doc["stop_engaged"] = True
    stop_all = stop_all or bool(doc.get("stop_engaged"))
    jobs = facts.get("jobs")  # None = 観測失敗 (「無い」と区別する)
    results = facts.get("results", {})
    reviews = facts.get("reviews", {})
    prs = facts.get("open_prs", {})
    merged_prs = facts.get("merged_prs", {})
    unhealthy = facts.get("unhealthy_apps")  # None = 観測失敗

    # --- 即時 dispatch (設計 rev3 Phase D) の結末を取り込む ---
    # gate スレッドが admission を判定し、採択ゲートを実測し、Job まで作ってある。
    # ここは ops-state への登録と記録だけ (書き手は heart のビートのまま)。
    # **running を数える前に折り込む** — dispatched は active で入るので、
    # 数えた後に足すとこのビートの spawn 判断が上限を 1 本ぶん超える
    actions.extend(_fold_dispatches(doc, facts, now))

    running = sum(
        1 for p in doc["projects"] if p["state"] in ("active", "in_review", "merging")
    )

    # --- 台帳 (main の archive.jsonl) に載った採択 spec も登録する ---
    # 着手の正は ops-state の projects.json に移った (設計 rev3 D32) が、この経路は
    # 残す。人間が archive.jsonl に adopted 行を足す手動採択の入口がここだからで、
    # 「main に載れば動き出す」という意味論は**手動採択については変えていない**。
    # 変わったのは curriculum の採択で、そちらは PR の merge を待たずに
    # result.json 経由で登録される (下の curriculum 節)。
    # 終端 (delivered/stalled/vetoed) のエントリも projects に残るため、済んだ spec が
    # ここで蘇ることはない。projects.json の終端エントリを将来間引くときは、
    # 登録済み id の記録を別に持つこと
    existing_ids = {p["id"] for p in doc["projects"]}
    for spec in facts.get("adopted_specs") or []:
        if spec.get("id") and spec["id"] not in existing_ids:
            _register_spec(doc, spec, now)
            existing_ids.add(spec["id"])

    # --- curriculum の採択は台帳 PR を待たずに登録する (設計 rev3 D32) ---
    # spec は result.json に載っている。ここで projects.json (= dispatch の正) に
    # 登録した時点で採択ゲート → 予告 → 着手が始まり、main への PR・CI・merge は
    # 台帳の追記として非同期に流れる (下の curriculum 節が merge を進める)。
    # **意味論の変更**: 台帳 PR を人間が close しても採択は取り消されない。
    # 取り消しは veto (予告窓) で行う — 窓は従来どおり効いている。
    # 同じ result を毎ビート読み直すので、登録は PR 番号で 1 度に畳む。
    # **この位置で登録する** — 下の curriculum 節はプロジェクトの状態機械より後ろに
    # あり、そこで登録すると採択ゲートの実測が 1 ビート遅れる
    cur_facts = facts.get("curriculum") or {}
    if (
        cur_facts.get("state") == "curriculum_done"
        # 既定値は None でなく番人にする — PR 番号が None のときに
        # 「登録済み」と読み違えて 1 ラウンド丸ごと落とさないため
        and doc.get("curriculum_registered_pr", _UNSET) != cur_facts.get("pr")
    ):
        adopted = [
            s for s in (cur_facts.get("adopted_specs") or []) if s.get("id")
        ]
        for spec in adopted:
            if spec["id"] not in existing_ids:
                _register_spec(doc, spec, now)
                existing_ids.add(spec["id"])
        # 実りの有無を記録する。次のアイドルで即座に立案してよいか (実りあり) /
        # min_interval の間隔を置くべきか (空振り) の判定に使う (2026-08-10)
        doc["last_curriculum_dry"] = not adopted
        # 採択された依頼由来の案 (request_id 持ち) があれば、その依頼を処理済みに
        # する (P-0091)。対応づけは案に埋まった request_id の一致だけで決定論。
        # 実行は heart.execute() が tasks.mark_processed() で行う (冪等)
        done = tasks.done_ids(adopted)
        if done:
            actions.append(_action("mark_task_requests_done", ids=done))
        doc["curriculum_registered_pr"] = cur_facts.get("pr")

    # --- 既読化 (ack P-NNNN): 終端プロジェクトの墓標を要対応キューから下げる ---
    # 状態は変えない (歴史は残す)。ダッシュボードが acknowledged を隠す
    acks = set(facts.get("acks", []))
    if acks:
        for p in doc["projects"]:
            if p["id"] in acks and p["state"] in TERMINAL_STATES:
                p["acknowledged"] = True

    # --- 承認 (approve P-NNNN): 拒否権窓を人間の意思で畳む ---
    # 可逆案の窓は空きスロットがあれば自動で繰り上がるので、これが効くのは実質
    # 不可逆案 (窓が明けるまで着手しない) と満席のとき。状態は変えない —
    # deadline を今にするだけで、着手の可否は下の announced 分岐が従来どおり判断する
    # (並列上限を迂回させない)。停止中は効かせない
    approves = set(facts.get("approves", []))
    if approves and not stop_all:
        for p in doc["projects"]:
            if p["id"] not in approves or p["state"] != "announced":
                continue
            if p["id"] in vetoes:
                continue  # 同じビートで veto と approve が来たら止める方に倒す
            if parse_iso(p["veto_deadline"]) <= now:
                continue  # 既に窓は明けている
            p["veto_deadline"] = now_iso(now)
            p["approved_by_human_at"] = now_iso(now)
            actions.append(
                _action(
                    "notify",
                    p["id"],
                    ntype="notify",
                    text=f"{p['id']} の拒否権窓を人間の承認により繰り上げました",
                )
            )

    # --- コア発の command (設計 D3/D7/D21) ---
    # 常駐コアは git にも K8s にも書かない。実装依頼は bus に publish され、
    # サイドカーがファイルに落とし、ここで初めて heart の仕事になる。
    # 取り込みは仕事を作らない (立案・spawn 側が並列上限を見る) ので、停止中でない
    # 限りいつでも受け取る。落とすと来た依頼が黙って消える。
    #
    # 守るのは 2 つ:
    #   - command_id の台帳で二重実行しない。同じ依頼で 2 つプロジェクトが立つのは
    #     取りこぼしより高くつく
    #   - 停止中 (stop_engaged) は 1 件も実行しない。**台帳にも刻まない**ので、
    #     人間が再開したビートで拾い直す (止めている間の依頼を捨てない)
    processed_commands = set(facts.get("processed_commands") or [])
    for command in facts.get("commands") or []:
        if stop_all:
            break
        cid = str(command.get("command_id") or "").strip()
        if not cid or cid in processed_commands:
            continue
        processed_commands.add(cid)
        ctype = command.get("type")
        if ctype == tasks.KIND_TASK_REQUEST:
            actions.append(
                _action(
                    "ingest_command",
                    command_id=cid,
                    command_type=ctype,
                    status="accepted",
                    title=str(command.get("title") or ""),
                    body=str(command.get("body") or ""),
                )
            )
        else:
            # 知らない種別は実行しない。ただし台帳には刻む — 刻まないと同じ
            # command を毎ビート見て通知が発振する
            actions.append(
                _action(
                    "ingest_command",
                    command_id=cid,
                    command_type=str(ctype),
                    status="unsupported",
                )
            )
            actions.append(
                _action(
                    "notify", None, ntype="notify",
                    text=f"コアから未知の command 種別 {ctype} を受け取りました "
                         f"(id={cid})。実行していません",
                )
            )

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
            # --- 採択ゲート: 予告の前に、新品 clone で verify を実測する (P-0015) ---
            # **dispatch 由来 (P-9NNN) で verify を持たない spec は通さない**
            # (2026-08-24 の所有者判断)。verify を書くのも LLM なので迂回でき、
            # 機械の判定として意味を成さない。ゲートを積まずに次へ進める —
            # ここで積むと adoptgate.classify() が「verify が空 = broken_command」と
            # 判定し、依頼が必ず stalled (終端) に落ちる。
            # curriculum 由来 (verify を持つ) は今までどおり測る
            if not (p.get("dispatch_id") and not p.get("verify")):
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
            # 窓ゼロ (アイドルかつ可逆) なら同じビートで着手する。予告→着手の間で
            # 1 ビートを空費しない (2026-08-09 テンポ改善)。見るのは capacity だけ
            if (
                parse_iso(p["veto_deadline"]) <= now
                and running < rules["runner"]["max_concurrent"]
            ):
                p["state"] = "active"
                running += 1
                actions.append(_action("spawn_runner", pid))

        elif state == "announced":
            if parse_iso(p["veto_deadline"]) > now:
                # 窓の繰り上げ: 予告時に満席だったために窓が付いた可逆案は、
                # スロットが空いた時点で即着手してよい (稼働率基準、2026-08-10 に
                # 空きスロット基準へ変更)。不可逆案は繰り上げない
                if (
                    p.get("irreversible")
                    or running >= rules["runner"]["max_concurrent"]
                ):
                    continue
                p["veto_deadline"] = now_iso(now)
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
                p.pop("quota_wait_since", None)
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
            elif result and result.get("state") == "session_limit":
                actions.append(_action("consume_result", pid))
                _stall(
                    p, actions, "session_limit", "question",
                    f"{pid} が 1 プロジェクトあたりのセッション上限 "
                    f"({rules['runner']['max_sessions_per_project']}) に達しました。"
                    "同じところを回り続けている可能性があります。"
                    "続ける価値があるか判断してください",
                )
            elif result and result.get("state") == "waiting_quota":
                # アカウントの利用上限は器の外側の事実であって、プロジェクトの停滞
                # ではない。通知も出さない (障害ではない)。projects.json の state は
                # active のまま、resume_after まで待って runner を出し直す。
                # **ただし無限には待たない** — 冒頭の不変条件「恒久的に黙って待つ状態を
                # 作らない」はこの待ちにも掛かる
                actions.append(_action("consume_result", pid))
                p["quota_wait_count"] = p.get("quota_wait_count", 0) + 1
                # 打ち切りの基準は回数ではなく**連続して待った実時間**。待ち始めた
                # 時刻を doc に持ち、そこからの経過で測る (回数はビート周期に依存する
                # ので閾値として意味を持たない。回数は記録としてだけ残す)
                since = p.setdefault("quota_wait_since", now_iso(now))
                if (now - parse_iso(since)) > timedelta(hours=QUOTA_WAIT_MAX_HOURS):
                    # 上限が明けないまま丸一日待ち続けている。器の側では直せない
                    # (待つ以外に手が無い) ので人間に判断を渡す。スロットもここで
                    # 解放される。札・回数・起点は落とす — 残すと人間が active に
                    # 戻した次の waiting_quota で即また stalled になる (再開できない停止)
                    rounds = p.pop("quota_wait_count")
                    p.pop("quota_wait_until", None)
                    p.pop("quota_wait_since", None)
                    _stall(
                        p, actions, "quota_wait_exhausted", "question",
                        f"{pid} がアカウントの利用上限で "
                        f"{QUOTA_WAIT_MAX_HOURS} 時間以上 ({rounds} 回) 連続して"
                        "待機し続けています。上限が明けていないか、死因の判定が"
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
            elif job is None and _too_fresh_to_be_missing(p, now):
                # 作った直後の Job は、まだ観測に載っていないのが普通。
                # gate (別スレッド) が Job を作るのはビートの外側なので、
                # Job 収集 → gate の作成 → 折り込み、の順に起きたビートでは
                # 「走っているはずの Job が居ない」が必ず 1 回成立する。
                # ここで乖離と数えると attempt が進んで **2 本目の Job が立つ**
                continue
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
                # 直前の merge がコンフリクトで失敗していたら、もう叩かない。
                # 失敗の事実は heart.execute() が p["merge_error"] に書き戻す
                # (adopt_gate と同じ「実測は execute・判定は純関数」の分担)。
                # 別 PR に対する古い記録では止めない
                err = p.get("merge_error")
                if err and err.get("pr") == pr_num and merge_conflict(err):
                    _stall(
                        p, actions, "merge_conflict", "question",
                        f"{pid} の PR #{pr_num} が main とコンフリクトしています。"
                        "再試行では直らないので merge を止めました。ブランチを"
                        "作り直すか、PR を閉じる判断をください",
                    )
                    continue
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
        # 採択の登録と依頼の処理済み化は decide の冒頭で済んでいる (D32)。
        # ここは台帳 PR の後始末だけ — merge するか、諦めて結果を捨てるか
        if cur.get("pr_unknown"):
            pass  # PR の状態が観測できないビートでは merge/破棄の判断をしない
        elif cur.get("pr_merged"):
            actions.append(_action("consume_curriculum"))
            curriculum_pending = False
        elif cur.get("pr_open") and cur.get("checks_green"):
            actions.append(_action("merge_pr", "system", pr=cur["pr"]))
        elif cur.get("pr") is None or (
            not cur.get("pr_open") and not cur.get("pr_merged")
        ):
            # PR が無い、または merge されずに close された。結果 (result.json) を
            # 破棄する。採択は既に projects.json に登録済みなので、これは台帳への
            # 追記が流れなかったというだけ — 次の curriculum の backfill が拾う
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
        # エラーは空振り扱い: 即再立案せず min_interval を置く (連打防止)
        doc["last_curriculum_dry"] = True
        curriculum_pending = False

    # 走行中の curriculum Job も「立案中」。result.json は Job 完走まで存在しないので、
    # 結果ファイルだけを見ると走行中の 10 数分が「立案していない」に見える。eager 立案
    # (実りの直後は min_interval 免除) と重なると、アイドルの毎ビートに新しい Job を
    # spawn し続ける (2026-08-10 に毎分 1 Job の暴走が実際に起きた)。
    # jobs 観測に失敗したビート (None) は「走っていない」と断定できないので spawn しない
    if jobs is None:
        curriculum_pending = True
    elif any(
        name.startswith("curriculum-") and st.get("active")
        for name, st in jobs.items()
    ):
        curriculum_pending = True

    # アイドルの定義 (2026-08-10 改定、人間の指摘「アイドル中に何もしない理由が無い」):
    #   - 拒否権窓で待機中の announced (deadline が未来) はスロットを使っていないので
    #     「仕事がある」に数えない。窓 24h の案件 1 つが立案を丸一日塞ぐ実害があった
    #   - min_interval は空振り (採択ゼロ / エラー) の後にだけ適用する。実りある回の後の
    #     アイドルは「仕事が終わった」なので即座に次を立案してよい。間隔が守っているのは
    #     「同じ世界に同じ問いを連打してトークンを燃やす」ことだけ
    # 2026-08-22 改定 (人間の指示「がっつり並列」): 「完全アイドル」でなく
    # 「パイプラインに空きがある」を立案の条件にする。active 1 本が残っている間
    # ずっと立案が止まり、max_concurrent=4 でも常時 2 本しか走らない実態があった。
    # 深さの上限はパイプライン (窓待ちを除く非終端) を max_concurrent と比べて守り、
    # 採択数の上限も空き分 (adopt_limit) として curriculum judge に渡す
    pipeline = [
        p for p in doc["projects"]
        if p["state"] not in TERMINAL_STATES
        and not (
            p["state"] == "announced"
            and parse_iso(p.get("veto_deadline", now_iso(now))) > now
        )
    ]
    free_slots = rules["runner"]["max_concurrent"] - len(pipeline)
    curriculum_idle = free_slots > 0 and not curriculum_pending
    last_curriculum = doc.get("last_curriculum_at")
    min_gap = timedelta(hours=rules["curriculum"].get("min_interval_hours", 6))
    gap_ok = last_curriculum is None or (now - parse_iso(last_curriculum)) >= min_gap
    if doc.get("last_curriculum_dry") is False:
        gap_ok = True
    if curriculum_idle and gap_ok and not stop_all:
        doc["last_curriculum_at"] = now_iso(now)
        actions.append(
            _action(
                "spawn_curriculum", adopt_limit=free_slots,
                # 台帳の遅延追記をこの Job の PR にまとめて載せる (D32)
                archive_backfill=_archive_backfill(doc, facts),
            )
        )

    # --- 活動の記録 (critic の due 判定の材料) ---
    # ここまでに積んだ action だけを「活動」と数える。この行より後に積む critic 自身の
    # action (consume_critic / notify_critic / spawn_critic) は数えない (_critic_due 参照)
    if actions:
        doc["last_activity_at"] = now_iso(now)

    # --- critic: 前回の所見の消費と、日次の自己観測 (P-0045) ---
    # 器が自分の詰まり (状態別の滞留・アイドル率) と利用者面の不満を、人間より先に
    # 見つけるための常設の器官。**見つける役であって直す役ではない** ので、ここでは
    # 所見を人間 (notify) と次の立案 (/data/critic/) に流すところまでしかしない
    critic = facts.get("critic")  # /data/projects/critic/result.json (無ければ None)
    if critic:
        # 消費しないと同じ結果を毎ビート再消費して通知が発振する (curriculum と同じ罠)
        actions.append(_action("consume_critic"))
        if critic.get("state") == "done":
            # 本文は所見ファイル (/data/critic/<日付>.md) 側にあり、純関数からは
            # 読めない。読んで整形するのは heart.execute() の仕事
            actions.append(_action("notify_critic"))
        else:
            actions.append(
                _action(
                    "notify", "critic", ntype="incident",
                    text=f"critic Job が {critic.get('state')} で終了: "
                         f"{str(critic.get('error', ''))[:200]}",
                )
            )
    # stop_all 中は新しい仕事を作らない (冒頭の不変条件)。
    # max_concurrent は見ない — critic は runner スロットを消費しない別 Job
    if _critic_due(doc, now) and not stop_all:
        doc["last_critic_at"] = now_iso(now)
        actions.append(_action("spawn_critic"))

    return doc, actions
