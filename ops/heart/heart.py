"""heart — 決定論 reconcile ループの本体。

起動: リポジトリ checkout の直下で `python3 -m ops.heart.heart`
(apps/autopilot/ の bootstrap ConfigMap が clone → exec する)。

1 ビート (既定 120s):
  1. main を最新化 (**読むだけ**。git への書き込みはビートに 1 つも無い)
  2. 事実収集 (health / Job / PVC の result / PR / フィードバック 2 経路)
  3. reconcile.decide() — 判断はすべて純関数側
  4. actions 実行 (shadow モードでは記録のみ)
  5. 指標追記・heartbeat・Project CR の更新・Lease の更新

heartbeat は旧 loop.sh と同じ書式で stdout に出す。ops-health-reporter の
HEARTBEAT_RE (report.py) がこれを拾って自己ハング検知に使うため、
書式を変えるときは report.py と CHARTER §2 を同時に変えること。
"""

import json
import os
import shutil
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

from . import (
    adoptgate,
    config,
    dispatch,
    facts,
    gate,
    gitutil,
    lease,
    metrics,
    projectcr,
    reconcile,
    spawn,
    tasks,
)
from .gh import Gh, GhError
from .notify import Notifier, veto_footer
from .statefiles import TERMINAL_STATES, StateFiles, migrate_plan, now_iso

# Project CR の書き込み失敗を人間に鳴らすまでの連続ビート数 (note_cr_failures)。
# 120s/beat なので 5 ビート ≒ 10 分。瞬断は黙って通り、スキーマの穴は鳴る
CR_FAIL_ALERT_BEATS = 5

# CR の取りこぼし (migrate_projects_doc) を鳴らし直す間隔。毎ビート鳴らすと
# 通知が壊れた側になり、鳴らさないと静かに移行が止まったままになる
PARITY_ALERT_BEATS = 60

# 通知に載せる取りこぼし id の上限 (全部載せると Discord の 1900 字を越える)
PARITY_ALERT_IDS = 10

# 棄却案の台帳 (PVC)。git の ops/projects/archive.jsonl への追記を止めた 4b-2b
# 以後、新しい死因はここに積み、次のビートが Project CR に移す
REJECTED_LEDGER_FILE = "curriculum-rejected.jsonl"

_stop = False


def _sigterm(_sig, _frame):
    global _stop
    _stop = True


def _names(directory):
    """ディレクトリ直下のファイル名。無ければ空。"""
    if not directory.is_dir():
        return []
    return [p.name for p in directory.iterdir() if p.is_file()]


def log(msg):
    print(f"[autopilot] {now_iso()} {msg}", flush=True)


def spec_env(project):
    """projects.json に載っている spec を Job の env にも積む。

    spec の正は Project CR で、runner はそこを読めない (worker はトークンを
    automount しない)。env がその写しで、runner の唯一の読み先になる:

    - 即時 dispatch は **Job 作成がビートの CR 更新より先**なので、走り出しの
      瞬間はまだ CR に載っていない。そこを env が埋める
    - k8s API が読めないビートでも走り出せる

    経路は違うが**書き手が heart だけ**という性質は同じで、Job の spec に
    固定される env は runner のブランチからは書き換えられない。
    """
    spec = (project or {}).get("spec")
    if not spec:
        return {}
    return {"HEART_SPEC_JSON": json.dumps(spec, ensure_ascii=False)}


def announce_text(project):
    lines = [
        f"{project['id']}: {project['title']}",
        f"検証: {'; '.join(project.get('verify', [])) or '(spec 参照)'}",
        f"不可逆: {'あり' if project.get('irreversible') else 'なし'} / "
        f"自信: {project.get('confidence', 'unsure')}",
    ]
    if "kubectl-write" in project.get("capabilities", []):
        lines.append("このプロジェクトはクラスタへの書き込み権限 (autopilot-writer) を使います")
    lines.append(veto_footer(project["id"], project.get("veto_deadline")))
    return "\n".join(lines)


