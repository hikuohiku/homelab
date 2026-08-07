"""heart — 決定論 reconcile ループの本体。

起動: リポジトリ checkout の直下で `python3 -m ops.heart.heart`
(apps/autopilot/ の bootstrap ConfigMap が clone → exec する)。

1 ビート (既定 120s):
  1. main を最新化・ops-state を最新化
  2. 事実収集 (health / Job / PVC の result / PR / フィードバック 2 経路)
  3. reconcile.decide() — 判断はすべて純関数側
  4. actions 実行 (shadow モードでは記録のみ)
  5. 指標追記・heartbeat・ops-state push

heartbeat は旧 loop.sh と同じ書式で stdout に出す。ops-health-reporter の
HEARTBEAT_RE (report.py) がこれを拾って自己ハング検知に使うため、
書式を変えるときは report.py と CHARTER §2 を同時に変えること。
"""

import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

from . import config, facts, gitutil, metrics, reconcile, spawn
from .gh import Gh
from .notify import Notifier, veto_footer
from .statefiles import StateFiles, now_iso

_stop = False


def _sigterm(_sig, _frame):
    global _stop
    _stop = True


def log(msg):
    print(f"[autopilot] {now_iso()} {msg}", flush=True)


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
        self.state_dir = self.cfg.data_dir / "ops-state"
        self.transcripts = self.cfg.data_dir / "transcripts"
        self.gh = Gh(self.cfg.github_token, self.cfg.repo)
        self.k8s = None  # 遅延初期化 (単体テスト・クラスタ外実行のため)
        self.start_tree = None

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
            audit = {"at": now_iso(now), "action": kind, "project": pid, "shadow": shadow}
            try:
                if kind == "announce":
                    if shadow:
                        log(f"[shadow] announce {pid}")
                    else:
                        notifier.send("announce", announce_text(p), now)
                elif kind == "spawn_runner":
                    p["spawn_count"] = p.get("spawn_count", 0) + 1
                    if shadow:
                        log(f"[shadow] spawn runner for {pid}")
                    else:
                        extra = {}
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
                        )
                elif kind == "spawn_curriculum":
                    if shadow:
                        log("[shadow] spawn curriculum")
                    else:
                        # attempt に分単位の時刻を入れて Job 名を一意にする。固定名だと
                        # 前回分が TTL (6h) 内に残っている間 409 を成功扱いして
                        # 黙って空振りする (レビュー指摘 [20])
                        spawn.create(
                            self.k8s_client(), self.cfg, "curriculum",
                            attempt=int(time.time()) // 60 % 1000000,
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
                        self.gh.merge_pr(a["pr"])
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
                elif kind in ("consume_result", "consume_review", "consume_curriculum"):
                    # 消費した事実ファイルを退避する。残すと次のビートが同じ事実を
                    # 再消費して状態機械が発振する (レビュー指摘 [0])
                    name = {
                        "consume_result": "result.json",
                        "consume_review": "review.json",
                        "consume_curriculum": "result.json",
                    }[kind]
                    target = "system" if kind == "consume_curriculum" else pid
                    if shadow:
                        log(f"[shadow] consume {target}/{name}")
                    else:
                        self.consume_file(target, name, now)
                elif kind == "record_drift":
                    audit["reason"] = a.get("reason")
            except Exception as e:
                audit["error"] = str(e)[:300]
                log(f"action {kind} for {pid} failed: {e}")
            sf.append_jsonl("audit.jsonl", audit)

    def consume_file(self, project_id, name, now):
        """消費済みの result/review を processed/ へ移す (削除でなく退避 — 監査用)。"""
        src = self.cfg.data_dir / "projects" / project_id / name
        if not src.exists():
            return
        dst = src.parent / "processed" / f"{now_iso(now).replace(':', '')}-{name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    # --- beat ---
    def beat(self, i):
        now = datetime.now(timezone.utc)
        gitutil.sync_main(self.repo_dir, self.repo_url)
        # rules/models は main から毎ビート読み直す (PR で変えたものが再起動なしで効く)
        self.cfg = config.load(self.repo_dir)

        self.gh.ensure_branch(self.cfg.state_branch)
        gitutil.sync_state_branch(self.state_dir, self.repo_url, self.cfg.state_branch)
        sf = StateFiles(self.state_dir)
        notifier = Notifier(
            self.cfg.discord_webhook, sf, self.cfg.rules, self.gh, self.cfg.feedback_issue
        )

        doc = sf.load_projects()
        cursors = sf.load_cursors()

        # --- 観測。失敗した項目は None (「無い」と区別する。decide が保守的に扱う) ---
        unhealthy_apps, health_fresh, _ = facts.load_health(
            self.repo_dir, self.cfg.health_branch
        )
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
        vetoes, stop_all, review_needed, cursors = facts.collect_feedback(
            self.gh, self.repo_dir, cursors, self.cfg.rules,
            self.cfg.feedback_issue, self.cfg.feedback_branch,
        )
        curriculum = facts.collect_curriculum(
            self.cfg.data_dir, self.repo_dir, self.gh
        )
        tripped, breaker_info = metrics.breaker_tripped(
            sf, self.cfg.rules, self.transcripts, now
        )

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
            "stop_all": stop_all,
            "breaker_tripped": tripped,
            "running_runners": running,
            "curriculum": curriculum,
        }
        doc, actions = reconcile.decide(doc, f, self.cfg.rules, now)

        # --- 一段目: 状態遷移を副作用より先に永続化する (レビュー指摘 [8])。
        # ここで落ちても副作用は未実行なので、次のビートが同じ判断をやり直すだけ。
        # 逆順 (実行→保存) だと、保存失敗の翌ビートが「実行済みの副作用」を知らずに
        # 二重実行する
        sf.save_projects(doc)
        sf.save_cursors(cursors)
        for item in review_needed:
            sf.append_jsonl("briefing-queue.jsonl", {"at": now_iso(now), **item})
        gitutil.commit_and_push_state(
            self.state_dir, self.cfg.state_branch, f"heart: beat {i} decide"
        )

        # --- 二段目: 副作用の実行と、その結果 (job 名等) の永続化 ---
        self.execute(actions, doc, sf, notifier, now)

        sf.append_jsonl(
            "metrics.jsonl",
            {
                "at": now_iso(now),
                "beat": i,
                "projects": {p["id"]: p["state"] for p in doc["projects"]},
                "jobs": len(jobs) if jobs is not None else None,
                "open_prs": len(open_prs),
                "unhealthy_apps": unhealthy_apps,
                "health_fresh": health_fresh,
                "breaker": breaker_info,
                "actions": [a["type"] for a in actions],
                "shadow": self.cfg.shadow,
            },
        )
        sf.save_projects(doc)
        sf.write_heartbeat(i, now)
        if not self.cfg.shadow:
            notifier.flush_outbox(now)
        removed = metrics.rotate_transcripts(self.transcripts, self.cfg.rules, now)
        if removed:
            log(f"rotated {removed} old transcript files")
        gitutil.commit_and_push_state(
            self.state_dir, self.cfg.state_branch, f"heart: beat {i}"
        )

    def self_update_check(self):
        tree = gitutil.run(
            ["rev-parse", "origin/main:ops/heart"], cwd=self.repo_dir, check=False
        )
        if self.start_tree is None:
            self.start_tree = tree
        elif tree and tree != self.start_tree:
            log(f"ops/heart が更新された ({self.start_tree[:12]} -> {tree[:12]})。exec し直す")
            os.chdir(self.repo_dir)
            os.execv(sys.executable, [sys.executable, "-m", "ops.heart.heart"])

    def run(self):
        signal.signal(signal.SIGTERM, _sigterm)
        signal.signal(signal.SIGINT, _sigterm)
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
