#!/usr/bin/env python3
"""P-0164 — ArgoCD 全停止中のデプロイ継続性演習の統括スクリプト。

唯一のデプロイ経路 Git → CI → ArgoCD について、「癒し手自身 (ArgoCD) が死んでいる
間に main が進んだら何が起きるか」を実測する。既存の復旧計測系 (P-0080 RTO /
P-0094 canary 自壊) はすべて「ArgoCD が生きていてアプリが死ぬ」方向で、この方向
だけ未開拓だった。

使い方::

    python3 ops/tools/deploy_continuity.py --dry-run   # 安全弁の判定だけ見る。書き込み無し
    python3 ops/tools/deploy_continuity.py --run       # 演習本体 (要: 安全弁が開いていること)

--run の手順 (spec の順で固定):

  1. 安全弁: origin/ops-state の projects.json を読み、自己 (P-0164) 以外に
     announced/active が 1 件でも居たら始めない (rc=2)。演習はリポジトリ全体の
     デプロイを凍結するので、他プロジェクトの作業時間帯に重ねてはいけない
  2. ArgoCD 3 コンポーネントを一斉 scale 0 (下記 TARGETS。controller は StatefulSet)
  3. main へ可逆な小変更 2 commit を積んでもらう (ruleset で直 push 不可のため PR
     経由。演習アプリ vaultwarden/coder の application.yaml へのラベル追加を想定。
     スクリプト自身は Git 書き込みをしない。ls-remote で main の前進を見るだけ)
  4. 所定時間 (--dwell) 後に scale 1 へ復帰
  5. 各 Application の status.sync.revision が新 main を指すまで (refresh)、さらに
     ラベルが live の Application CR に乗るまで (sync) の壁時計を秒で計測
  6. report.json に実測値を書き出す。異常終了時も scale 1 への復帰を最後に試みる

安全弁が自己を除外する理由 (解釈の明示。レビューで覆すのは容易にしてある):
spec は「projects.json に announced/active が 1 件も無いことを確認してからでないと
始まらない」と読めるが、P-0164 自身は演習中つねに active なので文字通り数えると
弁は永久に開かず、このプロジェクトは完遂できない。PROJECT.md が挙げる禁止理由は
「他プロジェクトのデプロイを凍結させることになるため」であり、自己の凍結は演習の
目的そのものなので害ではない。よって自己のみ除外し、除外した事実は報告に載せる。

既知の前提 (2026-08-23 実測):
  - chart 9.1.6 では application-controller だけ StatefulSet (server/repo-server は
    Deployment)。PROJECT.md 初版の「3 つとも Deployment」前提は誤り
  - kubectl write はこの 3 対象の scale 操作のみ (spec capabilities 制約)。
    dex / redis / applicationset-controller には触れない
  - 集計・検算は build_report / validate_report の純関数に切り出してあり、
    クラスタなしで試せる (ops/tests/test_deploy_continuity.py)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "ops" / "projects" / "logs" / "P-0164" / "report.json"

SELF_ID = "P-0164"
ARGOCD_NS = "argocd"
BLOCKING_STATES = frozenset({"announced", "active"})

# chart 9.1.6 実測。controller だけ StatefulSet なのはチャート構造そのもの
TARGETS = (
    {"kind": "deployment", "name": "argocd-server"},
    {"kind": "deployment", "name": "argocd-repo-server"},
    {"kind": "statefulset", "name": "argocd-application-controller"},
)

# 演習の可逆小変更を当てる子 Application。別アプリにすることで同期順序が観測できる。
# 変更内容は metadata.labels への LABEL_KEY 追加 (ワークロード再起動を誘発しない)
EXERCISE_APPS = ("vaultwarden", "coder")
LABEL_KEY = "p0164.continuity"

FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
OPS_STATE_BLOB = "origin/ops-state:projects.json"


class CommandError(RuntimeError):
    """外部コマンドの非ゼロ終了。stdout/stderr を握りつぶさず運ぶ。"""


# ---------------------------------------------------------------- 時刻ユーティリティ


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(ts):
    """ISO 8601 をパースする。Z 接尾子は古い fromisoformat が読めないので置換。"""
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def seconds_between(start_iso, end_iso):
    """end - start を秒 (小数第 1 位で丸め) で。負になるのは呼び出し側のバグ。"""
    delta = (parse_iso(end_iso) - parse_iso(start_iso)).total_seconds()
    return round(delta, 1)


# ---------------------------------------------------------------- 安全弁 (純関数)


def parse_projects(text):
    """projects.json (ops-state ブランチ) の本文を projects の list へ。"""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise ValueError("projects.json に projects の list が無い")
    return data["projects"]


def blocking_projects(projects, exclude=(SELF_ID,)):
    """演習が凍結してはいけない他プロジェクトの id をソートして返す。

    自己は除外する (モジュール docstring の「解釈の明示」節)。除外は id 完全一致。
    """
    dropped = set(exclude)
    return sorted(
        p["id"]
        for p in projects
        if p.get("state") in BLOCKING_STATES and p.get("id") not in dropped
    )


def valve_verdict(projects, exclude=(SELF_ID,)):
    blocked = blocking_projects(projects, exclude)
    kept = set(exclude)
    return {
        "ok": not blocked,
        "blocking": blocked,
        "excluded_self": sorted(
            p["id"]
            for p in projects
            if p.get("state") in BLOCKING_STATES and p.get("id") in kept
        ),
        "checked": len(projects),
    }


# ---------------------------------------------------------------- レポート集計 (純関数)


def sync_order(app_results):
    """sync 完了 (label 反映) を観測できた順に名前を並べる。未完了は含まれない。"""
    done = sorted((a["synced_at"], a["name"]) for a in app_results if a.get("synced_at"))
    return [name for _, name in done]


def build_report(m):
    """実測の生時刻 (ISO 文字列) から report.json の内容を組み立てる。純関数。

    catchup_seconds の定義: scale 1 発行 → 全演習アプリへの変更反映完了 (label 到達)。
    1 つでも反映を確認できなければ取りこぼしなので null。
    refresh_seconds の定義: scale 1 発行 → 全アプリの status.sync.revision が新 main
    を指し切るまで。「ArgoCD が Git の前進に気いた」段階と「適用し切った」段階を
    分けて記録する (refresh だけで sync しない落ち方があるかを判定できる)。
    """
    apps = [
        {
            "name": a["name"],
            "refreshed_at": a.get("refreshed_at"),
            "synced_at": a.get("synced_at"),
        }
        for a in m["apps"]
    ]
    missed = any(a["synced_at"] is None for a in apps)
    refreshed = [a["refreshed_at"] for a in apps if a.get("refreshed_at")]
    synced = [a["synced_at"] for a in apps if a.get("synced_at")]
    return {
        "project": SELF_ID,
        "ran_at": m["ran_at"],
        "targets": [dict(t) for t in TARGETS],
        "base_main_sha": m["base_main_sha"],
        "new_main_shas": list(m["new_main_shas"]),
        "commits_landed_at": m["commits_landed_at"],
        "down_confirmed_at": m["down_confirmed_at"],
        "up_issued_at": m["up_issued_at"],
        "caught_up_at": max(synced) if not missed else None,
        "refreshed_all_at": max(refreshed) if refreshed else None,
        "downtime_seconds": seconds_between(m["down_confirmed_at"], m["up_issued_at"]),
        "catchup_seconds": (
            seconds_between(m["up_issued_at"], max(synced)) if not missed else None
        ),
        "refresh_seconds": (
            seconds_between(m["up_issued_at"], max(refreshed)) if refreshed else None
        ),
        "missed_changes": missed,
        "sync_order": sync_order(apps),
        "apps": apps,
        "self_heal_restored": bool(m.get("self_heal_restored")),
        "outage_view": m.get("outage_view"),
        "observations": m.get("observations", {}),
    }


def validate_report(report):
    """report.json の不変条件を検査して違反文字列の list を返す (空なら健全)。"""
    errors = []
    required = (
        "project",
        "ran_at",
        "base_main_sha",
        "new_main_shas",
        "commits_landed_at",
        "down_confirmed_at",
        "up_issued_at",
        "apps",
    )
    for key in required:
        if report.get(key) in (None, "", [], ()):
            errors.append("必須キー {} が空".format(key))
    # verify が直接見るキー。値は null 許容 (取りこぼし時) だが key 自体は必須
    if "catchup_seconds" not in report:
        errors.append("catchup_seconds キーが無い")
    for key in ("downtime_seconds", "catchup_seconds", "refresh_seconds"):
        value = report.get(key)
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            errors.append("{} が負または数値でない: {!r}".format(key, value))
    apps = report.get("apps") or []
    for app in apps:
        if not app.get("name"):
            errors.append("apps の要素に name が無い: {!r}".format(app))
        if app.get("synced_at") is None and not report.get("missed_changes"):
            errors.append(
                "{} の synced_at が無いのに missed_changes が立っていない".format(app.get("name"))
            )
    # catchup_seconds の null 許容は「取りこぼしを記録したとき」に限る。
    # verify は key の存在だけを見るので、ここで値と旗の整合を見る
    flag = report.get("missed_changes")
    if not isinstance(flag, bool):
        errors.append("missed_changes が真偽値でない: {!r}".format(flag))
    catch = report.get("catchup_seconds")
    if catch is None and flag is False:
        errors.append("catchup_seconds が null なのに missed_changes が False")
    if catch is not None and flag is True:
        errors.append("取りこぼしを記録したのに catchup_seconds が数値になっている")
    order = report.get("sync_order") or []
    synced_names = {a["name"] for a in apps if a.get("synced_at")}
    if set(order) != synced_names:
        errors.append("sync_order {} が sync 済みアプリ {} と一致しない".format(order, sorted(synced_names)))
    try:
        if report.get("down_confirmed_at") and report.get("up_issued_at"):
            if seconds_between(report["down_confirmed_at"], report["up_issued_at"]) < 0:
                errors.append("up_issued_at が down_confirmed_at より前")
    except (ValueError, TypeError) as exc:
        errors.append("時刻列のパースに失敗: {}".format(exc))
    return errors


# ---------------------------------------------------------------- 外部コマンド層 (注入可能)


def default_runner(cmd, cwd=None, timeout=120):
    return subprocess.run(
        cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True
    )


def run(runner, cmd, cwd=None, timeout=120):
    proc = runner(cmd, cwd=cwd, timeout=timeout)
    if getattr(proc, "returncode", 1) != 0:
        raise CommandError(
            "{} failed rc={}: {}".format(" ".join(cmd), proc.returncode, (proc.stderr or "").strip())
        )
    return proc.stdout


def fetch_ops_state_projects(runner, cwd=ROOT):
    """ops-state ブランチから projects.json を取る。

    shallow clone は remote.origin.fetch が 1 ブランチ分しか無く、素の git fetch
    では origin/ops-state が生えない (substrate.md 実測の罠)。refspec を明示する。
    """
    run(runner, ["git", "fetch", "--quiet", "origin", FETCH_REFSPEC], cwd=cwd)
    return parse_projects(run(runner, ["git", "show", OPS_STATE_BLOB], cwd=cwd))


def ls_remote_main(runner, cwd=ROOT):
    out = run(runner, ["git", "ls-remote", "origin", "refs/heads/main"], cwd=cwd)
    sha = out.split()[0] if out.split() else ""
    if not sha:
        raise CommandError("ls-remote が main の SHA を返さない")
    return sha


def k_scale(runner, kind, name, replicas):
    """scale 操作。spec 制約によりこの形の kubectl write は TARGETS だけを許す。"""
    allowed = {(t["kind"], t["name"]) for t in TARGETS}
    if replicas != 0 and replicas != 1:
        raise ValueError("scale 先は 0/1 のみ: {!r}".format(replicas))
    if (kind, name) not in allowed:
        raise ValueError("scale 許可対象外: {}/{}".format(kind, name))
    run(
        runner,
        [
            "kubectl",
            "scale",
            "{}/{}".format(kind, name),
            "-n",
            ARGOCD_NS,
            "--replicas={}".format(replicas),
        ],
    )


def target_status(runner, kind, name):
    out = run(runner, ["kubectl", "get", kind, name, "-n", ARGOCD_NS, "-o", "json"])
    data = json.loads(out)
    spec = data.get("spec", {}) or {}
    status = data.get("status", {}) or {}
    return {
        "kind": kind,
        "name": name,
        "replicas": spec.get("replicas"),
        "ready": status.get("readyReplicas") or 0,
    }


def applications_view(runner):
    """argocd ns の全 Application を {name: {revision, labels, health}} で返す。"""
    out = run(
        runner, ["kubectl", "get", "applications.argoproj.io", "-n", ARGOCD_NS, "-o", "json"]
    )
    items = (json.loads(out).get("items")) or []
    view = {}
    for item in items:
        meta = item.get("metadata", {}) or {}
        status = item.get("status", {}) or {}
        view[meta.get("name")] = {
            "revision": (status.get("sync", {}) or {}).get("revision"),
            "labels": meta.get("labels") or {},
            "health": ((status.get("health") or {}).get("status")),
            "sync_status": (status.get("sync", {}) or {}).get("status"),
        }
    return view


def wait_until(fn, timeout, poll_interval=5.0, clock=time.monotonic, sleep=time.sleep):
    """fn() の真値を返すまで poll。制限時間切れで None。"""
    deadline = clock() + timeout
    while True:
        result = fn()
        if result:
            return result
        if clock() >= deadline:
            return None
        sleep(poll_interval)


def all_zero(runner):
    return all(target_status(runner, t["kind"], t["name"])["ready"] == 0 for t in TARGETS)


def all_ready(runner):
    statuses = [target_status(runner, t["kind"], t["name"]) for t in TARGETS]
    return statuses if all(s["ready"] == s["replicas"] == 1 for s in statuses) else None


def watch_main_advance(runner, base_sha, commit_count, max_wait, settle,
                       clock=time.monotonic, sleep=time.sleep, now=utc_now_iso):
    """main の前進を監視し (新 SHA の列, 確定時刻) を返す。

    SHA が settle 秒動かなければ確定。max_wait 内に一度も動かなければ None。
    個数が commit_count に満ない場合も列を返す — 足りない判定は呼び出し側で
    行う (何個観測できたかをエラーに残すため)。
    """
    seen = []
    stable_since = None
    deadline = clock() + max_wait
    while clock() < deadline:
        sha = ls_remote_main(runner)
        if sha != base_sha and (not seen or sha != seen[-1]):
            if sha not in seen:
                seen.append(sha)
            stable_since = clock()
        if seen and clock() - stable_since >= settle:
            return seen, now()
        sleep(5)
    return None


def wait_catchup(runner, want_sha, apps, deadline_s, poll_interval=5.0,
                 clock=time.monotonic, sleep=time.sleep, now=utc_now_iso):
    """各アプリの refresh (revision 到達) と sync (label 到達) 時刻を集める。"""
    results = [{"name": n, "refreshed_at": None, "synced_at": None} for n in apps]
    deadline = clock() + deadline_s
    while clock() < deadline:
        view = applications_view(runner)
        for entry in results:
            state = view.get(entry["name"])
            if not state:
                continue
            if entry["refreshed_at"] is None and state["revision"] == want_sha:
                entry["refreshed_at"] = now()
            if entry["synced_at"] is None and state["labels"].get(LABEL_KEY):
                entry["synced_at"] = now()
        if all(e["synced_at"] for e in results):
            break
        sleep(poll_interval)
    return results


# ---------------------------------------------------------------- 統括


def cmd_dry_run(args, runner=default_runner):
    """安全弁の判定だけを行って終わる。クラスタ書き込みは一切しない。

    読み取り (kubectl get) は試みるが、クラスタに届かない環境では skipped を
    記して続行する — dry-run の価値は「弁の状態が見えること」にあり、verify は
    クラスタなしの checkout でも完走することを求める。
    """
    report = {"mode": "dry-run", "cluster_writes": False, "checked_at": utc_now_iso()}
    try:
        projects = fetch_ops_state_projects(runner)
        verdict = valve_verdict(projects)
        if args.exclude_all:
            verdict = valve_verdict(projects, exclude=())
        report["valve"] = verdict
    except Exception as exc:  # 判定不能は判定不能として見せる (握りつぶさない)
        report["valve"] = {"ok": None, "error": "{}: {}".format(type(exc).__name__, exc)}
    try:
        statuses = [target_status(runner, t["kind"], t["name"]) for t in TARGETS]
        report["targets_seen"] = [
            {"kind": s["kind"], "name": s["name"], "replicas": s["replicas"], "ready": s["ready"]}
            for s in statuses
        ]
        report.setdefault("plan", plan_lines())
    except Exception as exc:
        report["targets_seen"] = "skipped ({})".format(exc)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if isinstance(report.get("valve"), dict) and report["valve"].get("ok") is not None else 1


def plan_lines():
    return [
        "1. 安全弁: 自己以外の announced/active が 0 件であることを確認",
        "2. scale 0: " + ", ".join("{}/{}".format(t["kind"], t["name"]) for t in TARGETS),
        "3. main へ可逆小変更 2 commit (PR 経由): {}/application.yaml と {}/application.yaml にラベル {} 追加".format(
            EXERCISE_APPS[0], EXERCISE_APPS[1], LABEL_KEY
        ),
        "4. dwell 後 scale 1",
        "5. status.sync.revision/label の到達を秒で計測し report.json へ",
    ]


def restore_scale_one(runner, timeout=180, poll_interval=5.0,
                      clock=time.monotonic, sleep=time.sleep):
    """異常終了時を含む最終復帰。失敗しても例外を投げず、結果を返す。"""
    restored = []
    try:
        for t in TARGETS:
            status = target_status(runner, t["kind"], t["name"])
            if status["replicas"] != 1:
                k_scale(runner, t["kind"], t["name"], 1)
                restored.append(t["name"])
        wait_until(lambda: all_ready(runner), timeout=timeout,
                   poll_interval=poll_interval, clock=clock, sleep=sleep)
    except Exception as exc:
        return {"restored": restored, "error": str(exc)}
    return {"restored": restored, "ready": all_ready(runner) is not None}


def cmd_run(args, runner=default_runner, clock=time.monotonic, sleep=time.sleep,
            now=utc_now_iso):
    """演習本体。どの経路で抜けても scale 1 復帰が最後になるよう try/finally。

    安全弁の拒否 (rc=2) はクラスタに一切触れない — 弁チェックは try の外で行う。
    """
    projects = fetch_ops_state_projects(runner)
    verdict = valve_verdict(projects)
    if not verdict["ok"]:
        print(json.dumps({"mode": "run", "started": False, "valve": verdict},
                         ensure_ascii=False, indent=2))
        return 2
    measurements = {}
    try:
        baseline = [target_status(runner, t["kind"], t["name"]) for t in TARGETS]
        if not all(s["replicas"] == 1 for s in baseline):
            raise CommandError("baseline replicas != 1: {!r}".format(baseline))
        base_sha = ls_remote_main(runner)

        for t in TARGETS:
            k_scale(runner, t["kind"], t["name"], 0)
        wait_until(lambda: all_zero(runner), timeout=args.down_timeout,
                   poll_interval=args.poll, clock=clock, sleep=sleep)
        down_confirmed_at = now()

        watched = watch_main_advance(
            runner, base_sha, commit_count=2, max_wait=args.max_wait,
            settle=args.settle, clock=clock, sleep=sleep, now=now,
        )
        if watched is None:
            raise CommandError(
                "main が一度も動かないまま --max-wait={}s に達した".format(args.max_wait)
            )
        new_shas, commits_landed_at = watched
        if len(new_shas) < 2:
            raise CommandError(
                "main の前進が {} commit しか観測できなかった (要 2): {}".format(
                    len(new_shas), new_shas
                )
            )

        sleep(args.dwell)
        outage_view = applications_view(runner)

        pre_up = [target_status(runner, t["kind"], t["name"]) for t in TARGETS]
        self_heal_restored = any((s["replicas"] or 0) > 0 for s in pre_up)
        up_issued_at = now()
        for s, t in zip(pre_up, TARGETS):
            if (s["replicas"] or 0) == 0:
                k_scale(runner, t["kind"], t["name"], 1)
        wait_until(all_ready, args.up_timeout, args.poll, clock, sleep)

        results = wait_catchup(
            runner, new_shas[-1], EXERCISE_APPS, deadline_s=args.catchup_timeout,
            poll_interval=args.poll, clock=clock, sleep=sleep, now=now,
        )

        measurements.update({
            "ran_at": down_confirmed_at,
            "base_main_sha": base_sha,
            "new_main_shas": new_shas,
            "commits_landed_at": commits_landed_at,
            "down_confirmed_at": down_confirmed_at,
            "up_issued_at": up_issued_at,
            "apps": results,
            "self_heal_restored": self_heal_restored,
            "outage_view": {
                name: {k: v for k, v in state.items() if k in ("health", "sync_status", "revision")}
                for name, state in sorted(outage_view.items())
            },
            "observations": {"notes_file": None},
        })
        if args.notes_file:
            with open(args.notes_file, encoding="utf-8") as fh:
                measurements["observations"] = json.load(fh)
        report = build_report(measurements)
        errors = validate_report(report)
        if errors:
            raise ValueError("report 検算 NG: {}".format("; ".join(errors)))
        out_path = Path(args.out) if args.out else REPORT_PATH
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(json.dumps({"mode": "run", "report": str(out_path),
                          "catchup_seconds": report["catchup_seconds"],
                          "downtime_seconds": report["downtime_seconds"],
                          "missed_changes": report["missed_changes"],
                          "sync_order": report["sync_order"]},
                         ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print("exercise aborted: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 3
    finally:
        # 正常系でも scale 1 発行後に ready 確認で抜けているが、ここでも必ず試す
        state = restore_scale_one(runner)
        print("final restore: {}".format(json.dumps(state, ensure_ascii=False)),
              file=sys.stderr)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="安全弁の判定だけ行い、書き込み無しで終わる")
    parser.add_argument("--exclude-all", action="store_true",
                        help="(dry-run 専用) 自己除外を外した文字通りの判定も併せて見る")
    parser.add_argument("--run", action="store_true", help="演習本体を実施する")
    parser.add_argument("--out", default=None, help="report.json の出力先 (既定: logs/P-0164)")
    parser.add_argument("--notes-file", default=None,
                        help="watcher/critic の観察を JSON で渡すと observations に入る")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--dwell", type=float, default=60.0,
                        help="commit 確認後も ArgoCD を止めておく秒数")
    parser.add_argument("--settle", type=float, default=20.0,
                        help="main の SHA がこの秒数動かなければ確定とみなす")
    parser.add_argument("--max-wait", type=float, default=900.0,
                        help="main が 2 commit 進むのを待つ上限秒")
    parser.add_argument("--down-timeout", type=float, default=120.0)
    parser.add_argument("--up-timeout", type=float, default=300.0)
    parser.add_argument("--catchup-timeout", type=float, default=600.0,
                        help="scale 1 から全アプリ反映までの待ち上限秒")
    args = parser.parse_args(argv)
    if sum([args.dry_run, args.run]) != 1:
        parser.error("--dry-run か --run のどちらかを指定する")
    # runner はここで解決して渡す (デフォルト引数にすると定義時束縛になり、
    # テストからの差し替えが効かない)
    if args.run:
        return cmd_run(args, runner=default_runner)
    return cmd_dry_run(args, runner=default_runner)


if __name__ == "__main__":
    sys.exit(main())