class Heart:
    def __init__(self, repo_dir):
        self.cfg = config.load(repo_dir)
        self.repo_dir = self.cfg.repo_dir
        self.repo_url = f"https://github.com/{self.cfg.repo}.git"
        # 移行前の ops-state ブランチの checkout (設計 state-out-of-git 4b-2b で
        # git から切り離した)。**もう fetch も push もしない** — 残っているのは
        # PVC 上の最後の写しで、doc を PVC へ移すときの読み元としてだけ使う
        self.state_dir = self.cfg.data_dir / "ops-state"
        # プロジェクトの doc (projects.json) と heartbeat.json の置き場。
        # 外から見える正は Project CR で、ここはビートの作業用の写し
        self.doc_dir = self.cfg.data_dir / "state"
        self.docs = StateFiles(self.doc_dir)
        # 指標は git に出さない (設計 state-out-of-git Phase 1)。PVC 上に
        # 保持窓ぶんだけ置く。誰も読み戻さないので、消えても判断は狂わない
        self.metrics_store = StateFiles(self.cfg.data_dir / "metrics")
        # heart しか読まない作業ファイル (キュー・監査・カーソル) は git に出さない
        # (設計 state-out-of-git Phase 3)
        self.work_dir = self.cfg.data_dir / "work"
        self.work = StateFiles(self.work_dir)
        self.transcripts = self.cfg.data_dir / "transcripts"
        self.gh = Gh(self.cfg.github_token, self.cfg.repo)
        self.k8s = None  # 遅延初期化 (単体テスト・クラスタ外実行のため)
        self.start_tree = None
        # admission gate は run() で起こす (単体テストで Heart を作るだけのときは
        # ポートを掴まない)。None のままでも heart は従来どおり回る
        self.gate = None
        # Project CR の書き込みが連続して失敗しているビート数 (note_cr_failures)
        self.cr_fail_streak = 0
        # doc を PVC へ移せずにいるビート数 (migrate_projects_doc の守り)
        self.parity_gap_streak = 0

    def k8s_client(self):
        if self.k8s is None:
            from .k8s import K8s

            self.k8s = K8s()
        return self.k8s

    # --- actions ---
    def execute(self, actions, doc, sf, notifier, now):
        shadow = self.cfg.shadow
        by_id = {p["id"]: p for p in doc["projects"]}
        for a in actions:
            kind = a["type"]
            pid = a.get("project")
            p = by_id.get(pid)
            # requested_by は「誰がこの action を要求したか」。既定は heart 自身の
            # 判断で、コアが即時 dispatch で要求したものだけ core になる
            # (設計 rev3「監査に dispatch 元を記録する」)
            audit = {
                "at": now_iso(now), "action": kind, "project": pid,
                "requested_by": a.get("requested_by", "heart"), "shadow": shadow,
            }
            try:
                if kind == "announce":
                    if shadow:
                        log(f"[shadow] announce {pid}")
                    else:
                        notifier.send("announce", announce_text(p), now)
                elif kind == "run_adopt_gate":
                    # 採択ゲートの実測 (P-0015)。使い捨ての新品 clone で spec の
                    # verify を 1 本ずつ実行し、生レコードを書き戻す。判定は
                    # reconcile.adoptgate.classify() が次のビートで導く。
                    # **shadow でも実行する**: 副作用は使い捨て clone の中の読み取り
                    # だけで外に出るものが無く、逆にここを飛ばすと shadow から本番へ
                    # 切り替えた最初のビートで未検査のまま予告が出てしまう。
                    # clone に失敗したら例外 → adopt_gate を書かず次のビートでやり直す
                    # (測れなかったことを all_fail と取り違えない)
                    verify_results = adoptgate.run_gate(
                        self.repo_url, p.get("verify", [])
                    )
                    p["adopt_gate"] = {
                        "at": now_iso(now),
                        "verify": verify_results,
                        **adoptgate.classify(verify_results),
                    }
                    audit["verdict"] = p["adopt_gate"]["verdict"]
                    log(
                        f"adopt gate {pid}: {p['adopt_gate']['verdict']} — "
                        f"{adoptgate.describe(p['adopt_gate'])}"
                    )
                elif kind == "spawn_runner":
                    p["spawn_count"] = p.get("spawn_count", 0) + 1
                    if shadow:
                        log(f"[shadow] spawn runner for {pid}")
                    else:
                        extra = dict(spec_env(p))
                        if a.get("findings"):
                            extra["REVIEW_FINDINGS"] = "\n".join(
                                str(f) for f in a["findings"]
                            )[:4000]
                        p["job"] = spawn.create(
                            self.k8s_client(), self.cfg, "runner",
                            project=p, attempt=p["spawn_count"], extra_env=extra,
                        )
                elif kind == "spawn_reviewer":
                    if shadow:
                        log(f"[shadow] spawn reviewer for {pid}")
                    else:
                        spawn.create(
                            self.k8s_client(), self.cfg, "reviewer",
                            project=p, attempt=p.get("review_cycles", 0),
                            extra_env=spec_env(p),
                        )
                elif kind == "spawn_curriculum":
                    if shadow:
                        log("[shadow] spawn curriculum")
                    else:
                        # 過去案は CR から読んで PVC のファイルに落とす (4b-2a)。
                        # 読めなければ例外が上がり、下の spawn.create まで届かない。
                        # **死因を読めない立案は走らせない方が安い** — 同型再提案を
                        # 採択まで通してしまうため。失敗は audit.jsonl に残る
                        history = self.prepare_curriculum_input()
                        # attempt に分単位の時刻を入れて Job 名を一意にする。固定名だと
                        # 前回分が TTL (6h) 内に残っている間 409 を成功扱いして
                        # 黙って空振りする (レビュー指摘 [20])
                        spawn.create(
                            self.k8s_client(), self.cfg, "curriculum",
                            attempt=int(time.time()) // 60 % 1000000,
                            # 空きスロット分だけ採択してよい (judge プロンプトに渡る)。
                            # 未処理のタスク依頼は立案の最優先原料として渡す (P-0091)
                            extra_env={
                                "ADOPT_LIMIT": a.get("adopt_limit", 2),
                                "TASK_REQUESTS": tasks.for_env(
                                    self.work.read_jsonl(tasks.QUEUE_FILE)
                                ),
                                **history,
                            },
                        )
                elif kind == "mark_task_requests_done":
                    # 採択された依頼由来の案に対応する依頼を処理済みにする
                    # (P-0091)。tasks.mark_processed() が冪等なので、このビートの
                    # 再実行でも二重に刻まない
                    if shadow:
                        log(f"[shadow] mark task requests done: {a.get('ids')}")
                    else:
                        records = self.work.read_jsonl(tasks.QUEUE_FILE)
                        self.work.rewrite_jsonl(
                            tasks.QUEUE_FILE,
                            tasks.mark_processed(records, a.get("ids", []), now),
                        )
                        log(f"task requests processed: {a.get('ids')}")
                elif kind == "ingest_command":
                    # コア発の command を heart の仕事にする (設計 D3/D21)。
                    #
                    # 順序: **キューに載せてから台帳に刻む**。逆順にすると、間で
                    # 落ちたときに「処理済みなのにキューに無い」= 依頼が消える。
                    # この順なら再実行しても merge_new が id で落とすので、
                    # 同じ依頼が 2 件になることはない
                    if shadow:
                        log(f"[shadow] ingest command {a.get('command_id')} "
                            f"({a.get('status')})")
                    else:
                        if a.get("status") == "accepted":
                            body = a.get("body", "")
                            if a.get("title"):
                                body = f"{a['title']}\n\n{body}"
                            records = self.work.read_jsonl(tasks.QUEUE_FILE)
                            merged = tasks.merge_new(
                                records,
                                [{"source": tasks.command_source(a["command_id"]),
                                  "body": body}],
                                now,
                            )
                            if len(merged) != len(records):
                                self.work.rewrite_jsonl(tasks.QUEUE_FILE, merged)
                        self.work.append_jsonl(
                            tasks.COMMAND_LEDGER_FILE,
                            tasks.ledger_entry(
                                a["command_id"], a.get("command_type", ""),
                                a.get("status", ""), now,
                            ),
                        )
                        log(f"command ingested: {a['command_id']} ({a.get('status')})")
                elif kind == "spawn_critic":
                    if shadow:
                        log("[shadow] spawn critic")
                    else:
                        extra = self.prepare_critic_input(sf, doc, now)
                        # 結果置き場を "system" (curriculum の領分) と分ける。
                        # attempt は curriculum と同じく分単位の時刻で一意にする
                        # (固定名だと TTL 6h 内の残骸に 409 で黙って負ける)
                        spawn.create(
                            self.k8s_client(), self.cfg, "critic",
                            project_id="critic",
                            attempt=int(time.time()) // 60 % 1000000,
                            extra_env=extra,
                        )
                        log(f"spawned critic (input={extra['CRITIC_INPUT']})")
                elif kind == "notify_critic":
                    text = self.critic_summary()
                    if shadow:
                        log(f"[shadow] notify[critic] {text[:80]}")
                    elif text:
                        notifier.send("critic", text, now)
                    else:
                        # 所見ファイルが無い = critic が rc=0 で終わったのに何も
                        # 書いていない。黙って消さずに人間に見せる
                        notifier.send(
                            "incident",
                            "critic Job が正常終了したが /data/critic/ に所見が無い",
                            now,
                        )
                elif kind == "kill_job":
                    if shadow:
                        log(f"[shadow] kill job {a.get('job')}")
                    else:
                        try:
                            self.k8s_client().delete_job(self.cfg.namespace, a["job"])
                        except Exception as e:
                            log(f"kill_job {a['job']} failed: {e}")
                elif kind == "merge_pr":
                    if shadow:
                        log(f"[shadow] merge PR #{a['pr']} for {pid}")
                    else:
                        # 失敗の事実だけを doc に刻む。「再試行してよい失敗か」の
                        # 判断は次のビートの reconcile.merge_conflict() が下す
                        # (adopt_gate と同じ分担: 実測は execute、判定は純関数)
                        try:
                            self.gh.merge_pr(a["pr"])
                        except GhError as e:
                            if p is not None:
                                p["merge_error"] = {
                                    "at": now_iso(now),
                                    "pr": a["pr"],
                                    "status": e.status,
                                    "reason": e.message[:300],
                                }
                            raise
                        if p is not None:
                            p.pop("merge_error", None)
                elif kind == "deliver":
                    text = f"{pid}: {p['title']} を納品しました"
                    if shadow:
                        log(f"[shadow] deliver {pid}")
                    else:
                        notifier.send("deliver", text, now)
                elif kind == "notify":
                    if shadow:
                        log(f"[shadow] notify[{a.get('ntype')}] {a.get('text', '')[:80]}")
                    else:
                        notifier.send(a.get("ntype", "notify"), a.get("text", ""), now)
                elif kind in (
                    "consume_result", "consume_review",
                    "consume_curriculum", "consume_critic",
                ):
                    # 消費した事実ファイルを退避する。残すと次のビートが同じ事実を
                    # 再消費して状態機械が発振する (レビュー指摘 [0])
                    name = {
                        "consume_result": "result.json",
                        "consume_review": "review.json",
                        "consume_curriculum": "result.json",
                        "consume_critic": "result.json",
                    }[kind]
                    target = {
                        "consume_curriculum": "system",
                        "consume_critic": "critic",
                    }.get(kind, pid)
                    if shadow:
                        log(f"[shadow] consume {target}/{name}")
                    else:
                        self.consume_file(target, name, now)
                elif kind == "consume_dispatch":
                    # 即時 dispatch (設計 rev3 Phase D) の結末を消費する。
                    # gate が書いた audit 行をここで audit.jsonl に移す —
                    # 作業ファイルへの書き込みはビート側の単一書き手のまま
                    for line in a.get("audit") or []:
                        self.work.append_jsonl("audit.jsonl", line)
                    self.consume_dispatch(a.get("dispatch_id"), now)
                    log(f"dispatch consumed: {a.get('dispatch_id')} -> {pid}")
                elif kind == "record_drift":
                    audit["reason"] = a.get("reason")
            except Exception as e:
                audit["error"] = str(e)[:300]
                log(f"action {kind} for {pid} failed: {e}")
            self.work.append_jsonl("audit.jsonl", audit)

    def consume_dispatch(self, dispatch_id, now):
        """消費済みの dispatch レコードを done/ へ移す (削除でなく退避 — 監査用)。"""
        if not dispatch_id:
            return
        base = self.cfg.data_dir / dispatch.DISPATCH_DIR
        src = base / dispatch.INBOX / f"{dispatch_id}.json"
        if not src.exists():
            return
        dst = base / dispatch.DONE / f"{now_iso(now).replace(':', '')}-{dispatch_id}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    def consume_file(self, project_id, name, now):
        """消費済みの result/review を processed/ へ移す (削除でなく退避 — 監査用)。"""
        src = self.cfg.data_dir / "projects" / project_id / name
        if not src.exists():
            return
        dst = src.parent / "processed" / f"{now_iso(now).replace(':', '')}-{name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    # --- curriculum (立案) の入力 ---
    def curriculum_dir(self):
        d = self.cfg.data_dir / "curriculum"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def prepare_curriculum_input(self):
        """過去案の要点を /data/curriculum/proposals.jsonl に落とし、Job に渡す env を返す。

        curriculum Job は `autopilot-runner` SA でトークンを automount して
        **いない** (spawn.build_job)。worker がクラスタ API に触れないのは決定 #5
        の境界そのものなので、立案のためにそこを開けるのは筋が悪い。代わりに
        heart が読んで PVC のファイルに落とす — critic の input-<日付>.json と
        同じ形で、Job は /data を既にマウントしている。

        書き出すのは **棄却案を含む全件**。ここは reject_reason / improve_hint を
        読む唯一の読み手で、これを痩せさせると生成役は死因を知らずに同型再提案を
        繰り返す (immich postgres 更新系 7 度の再来)。

        CR が読めなければ例外を上げる。**空のファイルを置いて Job を走らせない** —
        過去案が 0 件に見える立案は「既出と同型を避ける」判断ができず、質の落ちた
        案を採択まで通してしまう。呼び出し側は spawn を見送る (fail-closed)。
        """
        items = self.k8s_client().list_custom(
            projectcr.API_VERSION, self.cfg.namespace, projectcr.PLURAL
        )
        rows = projectcr.proposal_digest(items)
        path = self.curriculum_dir() / "proposals.jsonl"
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(path)  # 書きかけを Job に読ませない
        log(f"curriculum input: 過去案 {len(rows)} 件を {path} に書いた")
        return {"PROPOSALS_HISTORY": str(path)}

    # --- critic (日次の自己観測) の入出力 ---
    def critic_dir(self):
        d = self.cfg.data_dir / "critic"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def recent_transcripts(self, limit=3):
        """直近に書かれた transcript のパス (新しい順)。critic が精読する対象。"""
        entries = []
        if self.transcripts.is_dir():
            for path in self.transcripts.rglob("*.jsonl"):
                try:
                    entries.append((path.stat().st_mtime, str(path)))
                except OSError:
                    continue
        entries.sort(reverse=True)
        return [p for _, p in entries[:limit]]

    def prepare_critic_input(self, sf, doc, now):
        """指標を集計して /data/critic/input-<日付>.json に落とし、Job に渡す env を返す。

        「候補区間の特定は指標側 (heart) がやり、critic は絞られた対象だけを読む」
        — ops/prompts/critic.md が冒頭で宣言している分業。ここで絞らないと critic は
        2000 行超の metrics.jsonl を生読みすることになる (生読みは上位モデルでも低精度)。
        """
        day = now_iso(now)[:10]
        summary = metrics.summarize_beats(
            self.metrics_store.read_jsonl("metrics.jsonl"), now
        )
        summary["stalled"] = metrics.summarize_stalled(doc)
        summary["targets"] = self.recent_transcripts()
        path = self.critic_dir() / f"input-{day}.json"
        with open(path, "w") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return {
            "CRITIC_INPUT": str(path),
            "CRITIC_TARGETS": ",".join(summary["targets"]),
            "CRITIC_OUTPUT": str(self.critic_dir() / f"{day}.md"),
        }

    def critic_summary(self, limit=1200):
        """最新の所見ファイルの冒頭。Discord は 1900 字で切るので要点だけ載せる。

        今日の日付で決め打たず更新時刻で選ぶ: critic Job は UTC の日付をまたいで
        終わることがあり、決め打つと静かに取り逃す。
        """
        files = []
        for path in self.critic_dir().glob("*.md"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
        if not files:
            return ""
        path = max(files)[1]
        text = path.read_text(errors="replace").strip()
        if not text:
            return ""
        return f"{path.name}\n{text[:limit]}" + ("…" if len(text) > limit else "")

    # --- beat ---
    def beat(self, i):
        now = datetime.now(timezone.utc)
        gitutil.sync_main(self.repo_dir, self.repo_url)
        # rules/models は main から毎ビート読み直す (PR で変えたものが再起動なしで効く)
        self.cfg = config.load(self.repo_dir)

        self.migrate_work_files()
        # Notifier の outbox / sent は PVC 側 (設計 state-out-of-git Phase 3)
        notifier = Notifier(
            self.cfg.discord_webhook, self.work, self.cfg.rules, self.gh,
            self.cfg.feedback_issue,
        )
        self.migrate_projects_doc(notifier, now)
        sf = self.doc_store()

        doc = self.load_doc()
        cursors = self.work.load_cursors()

        # --- 観測。失敗した項目は None (「無い」と区別する。decide が保守的に扱う) ---
        try:
            unhealthy_apps, health_fresh, health_doc = facts.load_health(
                self.k8s_client(), self.cfg.namespace, self.cfg.health_configmap
            )
        except Exception as e:
            # クライアントすら作れない (Pod の外・トークン欠落) ときもビートは続ける。
            # 観測不能は「健全」ではないので health_fresh=False のまま decide に渡る
            log(f"health report の読み取りに失敗 (観測不能として扱う): {e}")
            unhealthy_apps, health_fresh, health_doc = None, False, None
        # B2 download cap の帳簿の警報すべき状態 (P-0128)。warn/exceed のときだけ
        # 中身があり、それ以外は None (budget_alert_due が繰り返しを落とす)
        budget = facts.budget_alert(health_doc)
        # Mission Control 描画スモークの警報すべき状態 (P-0193)。fail/stale のとき
        # だけ中身があり、ok/no_data は None (成功は通知予算を消費しない)
        smoke = facts.dashboard_smoke_alert(health_doc)
        # CPU 飽和前兆の警報すべき状態 (P-9037)。warn のときだけ中身があり、
        # ok/観測失敗は None (P-0128 の budget 警告と同じ 2 段階)
        saturation = facts.node_saturation_alert(health_doc)
        # 書き置きのバス inbox が読めるか (設計 state-out-of-git Phase 6)。
        # ops-feedback を畳んだので、ここが読めないと所有者の「止めて」を
        # 落としたまま走り続ける。読めるときは None
        bus_fault = facts.feedback_bus_alert(self.cfg.feedback_bus_dir)
        try:
            jobs = facts.collect_jobs(self.k8s_client(), self.cfg.namespace)
        except Exception as e:
            log(f"job collection failed (観測不能として扱う): {e}")
            jobs = None
        results, reviews = facts.collect_results(self.cfg.data_dir)
        branches = [
            p["branch"]
            for p in doc["projects"]
            if p["state"] in ("active", "in_review", "merging")
        ]
        try:
            open_prs, merged_prs = facts.collect_prs(self.gh, branches)
        except Exception as e:
            log(f"PR collection failed: {e}")
            open_prs, merged_prs = {}, {}
        vetoes, acks, stop_all, review_needed, resume_all, task_requests, approves, cursors = (
            facts.collect_feedback(
                self.gh, cursors, self.cfg.rules,
                self.cfg.feedback_issue, self.cfg.feedback_bus_dir,
            )
        )
        if vetoes or acks or approves or stop_all or review_needed or resume_all or task_requests:
            # kill switch の受信は必ず可視化する (該当プロジェクトが無く action が
            # 生まれない場合でも、veto 疎通試験の結果を外から確認できるように)
            log(
                f"feedback received: vetoes={vetoes} acks={acks} approves={approves} stop_all={stop_all} "
                f"resume_all={resume_all} review_needed={len(review_needed)} "
                f"task_requests={len(task_requests)}"
            )
        # 常駐コア発の command (設計 D3/D21)。処理済み台帳を facts として渡し、
        # 二重実行の判断は decide (純関数) に置く
        commands = facts.collect_commands(self.cfg.command_bus_dir)
        processed_commands = sorted(
            tasks.ledger_ids(self.work.read_jsonl(tasks.COMMAND_LEDGER_FILE))
        )
        if commands:
            log(f"commands on bus: {len(commands)} "
                f"(processed={len(processed_commands)})")
        dispatches = facts.collect_dispatches(self.cfg.data_dir)
        if dispatches:
            log(f"dispatches to fold: {[d.get('project_id') for d in dispatches]}")
        # 採択済み案の spec は Project CR から読む (設計 state-out-of-git 4b-2a)。
        # **読めないビートは空で進める** — この facts の使い道は「まだ doc に
        # 居ない採択を登録する」ことだけなので、空 = このビートは何も登録しない
        # で済み、次のビートが同じ観測をやり直す。逆に git 側へ落とすと、
        # 読み手を CR に切り替えた意味が壊れたときだけ消える隠れ経路になる
        try:
            adopted_specs_by_id = facts.load_adopted_specs(
                self.k8s_client(), self.cfg.namespace
            )
        except Exception as e:  # noqa: BLE001 — 観測失敗。判断はしない
            log(f"Project CR を読めない (このビートの採択登録は見送る): {e}")
            adopted_specs_by_id = {}
        curriculum = facts.collect_curriculum(self.cfg.data_dir)
        critic = facts.collect_critic(self.cfg.data_dir)
        adopted_specs = list(adopted_specs_by_id.values())
        usage_info = metrics.daily_usage(self.transcripts, now)

        running = sum(
            1 for p in doc["projects"] if p["state"] in ("active", "in_review", "merging")
        )
        f = {
            "jobs": jobs,
            "results": results,
            "reviews": reviews,
            "open_prs": open_prs,
            "merged_prs": merged_prs,
            "unhealthy_apps": unhealthy_apps,
            "health_green": unhealthy_apps == [],
            "health_fresh": health_fresh,
            "vetoes": vetoes,
            "acks": acks,
            "approves": approves,
            "stop_all": stop_all,
            "resume_all": resume_all,
            "running_runners": running,
            "curriculum": curriculum,
            "critic": critic,
            "adopted_specs": adopted_specs,
            "commands": commands,
            "processed_commands": processed_commands,
            # 即時 dispatch (設計 rev3 Phase D) の結末。gate スレッドが
            # 採択ゲートと Job 作成まで済ませたレコードが inbox に落ちている
            "dispatches": dispatches,
        }
        doc, actions = reconcile.decide(doc, f, self.cfg.rules, now)

        # B2 download cap の警報 (P-0128)。既存の流路 2 本に乗せる:
        # briefing-queue.jsonl (review_needed と同じ位置) と incident 通知 (下段)。
        # 鳴るのは budget.status が warn/exceed のときだけで、同じ status の同一日内
        # の再通知は cursors の前回記録で落とす (ビートは 120s で回るため)。
        # cursors への書き込みは下の save_cursors(cursors) より **前** に行う。
        # StateFiles._save_json は即時 json.dump のため、save の後から dict へ入れても
        # cursors.json には反映されず、次のビートの load_cursors() に前回記録が無くて
        # 警報を積み直す (レビュー指摘。collect_feedback が new_cursors を返してから
        # save する既存パターンと同じ順序)
        today = now.date().isoformat()
        budget_incident_text = None
        budget_queued = False
        if facts.budget_alert_due(budget, cursors.get("download_budget_alert"), today):
            # budget_alert_due が True を返すのは alert が実在するときだけ
            cursors["download_budget_alert"] = {
                "status": budget["status"],
                "date": today,
            }
            budget_incident_text = (
                f"B2 download cap の帳簿が {budget['status']} です: {budget['reason']}"
            )
            budget_queued = True
        # 描画スモークの警報 (P-0193)。流儀は上の budget 警報と同じ:
        # briefing-queue.jsonl への追記と incident 通知。同じ status の同一日内の
        # 再通知は cursors の前回記録で落とす。budget_alert_due は status/date の
        # 一般判定なので budget と流用する (名前は budget だが中身は汎用)
        smoke_incident_text = None
        smoke_queued = False
        if facts.budget_alert_due(smoke, cursors.get("dashboard_smoke_alert"), today):
            cursors["dashboard_smoke_alert"] = {
                "status": smoke["status"],
                "date": today,
            }
            smoke_incident_text = (
                f"Mission Control の描画断言が {smoke['status']} です: {smoke['reason']}"
            )
            smoke_queued = True
        bus_incident_text = None
        bus_queued = False
        if facts.budget_alert_due(bus_fault, cursors.get("feedback_bus_alert"), today):
            cursors["feedback_bus_alert"] = {
                "status": bus_fault["status"],
                "date": today,
            }
            bus_incident_text = f"書き置きのバス経路が読めません: {bus_fault['reason']}"
            bus_queued = True

        # CPU 飽和前兆の警報 (P-9037)。流儀は上の budget / smoke 警報と同じ:
        # briefing-queue.jsonl への追記と incident 通知。同じ status の同一日内の
        # 再通知は cursors の前回記録で落とす。budget_alert_due は status/date の
        # 一般判定なので流用する
        saturation_incident_text = None
        saturation_queued = False
        if facts.budget_alert_due(saturation, cursors.get("node_saturation_alert"), today):
            cursors["node_saturation_alert"] = {
                "status": saturation["status"],
                "date": today,
            }
            saturation_incident_text = (
                f"CPU 飽和前兆 (node01): {saturation['reason']}"
            )
            saturation_queued = True

        # --- 一段目: 状態遷移を副作用より先に永続化する (レビュー指摘 [8])。
        # ここで落ちても副作用は未実行なので、次のビートが同じ判断をやり直すだけ。
        # 逆順 (実行→保存) だと、保存失敗の翌ビートが「実行済みの副作用」を知らずに
        # 二重実行する
        sf.save_projects(doc)
        self.work.save_cursors(cursors)

        for item in review_needed:
            self.work.append_jsonl("briefing-queue.jsonl", {"at": now_iso(now), **item})
        if budget_queued:
            self.work.append_jsonl(
                "briefing-queue.jsonl",
                {
                    "at": now_iso(now),
                    "source": f"download-budget ({budget['status']})",
                    "body": budget["reason"],
                },
            )
            log(f"download_budget alert: {budget['status']} — queued to briefing")
        if smoke_queued:
            self.work.append_jsonl(
                "briefing-queue.jsonl",
                {
                    "at": now_iso(now),
                    "source": f"dashboard-smoke ({smoke['status']})",
                    "body": smoke["reason"],
                },
            )
            log(f"dashboard_smoke alert: {smoke['status']} — queued to briefing")
        if saturation_queued:
            self.work.append_jsonl(
                "briefing-queue.jsonl",
                {
                    "at": now_iso(now),
                    "source": f"node-saturation ({saturation['status']})",
                    "body": saturation["reason"],
                },
            )
            log(f"node_saturation alert: {saturation['status']} — queued to briefing")
        if bus_queued:
            self.work.append_jsonl(
                "briefing-queue.jsonl",
                {
                    "at": now_iso(now),
                    "source": f"feedback-bus ({bus_fault['status']})",
                    "body": bus_fault["reason"],
                },
            )
            log(f"feedback_bus alert: {bus_fault['status']} — queued to briefing")
        # タスク依頼の受領 (P-0091)。id 重複は merge_new が落とすので、
        # カーソル巻き戻り等で同じ note を再取り込みしても積み直さない
        queue = self.work.read_jsonl(tasks.QUEUE_FILE)
        merged = tasks.merge_new(queue, task_requests, now)
        if len(merged) != len(queue):
            self.work.rewrite_jsonl(tasks.QUEUE_FILE, merged)
            log(f"task requests queued: total={len(merged)}")
        # 棄却案は curriculum の結果から台帳 (PVC) へ移す。**consume より先**に
        # 写しを取らないと、result.json が退避された時点で死因が消える
        self.record_rejected(f.get("curriculum"))

        # --- 二段目: 副作用の実行と、その結果 (job 名等) の永続化 ---
        self.execute(actions, doc, sf, notifier, now)
        if budget_incident_text:
            if self.cfg.shadow:
                log(f"[shadow] notify[incident] {budget_incident_text[:80]}")
            else:
                notifier.send("incident", budget_incident_text, now)
        if smoke_incident_text:
            if self.cfg.shadow:
                log(f"[shadow] notify[incident] {smoke_incident_text[:80]}")
            else:
                notifier.send("incident", smoke_incident_text, now)
        if saturation_incident_text:
            if self.cfg.shadow:
                log(f"[shadow] notify[incident] {saturation_incident_text[:80]}")
            else:
                notifier.send("incident", saturation_incident_text, now)
        if bus_incident_text:
            if self.cfg.shadow:
                log(f"[shadow] notify[incident] {bus_incident_text[:80]}")
            else:
                notifier.send("incident", bus_incident_text, now)

        record = metrics.beat_record(
            now,
            i,
            doc,
            jobs=len(jobs) if jobs is not None else None,
            open_prs=len(open_prs),
            unhealthy_apps=unhealthy_apps,
            health_fresh=health_fresh,
            usage=usage_info,
            budget_status=budget["status"] if budget else None,
            dashboard_smoke_status=smoke["status"] if smoke else None,
            node_saturation_status=saturation["status"] if saturation else None,
            vetoes=vetoes,
            stop_all=stop_all,
            actions=[a["type"] for a in actions],
            shadow=self.cfg.shadow,
        )
        self.metrics_store.append_jsonl("metrics.jsonl", record)
        self.prune_metrics(now)
        sf.save_projects(doc)
        # プロジェクトの正 (設計 state-out-of-git 4b-2b)。git 側の写しは凍結され、
        # 外から見えるのはここだけになった。**書けなければ記録が止まる**ので、
        # 失敗が続けば note_cr_failures が人間を叩く
        try:
            self.sync_project_crs(doc, notifier, now)
        except Exception as e:  # noqa: BLE001 — 1 ビートの失敗ではビートを止めない
            log(f"project CR sync failed: {e}")
            self.note_cr_failures(1, notifier, now)
        # usage はダッシュボードが gate の /healthz から読む (4b-2b)。
        # heartbeat.json は PVC に残る — livenessProbe (ops/heart/liveness.py) が
        # 「止まったまま死んだ」を検知する唯一の材料だから
        self.docs.write_heartbeat(i, now, usage=usage_info)
        # 生存はクラスタの中の Lease でも示す (設計 state-out-of-git Phase 7)。
        # **ここに置くこと自体が仕様**で、ビートが最後まで通ったときにしか
        # renewTime は進まない。プロセスが生きていても止まっていれば古くなる
        self.renew_lease(i, now)
        # admission gate に判定材料の写しを渡す (設計 rev3 Phase D)。
        # この写しの鮮度そのものが安全装置で、ビートが詰まればゲートは自動的に
        # 閉じる (reconcile.DISPATCH_SNAPSHOT_MAX_AGE_SECONDS)
        if self.gate is not None:
            self.gate.update(doc, self.cfg.rules, now, self.cfg.shadow, usage=usage_info)
        if not self.cfg.shadow:
            notifier.flush_outbox(now)
        self.prune_audit(now)
        removed = metrics.rotate_transcripts(self.transcripts, self.cfg.rules, now)
        if removed:
            log(f"rotated {removed} old transcript files")

    # --- doc の置き場 (設計 state-out-of-git 4b-2b) ---

    def doc_store(self):
        """projects.json を読み書きする StateFiles。

        移行が済むまでは ops-state の checkout 側を使い続ける。**移行が止まった
        ビートで PVC の空 doc に切り替えない** — 切り替えた瞬間に全プロジェクトを
        忘れ、次の save が「1 件も無い」を正として書き出すことになる。
        """
        if (self.doc_dir / "projects.json").exists():
            return self.docs
        if (self.state_dir / "projects.json").exists():
            return StateFiles(self.state_dir)
        return self.docs

    def load_doc(self):
        """プロジェクトの doc を読む。読み先は PVC → 移行前の checkout → CR。

        最後の CR からの復元は **PVC ごと失われた後の唯一の道**。git 側の写しが
        凍結された今、ここが空の doc を返すと器は全プロジェクトを忘れる。
        CR が読めないときは例外を上げて次のビートに任せる (空で走り出さない)。
        トップレベルの値 (stop_engaged / last_curriculum_at など) は CR に載らない
        ので復元されない — 失うのは「止めて」の保持だけで、記録ではない。
        """
        store = self.doc_store()
        if (store.dir / "projects.json").exists():
            return store.load_projects()
        items = self.k8s_client().list_custom(
            projectcr.API_VERSION, self.cfg.namespace, projectcr.PLURAL,
            label_selector=projectcr.NOT_REJECTED_SELECTOR,
        )
        projects = projectcr.projects_from_items(items)
        log(f"projects.json が無い。Project CR から {len(projects)} 件を復元する")
        return {"version": 1, "projects": projects, "chores": []}

    def cr_gap(self, doc):
        """git 側が知っている id のうち、まだ CR になっていないものを返す。

        突き合わせるのは 2 つ: 移行前の projects.json のプロジェクトと、台帳
        (archive.jsonl) の全行。**件数ではなく id で見る** — 数が合っていても
        中身がずれていたら取りこぼしは残る。
        """
        items = self.k8s_client().list_custom(
            projectcr.API_VERSION, self.cfg.namespace, projectcr.PLURAL
        )
        have = {
            (item.get("metadata") or {}).get("name") for item in items
        }
        want = {projectcr.cr_name(p["id"]) for p in doc.get("projects", []) if p.get("id")}
        want |= {
            projectcr.cr_name(rec["id"])
            for rec in facts.load_archive_records(self.repo_dir)
            if rec.get("id")
        }
        return sorted(want - have)

    def migrate_projects_doc(self, notifier=None, now=None):
        """projects.json を ops-state の checkout から PVC へ移す (4b-2b)。

        **CR が git 側の全 id を持っていることを確かめるまで移さない。** ここが
        この段の唯一の安全装置で、取りこぼしたまま git を離れると、CR にならな
        かったプロジェクトは restic のバックアップにも乗らないまま静かに消える。
        揃っていなければ移行を見送り、人間に言う — 見送っている間も heart は
        従来どおり checkout 側の doc で回り (push はしない)、毎ビートの
        sync_project_crs / plan_rejected が穴を埋めるので自力で収束する。

        コピーしてから消す (作業ファイルの移行と同じ規律)。消すのは metrics.jsonl
        だけで、projects.json は checkout 側にも残す — 戻す判断が要るときの
        最後の写しになる (もう push されないので git 側は凍結されたまま)。
        """
        src = self.state_dir / "projects.json"
        if (self.doc_dir / "projects.json").exists() or not src.exists():
            return
        with open(src) as f:
            doc = json.load(f)
        try:
            missing = self.cr_gap(doc)
        except Exception as e:  # noqa: BLE001 — 読めないことを「揃っている」に倒さない
            log(f"CR の突き合わせに失敗 (移行を見送る): {e}")
            missing = ["(CR を読めなかった)"]
        if missing:
            self.note_parity_gap(missing, notifier, now)
            return
        self.parity_gap_streak = 0
        self.doc_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, self.doc_dir / "projects.json")
        # heartbeat.json は写さない。毎ビート書き直すもので、古い写しを上書きで
        # 置くと livenessProbe が一瞬 stale を見る (再起動の空振りになる)
        # 経過措置で最新 1 行だけ置いていた指標 (設計 Phase 1)。読み手はもう居ない
        stale_metrics = self.state_dir / "metrics.jsonl"
        if stale_metrics.exists():
            stale_metrics.unlink()
        log(f"projects.json を PVC へ移した ({len(doc.get('projects', []))} 件)")

    def note_parity_gap(self, missing, notifier=None, now=None):
        """CR の取りこぼしを人間に届ける。毎ビート鳴らさず、続いていることは残す。"""
        self.parity_gap_streak += 1
        log(
            f"Project CR に {len(missing)} 件の取りこぼしがある。git 側の doc を"
            f"使い続ける: {', '.join(missing[:PARITY_ALERT_IDS])}"
        )
        if self.parity_gap_streak % PARITY_ALERT_BEATS != 1:
            return
        text = (
            f"Project CR に git 側の記録が {len(missing)} 件足りません。"
            "揃うまで projects.json の移行 (state-out-of-git 4b-2b) を見送っています: "
            + ", ".join(missing[:PARITY_ALERT_IDS])
        )
        if notifier is None or self.cfg.shadow:
            log(f"[cr-parity] {text}")
            return
        notifier.send("incident", text, now)

    def record_rejected(self, curriculum):
        """curriculum が落とした案を PVC の台帳へ写す (設計 4b-2b)。

        git の archive.jsonl への追記を止めた代わりの置き場。ここに落ちた行を
        次のビートの plan_rejected が Project CR にする。**CR にするまで消さない**
        ので、CR の書き込みが失敗しても死因 (reject_reason / improve_hint) は残る。
        """
        records = (curriculum or {}).get("records") or []
        rejected = [
            r for r in records
            if isinstance(r, dict) and r.get("id") and not r.get("adopted")
        ]
        if not rejected:
            return
        have = {
            r.get("id") for r in self.work.read_jsonl(REJECTED_LEDGER_FILE)
        }
        added = 0
        for rec in rejected:
            if rec["id"] in have:
                continue
            self.work.append_jsonl(REJECTED_LEDGER_FILE, rec)
            have.add(rec["id"])
            added += 1
        if added:
            log(f"棄却案 {added} 件を PVC の台帳に記録した")

    def prune_rejected_ledger(self, existing):
        """CR になった棄却案を PVC の台帳から落とす。

        **CR が実在することを確かめた行だけ**を消す (このビートの list_custom の
        結果で見る)。同じビートで apply したばかりの行は残す — 次のビートで
        存在が確認できてから消せばよく、急いで消して得るものが無い。
        こうしないと台帳は毎ビート読む一方で伸び続ける。
        """
        have = {(item.get("metadata") or {}).get("name") for item in existing}
        records = self.work.read_jsonl(REJECTED_LEDGER_FILE)
        kept = [
            r for r in records
            if not (r.get("id") and projectcr.cr_name(r["id"]) in have)
        ]
        if len(kept) != len(records):
            self.work.rewrite_jsonl(REJECTED_LEDGER_FILE, kept)
            log(f"棄却案 {len(records) - len(kept)} 件は CR になったので台帳から落とした")

    def rejected_records(self):
        """棄却案の全部。凍結された git の台帳 + PVC に積んだ新しい行。

        git 側を読み続けるのは、**過去 277 件の死因が今もそこにしか無い**から。
        書き込みは止めたので中身は変わらず、CR への取り込みが済めば差分は 0 になる。
        """
        return facts.load_archive_records(self.repo_dir) + self.work.read_jsonl(
            REJECTED_LEDGER_FILE
        )

    def renew_lease(self, beat, now):
        """生存の Lease を更新する (設計 state-out-of-git Phase 7)。

        読み手はコアで、古ければ Telegram で人間に言う。**失敗してもビートは
        落とさない** — 書けないなら Lease は自然に古くなり、沈黙として検知される
        (fail-closed)。ここで例外を上げると、検知の道具が本体を殺すことになる。
        """
        body = lease.to_lease(
            self.cfg.namespace,
            holder=f"heart/{os.environ.get('HOSTNAME', 'unknown')}",
            beat=beat,
            now=now,
            stale_seconds=self.cfg.rules["heartbeat"]["stale_seconds"],
        )
        try:
            self.k8s_client().apply_lease(self.cfg.namespace, lease.NAME, body)
        except Exception as e:  # noqa: BLE001 — 書けないことは沈黙として現れる
            log(f"lease renew failed: {e}")

    def sync_project_crs(self, doc, notifier=None, now=None):
        """doc のプロジェクトと棄却案を Project CR に書く。

        4b-2b で **CR が正になった**。git 側の写しは凍結されたので、ここに書け
        なかったものは restic のバックアップにも乗らない。だから失敗が続けば
        note_cr_failures が人間を叩く (1 回の失敗は API の瞬断でよくある)。
        1 ビートの失敗ではビートを止めない — 次のビートが同じ CR を送り直す。

        変わった CR だけを送る。消えたプロジェクトは消さない (projectcr.plan)。

        棄却案の入り口を **1 回きりの移行スクリプトにしていない**理由: 器の外に
        kubectl を持った人間が居ない前提で回っており、手で流す前提の経路は
        「誰も流さないまま次に進む」で終わる。毎ビート台帳を突き合わせれば
        取り込みは自力で収束し、restic からの復元後もひとりでに埋め直る。
        """
        k8s = self.k8s_client()
        existing = k8s.list_custom(
            projectcr.API_VERSION, self.cfg.namespace, projectcr.PLURAL
        )
        write, orphans = projectcr.plan(
            doc, existing, self.cfg.namespace, TERMINAL_STATES
        )
        # 棄却案は doc に居ない (居させない) ので、台帳から直接引く。
        # 1 ビートの取り込み件数は REJECTED_BATCH_LIMIT で抑える
        rejected = projectcr.plan_rejected(
            self.rejected_records(),
            existing,
            self.cfg.namespace,
            {p["id"] for p in doc.get("projects", [])},
        )
        # CR になった行は PVC の台帳から落とす (毎ビート読むので伸ばさない)
        self.prune_rejected_ledger(existing)
        failed = 0
        for cr in write + rejected:
            try:
                k8s.apply_custom(
                    projectcr.API_VERSION, self.cfg.namespace, projectcr.PLURAL,
                    cr["metadata"]["name"], cr,
                )
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で残りを諦めない
                failed += 1
                log(f"project CR apply failed: {cr['metadata']['name']}: {e}")
        if write or rejected or orphans:
            log(
                f"project CR sync: applied={len(write) + len(rejected) - failed} "
                f"failed={failed} rejected={len(rejected)} "
                f"orphans={','.join(orphans) if orphans else '-'}"
            )
        self.note_cr_failures(failed, notifier, now)

    def note_cr_failures(self, failed, notifier=None, now=None):
        """CR の書き込み失敗が続いていることを人間に届ける。

        1 回の失敗は API の瞬断でよくある。**続く**失敗はスキーマの穴で、
        2026-08-24 に `.spec.spec.budget` が未宣言だったために P-0353 の CR が
        毎ビート 500 で拒否され続けた (誰も鳴らさなかったので気づくのに丸一日
        かかった)。CR が正になる 4b-2 の後、これは静かなデータ欠損そのものになる。

        指標に載せずに通知にしたのは、metrics.jsonl が PVC 内で読み手が居らず、
        「載せた」が「届く」にならないため。連続 CR_FAIL_ALERT_BEATS ビートごとに
        1 回だけ鳴らす (毎ビート鳴らすと通知が壊れた側になる)。
        数えるのはプロセス内だけで、再起動で 0 に戻る — 失敗が続いていれば
        すぐまた積み上がるので、状態として持ち出す価値が無い。
        """
        if failed:
            self.cr_fail_streak += 1
        else:
            self.cr_fail_streak = 0
            return
        if self.cr_fail_streak % CR_FAIL_ALERT_BEATS:
            return
        text = (
            f"Project CR の書き込みが {self.cr_fail_streak} ビート連続で失敗しています。"
            "CRD のスキーマに未宣言のフィールドがあると server-side apply は 500 で拒否します "
            "(apps/autopilot/crd-project.yaml)。heart のログに失敗した CR 名が出ています"
        )
        if notifier is None:
            log(f"[cr-alert] {text}")
            return
        if self.cfg.shadow:
            log(f"[shadow] notify[incident] {text[:80]}")
        else:
            notifier.send("incident", text, now)

    def prune_metrics(self, now):
        """保持窓より古い指標行を落とす。行数が変わったときだけ書き直す。"""
        records = self.metrics_store.read_jsonl("metrics.jsonl")
        kept = metrics.prune_beats(records, now)
        if len(kept) != len(records):
            self.metrics_store.rewrite_jsonl("metrics.jsonl", kept)
            log(f"pruned {len(records) - len(kept)} old metric beats")

    def migrate_work_files(self):
        """ops-state に残っている作業ファイルを PVC へ移す (設計 Phase 3)。

        **コピーしてから消す**。逆順だと途中で落ちたときに未送信の通知や
        受理済みの依頼をまとめて失う。PVC 側に既にあるものは移行済みなので
        触らない = 何度呼んでも同じ (移行が済めばこのメソッドは何もしない)。

        4b-2b 以降、消すのは PVC 上の checkout からだけ (git へは反映されない)。
        """
        copy, remove = migrate_plan(_names(self.state_dir), _names(self.work_dir))
        if not remove:
            return
        self.work_dir.mkdir(parents=True, exist_ok=True)
        for name in copy:
            shutil.copyfile(self.state_dir / name, self.work_dir / name)
        for name in remove:
            (self.state_dir / name).unlink()
        log(f"作業ファイルを PVC へ移した: copied={copy} removed={remove}")

    def prune_audit(self, now):
        """保持窓より古い監査行を落とす。行数が変わったときだけ書き直す。"""
        records = self.work.read_jsonl("audit.jsonl")
        kept = metrics.prune_audit(records, now)
        if len(kept) != len(records):
            self.work.rewrite_jsonl("audit.jsonl", kept)
            log(f"pruned {len(records) - len(kept)} old audit lines")

    def self_update_check(self):
        # ops/heart だけでなく rules.json / models.json も監視する。config は起動時に
        # しか読まないため、これらが main で変わったら exec し直して読み直す必要がある
        # (2026-08-22 発覚: max_concurrent の変更が pod 再作成を伴わないと反映されなかった。
        # それまでは image digest 変更などによる pod 再作成が偶然重なって効いていた)
        tree = " ".join(
            gitutil.run(["rev-parse", f"origin/main:{p}"], cwd=self.repo_dir, check=False)
            or ""
            for p in ("ops/heart", "ops/rules.json", "ops/models.json")
        )
        if self.start_tree is None:
            self.start_tree = tree
        elif tree and tree != self.start_tree:
            log(f"ops/heart が更新された ({self.start_tree[:12]} -> {tree[:12]})。exec し直す")
            os.chdir(self.repo_dir)
            os.execv(sys.executable, [sys.executable, "-m", "ops.heart.heart"])

    def start_gate(self):
        """admission gate (設計 rev3 Phase D) を起こす。

        起きなくても heart は従来どおり回る — コアの request_task (バス経由の起票)
        が冷スペアとして残っているので、ここで例外を出してビートを止めない。
        """
        listen = os.environ.get("HEART_GATE_LISTEN", gate.DEFAULT_LISTEN)
        if not listen:
            log("admission gate は無効 (HEART_GATE_LISTEN が空)")
            return
        try:
            self.gate = gate.AdmissionGate(
                cfg_provider=lambda: self.cfg,
                k8s_provider=self.k8s_client,
                data_dir=self.cfg.data_dir,
                repo_url=self.repo_url,
            )
            self.gate.start(listen)
            log(f"admission gate listening on {listen} (POST /dispatch)")
        except Exception as e:
            self.gate = None
            log(f"admission gate を起こせなかった (heart は続行する): {e}")

    def run(self):
        signal.signal(signal.SIGTERM, _sigterm)
        signal.signal(signal.SIGINT, _sigterm)
        self.start_gate()
        log(
            f"heart started (mode={self.cfg.mode} beat={self.cfg.beat_seconds}s "
            f"repo={self.repo_url})"
        )
        i = 0
        while not _stop:
            i += 1
            started = time.time()
            log(f"iteration #{i} start")
            rc = 0
            try:
                self.beat(i)
            except Exception:
                rc = 1
                traceback.print_exc()
            elapsed = int(time.time() - started)
            log(f"iteration #{i} end exit={rc} elapsed={elapsed}s")
            self.self_update_check()
            # SIGTERM に即応するため小刻みに待つ
            deadline = time.time() + self.cfg.beat_seconds
            while not _stop and time.time() < deadline:
                time.sleep(1)
        log("heart stopped (SIGTERM)")


def main():
    repo_dir = os.environ.get("REPO_DIR", os.getcwd())
    Heart(repo_dir).run()


if __name__ == "__main__":
    main()
