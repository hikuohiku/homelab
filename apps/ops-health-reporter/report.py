"""クラスタ内から ArgoCD/Pod/PVC/Node の状態を集め、クラスタ内の ConfigMap へ書き戻す。

書き先は autopilot namespace の ConfigMap ops-health-report で、読み手 (heart / 常駐コア)
と同じ namespace に置く。以前は GitHub の ops-health-report ブランチを経由していたが、
書き手も読み手もクラスタの中にいるので往復する理由が無く、GitHub が落ちると器の健全性
情報が止まっていた（設計 docs/design/state-out-of-git Phase 5）。

標準ライブラリのみで動く（イメージに pip install を要求しない）。k8s API へは
ServiceAccount トークン（自動マウント）で到達する。GitHub の credential はもう要らない。
"""

import datetime
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# 純関数モジュール (P-0128)。configMapGenerator で report.py と同じ /scripts に載るため、
# `python /scripts/report.py` 起動時は sys.path[0]=/scripts で解決済み。この append は
# cluster 外 (CI・検査スクリプト) が importlib でロードしたときの解決用フォールバック
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import download_budget
# CPU 飽和前兆の判定 (P-9037)。canonical は ops/tools/node_saturation.py で、
# configMapGenerator がこのディレクトリに置いた同一内容のコピーを import する
# (drift は ops/check_node_saturation_script_sync.py が CI で検出)
import node_saturation

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
K8S_BASE = "https://{}:{}".format(K8S_HOST, K8S_PORT)

with open(os.path.join(SA_DIR, "token")) as f:
    SA_TOKEN = f.read().strip()
SSL_CTX = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))


def k8s_get(path):
    req = urllib.request.Request(
        K8S_BASE + path, headers={"Authorization": "Bearer " + SA_TOKEN}
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
        return json.load(resp)


def k8s_get_text(path):
    # /log エンドポイントはプレーンテキストを返す（JSON ではない）
    req = urllib.request.Request(
        K8S_BASE + path, headers={"Authorization": "Bearer " + SA_TOKEN}
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def collect(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — レポートの1項目が壊れても残りは出したい
        return {"error": "{}: {}".format(type(e).__name__, e)}


def collect_applications():
    data = k8s_get("/apis/argoproj.io/v1alpha1/applications")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        out.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "sync": status.get("sync", {}).get("status"),
                "health": status.get("health", {}).get("status"),
            }
        )
    return out


# 異常時に残す証拠の大きさ。terminationMessage は kubelet 側で 4096 バイトに
# 切られるが、履歴（history/*.jsonl）は 1 行 1 レポートで無限に伸びるので
# こちら側でも切る。FATAL の 1 行が読めれば足りるので 1200 文字にした。
TERMINATION_MESSAGE_LIMIT = 1200
# 1 レポートに載せる証拠の上限。全 Pod が同時に壊れてもファイルが破裂しない。
MAX_EVIDENCE_PER_REPORT = 8


def _terminated_evidence(cs):
    """直前に終了したコンテナの証拠を取り出す。

    「落ちた」ことだけを記録して「なぜ落ちたか」を捨てていたため、後から調べた
    ときには必ず手遅れになっていた（T-0112: immich-postgres の CrashLoopBackOff の
    FATAL が、events もログも保持期間切れで誰にも分からなくなった）。

    lastState.terminated は `pods` の read 権限だけで読める。ログ本文を取るには
    pods/log が要るが、それは autopilot namespace に閉じる判断を既にしている
    （T-0110、rbac.yaml のコメント参照）。その判断を覆さずに原因を残すため、
    ワークロード側を terminationMessagePolicy: FallbackToLogsOnError にして
    kubelet に異常終了時のログ末尾を message へ入れさせている。
    """
    term = (cs.get("lastState") or {}).get("terminated")
    if not term:
        return None
    message = (term.get("message") or "").strip()
    truncated = len(message) > TERMINATION_MESSAGE_LIMIT
    return {
        "container": cs.get("name"),
        # restart_count 込みで「どの落下か」が一意になる。同じ値なら同じ事象なので
        # 読む側で重複を捨てられる（レポートは 30 分毎に出るため）
        "signature": "{}:{}".format(cs.get("name"), cs.get("restartCount", 0)),
        "exit_code": term.get("exitCode"),
        "reason": term.get("reason"),
        "finished_at": term.get("finishedAt"),
        "message": message[:TERMINATION_MESSAGE_LIMIT] if message else None,
        "message_truncated": truncated,
        # message が空なら、そのワークロードが FallbackToLogsOnError になっていない
        "message_available": bool(message),
    }


def collect_pod_issues():
    data = k8s_get("/api/v1/pods")
    issues = []
    evidence_budget = MAX_EVIDENCE_PER_REPORT
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        phase = status.get("phase")
        cs_list = status.get("containerStatuses") or []
        restarts = sum(cs.get("restartCount", 0) for cs in cs_list)
        waiting_reasons = [
            cs["state"]["waiting"]["reason"]
            for cs in cs_list
            if "waiting" in cs.get("state", {})
        ]
        if phase not in ("Running", "Succeeded") or restarts > 3 or waiting_reasons:
            issue = {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "phase": phase,
                "restarts": restarts,
                "waiting_reasons": waiting_reasons,
            }
            evidence = [e for e in (_terminated_evidence(cs) for cs in cs_list) if e]
            if evidence and evidence_budget > 0:
                issue["terminated"] = evidence[:evidence_budget]
                evidence_budget -= len(issue["terminated"])
            elif evidence:
                issue["terminated_omitted"] = len(evidence)
            issues.append(issue)
    return issues


def collect_pvcs():
    data = k8s_get("/api/v1/persistentvolumeclaims")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        out.append(
            {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "phase": status.get("phase"),
                "requested": spec.get("resources", {}).get("requests", {}).get("storage"),
                "capacity": status.get("capacity", {}).get("storage"),
            }
        )
    return out


def collect_pod_metrics():
    data = k8s_get("/apis/metrics.k8s.io/v1beta1/pods")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        out.append(
            {
                "namespace": meta.get("namespace"),
                "name": meta.get("name"),
                "containers": [
                    {
                        "name": c.get("name"),
                        "cpu": c.get("usage", {}).get("cpu"),
                        "memory": c.get("usage", {}).get("memory"),
                    }
                    for c in item.get("containers", [])
                ],
            }
        )
    return out


def collect_node_metrics():
    data = k8s_get("/apis/metrics.k8s.io/v1beta1/nodes")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        usage = item.get("usage", {})
        out.append(
            {
                "name": meta.get("name"),
                "cpu": usage.get("cpu"),
                "memory": usage.get("memory"),
            }
        )
    return out


def collect_node_saturation():
    """node01 の CPU 飽和前兆を実測する (P-9037)。

    2026-08-24 18:18 JST、runner×2 + curriculum + heart の requests 合計
    3761m/4000m でホスト load 25 になり kube-apiserver も sshd も応答不能になった。
    「もうすぐ沈む」を告げる計器として、requests/allocatable とホスト load を
    実測し閾値超過で status=warn を返す (判定は node_saturation.judge の純関数)。

    requests は全 namespace の pod spec、allocatable は node status から取り、
    metrics.k8s.io は要らない。load は pod 内から読める /proc/loadavg を使う
    (loadavg は PID namespace で仮想化されない — substrate.md 実測記録参照。
    kubelet stats/summary には host load が無いため、取得源は /proc に倒す)。
    単一ノード前提 (substrate.md の実測どおり)。読み手 (heart) は status=warn を
    briefing / incident へ流す (P-0128 の budget 警告と同じ 2 段階)。
    """
    pods = k8s_get("/api/v1/pods")
    nodes = k8s_get("/api/v1/nodes")
    items = nodes.get("items", [])
    if not items:
        raise RuntimeError("node が見つからない")
    # 単一ノード前提。node01 があればそれを、無ければ最初の 1 台を使う
    node = next(
        (n for n in items if n.get("metadata", {}).get("name") == "node01"),
        items[0],
    )
    name = node.get("metadata", {}).get("name")
    requests_m = node_saturation.sum_cpu_requests(pods)
    alloc_m = node_saturation.allocatable_cpu_millicores(node)
    load = node_saturation.read_loadavg()
    report = node_saturation.judge(requests_m, alloc_m, load, node_saturation.vcpus(node))
    report["node"] = name
    report["load_source"] = "proc_loadavg" if load is not None else None
    report["checked_at"] = datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return report


PVC_USAGE_NAMESPACES = ["immich", "vaultwarden", "coder"]


def collect_pvc_usage():
    out = []
    for ns in PVC_USAGE_NAMESPACES:
        try:
            data = k8s_get(
                "/api/v1/namespaces/{}/configmaps/pvc-usage-report".format(ns)
            )
            report = json.loads(data.get("data", {}).get("report.json", "{}"))
            out.append(report)
        except Exception as e:  # noqa: BLE001 — namespace 側の CronJob が
            # まだ1回も走っていない（ConfigMap 未作成）場合を含め、他 namespace の
            # 収集を止めない
            out.append({"namespace": ns, "error": "{}: {}".format(type(e).__name__, e)})
    return out


# B2 download cap の帳簿 (P-0128) の集約対象。restic リポジトリのある namespace。
# coder は postgres / workspace-homes の 2 リポジトリが同じ namespace にあり、
# syncthing は pvc_usage の対象外だが backup は存在する。産出側 (各 ns の
# download-ledger CronJob) が専用 ConfigMap `download-budget` の report.json キーに
# run 記録 ({date: "YYYY-MM-DD", job: 名前, bytes: N} のリスト、UTC 日付) を書く契約。
# 契約先を pvc-usage-report にしない理由: 既存 pvc-usage-reporter は PUT で data を
# 全体置換する (apps/*/pvc-usage-cronjob.yaml の put_configmap)。「別 CronJob が同じ
# ConfigMap に追加キーで書く」設計だと reporter run のたびに帳簿が消え、逆に素朴な
# PUT で産出側が report.json を吹き飛ばす。専用名にして RBAC の resourceNames に
# 追加する 1 行で済ませる (configmaps get の resourceNames 追加は T-0110 の
# pods/log 閉じ込みとは無関係なので整合問題も無い)
DOWNLOAD_BUDGET_NAMESPACES = ["immich", "vaultwarden", "coder", "syncthing"]


def collect_download_budget():
    """namespace ごとの帳簿を読み、download_budget.build_report() で集約する。

    1 namespace の失敗 (産出側未稼働の ConfigMap 未作成を含む) で他 namespace の
    収集を止めない (collect_pvc_usage() と同じ思想)。生 runs をレポートに載せず
    集約後の形だけ出すのは、history jsonl の 1 行が無限に膨らむのを防ぐため
    (download_budget.build_report の docstring 参照)。
    """
    entries = []
    for ns in DOWNLOAD_BUDGET_NAMESPACES:
        try:
            data = k8s_get("/api/v1/namespaces/{}/configmaps/download-budget".format(ns))
            raw = data.get("data", {}).get("report.json")
            if not raw:
                raise KeyError(
                    "configmap download-budget に report.json キーが無い"
                    "(産出側がまだ稼働していない)"
                )
            payload = json.loads(raw)
            runs = payload.get("runs") if isinstance(payload, dict) else None
            if not isinstance(runs, list):
                raise ValueError("download_budget.json の runs がリストでない")
            entries.append({"namespace": ns, "runs": runs})
        except Exception as e:  # noqa: BLE001 — 他 namespace の収集を止めない
            entries.append({"namespace": ns, "error": "{}: {}".format(type(e).__name__, e)})
    return download_budget.build_report(entries)


# 上流版の観測 (P-0126) の集約対象。産出側は version-watcher namespace の CronJob が
# 毎晩 inventory 全対象を観測し、専用 ConfigMap `version-drift` の report.json キーへ
# 書く契約 (download-budget / dashboard-smoke と同じ「産出側が専用 ConfigMap に書き、
# reporter が読む」分離)。以前は watcher が ops-health-report ブランチの latest.json に
# GET→merge→PUT で相乗りしていたが、レポートが ConfigMap へ移った (state-out-of-git
# Phase 5) 時点でその latest.json の読み手が消え、version_drift だけが誰も読まない枝に
# 積もっていた
VERSION_DRIFT_NAMESPACE = "version-watcher"
# 夜間 1 回の CronJob より長く沈黙していたら「装置が回っていない」(stale)。
# 24h + 12h マージン。#49 型の静かな放置を防ぐ装置自身が静かに止まるのを見逃さない
VERSION_DRIFT_STALE_AFTER_S = 36 * 3600


def _version_drift_summary(payload, now):
    """ConfigMap の report.json (watch.py の observe() 戻り値) を latest.json に載せる形へ。

    鮮度を最優先で判定する: 古い drift 一覧より「観測が止まっている」を先に報せる。
    """
    generated_at = payload["generated_at"]
    age = (
        now - datetime.datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    ).total_seconds()
    summary = payload["summary"]
    if not isinstance(summary, dict):
        raise ValueError("version-drift の summary が object でない")
    out = {
        "generated_at": generated_at,
        "age_seconds": int(age),
        "summary": summary,
        "drifted": payload.get("drifted", []),
        "errors": payload.get("errors", []),
    }
    if age > VERSION_DRIFT_STALE_AFTER_S:
        out["status"] = "stale"
        out["reason"] = "最終観測が {} 時間前 (夜間 CronJob が回っていない)".format(
            int(age // 3600)
        )
    else:
        out["status"] = "ok"
    return out


def collect_version_drift():
    """version-watcher ns の version-drift ConfigMap を読み、要約を返す (P-0126)。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        data = k8s_get(
            "/api/v1/namespaces/{}/configmaps/version-drift".format(
                VERSION_DRIFT_NAMESPACE
            )
        )
        raw = data.get("data", {}).get("report.json")
        if not raw:
            raise KeyError(
                "configmap version-drift に report.json キーが無い (産出側が未稼働)"
            )
        return _version_drift_summary(json.loads(raw), now)
    except Exception as e:  # noqa: BLE001 — 他の収集を止めない
        return {
            "status": "no_data",
            "reason": "configmap version-drift を読めない",
            "error": "{}: {}".format(type(e).__name__, e),
        }


# Mission Control 描画スモーク (P-0193) の集約対象。産出側は autopilot namespace の
# CronJob dashboard-smoke が、headless chromium での実際の描画断言結果を専用
# ConfigMap `dashboard-smoke` の report.json キーへ書く契約 (download-budget と同じ
# 「産出側が専用 ConfigMap に書き、reporter が読む」分離。pvc-usage-report への追加
# キーにしない理由も同じ: 既存 writer の PUT が data 全体置換のため)。ConfigMap 自体は
# manifest に事前作成しない (ArgoCD 管理外にして selfHeal との競合を避ける。
# pvc-usage-reporter と同じ形) — reporter RBAC 側はこの名前の get を resourceNames に
# 追加済み (rbac.yaml 参照)
DASHBOARD_SMOKE_NAMESPACE = "autopilot"
# 日次 CronJob の 1 回分より長く沈黙していたら「装置が回っていない」(stale)。
# 24h + 2h マージン。1 日落ちでも鳴る側に倒す: この装置の守備範囲は「静かに壊れて
# 人間の目を裏切る画面」であり、装置自身が静かに壊れるのは本末転倒という理由
DASHBOARD_SMOKE_STALE_AFTER_S = 26 * 3600


def _dashboard_smoke_summary(payload, now):
    """ConfigMap の report.json (dashboard_smoke.run_smoke() の result dict) を
    latest.json / history jsonl に載せる要約へ変える。

    生 checks は載せず失敗した検査だけを出す (history jsonl の 1 行膨張止め。
    collect_download_budget が生 runs を載せないのと同じ)。screenshot フィールドは
    Pod 内一時ディレクトリの path を含むため載せない (PNG 実体の履歴蓄積は
    PROJECT.md の「やらないこと」)。

    ランナー (dashboard-smoke-cronjob.yaml) はスモーク本体が JSON を書けなかったとき
    (rc=2, 装置の故障) ok=False・failed_checks 空・tool_error/tool_error_rc 付きの
    代役レコードを書く。この形には「描画断言が不合格」の文面を当てはめない —
    ページの嘘と装置の故障の区別が消えるため。reason を tool_error 由来に分岐し、
    切り詰めた tool_error / tool_error_rc を要約に載せる。

    形が契約通りでない場合 (ok が真偽値でない等) は ValueError — 呼び出し側が
    no_data エントリへ落とす。壊れた記録を黙って ok=False 扱いにすると
    「ページの嘘」と「帳簿の壊れ」が区別できなくなるため。
    """
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise ValueError("report.json の ok が真偽値でない")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("report.json に generated_at 文字列が無い")
    try:
        generated = datetime.datetime.strptime(
            generated_at, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        raise ValueError(
            "report.json の generated_at を解釈できない: {!r}".format(generated_at)
        )
    age_seconds = int((now - generated).total_seconds())
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise ValueError("report.json の checks がリストでない")
    failed_checks = []
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "fail":
            continue
        failed_checks.append({
            "name": check.get("name"),
            # detail は人間向け文面。1 行 1 レポートの history jsonl を膨らませない
            # ため切り詰める (collect_externalsecrets の message と同じ上限)
            "detail": str(check.get("detail") or "")[:200],
        })

    # 鮮度を最優先で判定する: 古い記録が fail を指したままでも「いつの時点の
    # 不合格か」は信頼できず、まず装置が沈黙していることを報せる
    status = "stale" if age_seconds > DASHBOARD_SMOKE_STALE_AFTER_S else (
        "ok" if ok else "fail"
    )
    tool_error = payload.get("tool_error")
    tool_error = tool_error.strip()[:200] if isinstance(tool_error, str) and tool_error.strip() else None
    tool_rc = payload.get("tool_error_rc")
    if isinstance(tool_rc, bool) or not isinstance(tool_rc, int):
        tool_rc = None
    if status == "stale":
        reason = "最終記録が {} 秒前 (> 上限 {} 秒) — CronJob dashboard-smoke が沈黙している疑い".format(
            age_seconds, DASHBOARD_SMOKE_STALE_AFTER_S
        )
    elif status == "fail" and tool_error:
        # rc=2 の代役レコード (装置の故障)。failed_checks は空なので
        # 「描画断言が不合格」の文面は嘘になる
        rc_note = " (rc={})".format(tool_rc) if tool_rc is not None else ""
        reason = "スモーク本体が異常終了{} — 装置が回らなかった: {}".format(rc_note, tool_error)
    elif status == "fail":
        if failed_checks:
            reason = "描画断言が不合格: " + ", ".join(c["name"] for c in failed_checks)
        else:
            reason = "描画断言が不合格 (失敗検査の内訳が記録されていない)"
    else:
        reason = "全 {} 検査合格 ({} 秒前の実測)".format(len(checks), age_seconds)
    out = {
        "status": status,
        "reason": reason,
        "ok": ok,
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "url": payload.get("url") if isinstance(payload.get("url"), str) else None,
        "http_status": (
            payload.get("http_status")
            if isinstance(payload.get("http_status"), int)
            and not isinstance(payload.get("http_status"), bool)
            else None
        ),
        "elapsed_s": (
            payload.get("elapsed_s")
            if isinstance(payload.get("elapsed_s"), (int, float))
            and not isinstance(payload.get("elapsed_s"), bool)
            else None
        ),
        "checks_total": len(checks),
        "failed_checks": failed_checks,
    }
    # 装置故障の記録だけが持つフィールド。全レコードへの None 載せは
    # history jsonl の 1 行を膨らませるため、あるときだけ載せる
    if tool_error:
        out["tool_error"] = tool_error
    if tool_rc is not None:
        out["tool_error_rc"] = tool_rc
    return out


def collect_dashboard_smoke():
    """autopilot namespace の dashboard-smoke ConfigMap を読み、要約を返す (P-0193)。

    Mission Control は readiness probe では見えない「描画したときだけ現れる嘘」
    (JS エラー・白画面・矛盾シグナルの共存) を持つ。産出側 CronJob が毎日実際に
    描画した断言結果を読むのがここ。status の意味:
      ok       最新の描画断言が合格 — 記録のみで通知予算は消費しない
      fail     断言が不合格 — ダッシュボードが嘘をついている。briefing/incident
               に乗るべき状態
      stale    記録が古い — 装置 (CronJob) 自身が沈黙している
      no_data  ConfigMap・キーが無い/読めない — 産出側がまだ走っていないか壊れた

    産出側未稼働・記録破損は例外にせず no_data で正直に出す (collect_pvc_usage と
    同じ思想)。「失敗時のみ既存経路 (latest.json の異常フィールド → autopilot の
    briefing) へ流す」抽出は heart 側の配線で、P-0128 が budget.status →
    facts.budget_alert の 2 段階で行ったのと同じ順序。まず latest.json 上で
    区別できることが先。
    """
    try:
        data = k8s_get(
            "/api/v1/namespaces/{}/configmaps/dashboard-smoke".format(
                DASHBOARD_SMOKE_NAMESPACE
            )
        )
        raw = data.get("data", {}).get("report.json")
        if not raw:
            raise KeyError(
                "configmap dashboard-smoke に report.json キーが無い"
                "(産出側がまだ稼働していない)"
            )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("report.json が dict でない")
        return _dashboard_smoke_summary(
            payload, datetime.datetime.now(datetime.timezone.utc)
        )
    except Exception as e:  # noqa: BLE001 — 未稼働・破損で他の収集を止めない
        return {
            "status": "no_data",
            "reason": "configmap dashboard-smoke を読めない",
            "error": "{}: {}".format(type(e).__name__, e),
        }


# ExternalSecret の spec.refreshInterval は K8s duration 文字列 ("1h" / "30m") で
# 来るため秒へ換算する (P-0175。数値の API バージョンも一応受ける)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}


def _duration_seconds(v):
    # 決められないときは 0 ではなく None (経過秒との比較で「即滞留」に見えるのを防ぐ)。
    # 空文字列や単独の単位 ("h") もここへ落とす
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    total = 0
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
        elif ch in _DURATION_UNITS:
            if not num:
                return None
            total += int(num) * _DURATION_UNITS[ch]
            num = ""
        else:
            return None
    if num:
        # 単位の無い数字列は秒とみなす ("3600"。API によっては数値が文字列で来る)
        total += int(num)
    return total


def collect_externalsecrets():
    """ExternalSecret の状態を全件集める (P-0175)。

    Doppler は全 ExternalSecret の唯一の上流で、refreshInterval は概ね 1h。
    上流が死んでも各 ES の refresh 時刻が巡ってくるまで何も表面化しない
    (遮断演習の実測: P-0175 drill-report.json 参照)。この「静かな鮮度劣化」を
    見える化するのが目的なので、Synced のまま古いものも区別せず
    最終同期からの経過秒を出す。1 件ごとの失敗で全体を止めない既存思想に従い
    個別のパース失敗は error エントリに落とす。載せるのは status と時刻のみで、
    Secret の値には触れない。
    """
    data = k8s_get("/apis/external-secrets.io/v1/externalsecrets")
    now = datetime.datetime.now(datetime.timezone.utc)
    items = []
    errored = []
    for raw in data.get("items", []):
        try:
            meta = raw.get("metadata", {})
            status = raw.get("status", {})
            cond_ready = next(
                (c for c in status.get("conditions", []) if c.get("type") == "Ready"),
                {},
            )
            reason = cond_ready.get("reason")
            refresh_time = status.get("refreshTime")
            last_sync_age_seconds = None
            if refresh_time:
                synced_at = datetime.datetime.strptime(
                    refresh_time, "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=datetime.timezone.utc)
                last_sync_age_seconds = int((now - synced_at).total_seconds())
            interval_raw = raw.get("spec", {}).get("refreshInterval")
            entry = {
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "ready": cond_ready.get("status"),
                "sync_reason": reason,
                "refresh_interval_seconds": _duration_seconds(interval_raw),
                "last_sync_age_seconds": last_sync_age_seconds,
                "message": None,
            }
            if reason == "SecretSyncedError":
                # message は上流エラーの手がかりだが history jsonl を膨らませないため
                # 切り詰める。errored の対象名は集約側でも出す
                entry["message"] = (cond_ready.get("message") or "")[:200]
                errored.append(
                    "{}: {}".format(meta.get("namespace"), meta.get("name"))
                )
            items.append(entry)
        except Exception as e:  # noqa: BLE001 — 1 件のパース失敗で他を止めない
            items.append({"error": "{}: {}".format(type(e).__name__, e)})
    items.sort(key=lambda x: (x.get("namespace") or "", x.get("name") or ""))
    return {
        "total": len(items),
        "secret_synced_errors": len(errored),
        "errored": errored,
        "items": items,
    }


def collect_nodes():
    data = k8s_get("/api/v1/nodes")
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
        out.append(
            {
                "name": meta.get("name"),
                "allocatable": status.get("allocatable"),
                "capacity": status.get("capacity"),
                "ready": conditions.get("Ready"),
                "diskPressure": conditions.get("DiskPressure"),
                "memoryPressure": conditions.get("MemoryPressure"),
            }
        )
    return out


# heart (ops/heart/heart.py) の log() が書く心拍行だけを抜き出す正規表現。
# 産出側は `[autopilot] <ts> iteration #N start` /
# `[autopilot] <ts> iteration #N end exit=<rc> elapsed=<n>s` を出す（旧 loop.sh と
# 同じ書式を heart が引き継いでいる）。この結合は ops/check_health_reporter_target.py が
# CI で機械検査している（heart 側の書式を変えるとそこで落ちる）。
# ここにマッチした行から取り出した値（timestamp/iteration/exit_code/elapsed_seconds/reason）
# だけを report に載せる。生ログはこの関数の外に一切出さない — claude の出力を
# git 管理のブランチにそのまま持ち出す経路を作らないため（T-0110）。
# exit code の後に "exit=124 (timed out after Ns) elapsed=Ns" のような括弧書きの理由が
# 挟まることがある（タイムアウト、または repo 同期前段での早期return, T-0158）ので、
# その部分を任意で捕捉する
HEARTBEAT_RE = re.compile(
    r"^\[autopilot\] (\S+) iteration #(\d+) (?:start|end exit=(-?\d+)(?: \(([^)]*)\))? elapsed=(\d+)s)"
)


def parse_heartbeat(raw_log):
    last_start = None
    last_end = None
    for line in raw_log.splitlines():
        m = HEARTBEAT_RE.match(line.strip())
        if not m:
            continue
        ts, iteration, exit_code, reason, elapsed = m.groups()
        if exit_code is None:
            last_start = {"timestamp": ts, "iteration": int(iteration)}
        else:
            last_end = {
                "timestamp": ts,
                "iteration": int(iteration),
                "exit_code": int(exit_code),
                "elapsed_seconds": int(elapsed),
            }
            if reason:
                last_end["reason"] = reason
    return {"last_start": last_start, "last_end": last_end}


AUTOPILOT_NAMESPACE = "autopilot"
# 観測対象は heart（heart-and-projects の常駐ループ、ops/heart/heart.py）。
# 旧ループの Deployment `autopilot` / label `app=autopilot` は replicas 0 で退役済みで、
# そちらを見ていた間は heart が死んでも「pod が見つからない」という同じ文字列が出続けて
# いた（異常が定常状態に埋もれる、P-0011）。
# この 2 つの値は apps/autopilot/heart-deployment.yaml が正で、
# ops/check_health_reporter_target.py が CI で一致を検査している
# （Deployment をリネームしたら CI が落ちる）。定数のまま持つこと — URL 文字列へ
# 直書きすると機械抽出できなくなる
AUTOPILOT_DEPLOYMENT = "autopilot-heart"
AUTOPILOT_APP_LABEL = "autopilot-heart"


def collect_autopilot_health():
    result = {}

    try:
        dep = k8s_get(
            "/apis/apps/v1/namespaces/{}/deployments/{}".format(
                AUTOPILOT_NAMESPACE, AUTOPILOT_DEPLOYMENT
            )
        )
        status = dep.get("status", {})
        result["deployment"] = {
            "replicas": status.get("replicas", 0),
            "readyReplicas": status.get("readyReplicas", 0),
            "unavailableReplicas": status.get("unavailableReplicas", 0),
        }
    except Exception as e:  # noqa: BLE001
        result["deployment"] = {"error": "{}: {}".format(type(e).__name__, e)}

    pod_name = None
    try:
        pods = k8s_get(
            "/api/v1/namespaces/{}/pods?labelSelector={}".format(
                AUTOPILOT_NAMESPACE,
                urllib.parse.quote("app={}".format(AUTOPILOT_APP_LABEL), safe=""),
            )
        )
        items = pods.get("items", [])
        pod_list = []
        for item in items:
            meta = item.get("metadata", {})
            pstatus = item.get("status", {})
            cs = (pstatus.get("containerStatuses") or [{}])[0]
            pod_list.append(
                {
                    "name": meta.get("name"),
                    "phase": pstatus.get("phase"),
                    "restartCount": cs.get("restartCount"),
                }
            )
        result["pods"] = pod_list
        running = [i for i in items if i.get("status", {}).get("phase") == "Running"]
        pick = running[0] if running else (items[0] if items else None)
        if pick:
            pod_name = pick.get("metadata", {}).get("name")
    except Exception as e:  # noqa: BLE001
        result["pods"] = {"error": "{}: {}".format(type(e).__name__, e)}

    if pod_name:
        try:
            # 行数ではなく時間で窓を取る。tailLines=200 は不十分だった（2026-08-06
            # run #89 で last_start/last_end が両方 null になる事象を観測）——
            # 旧ループは claude の stream-json を 1 行ずつ吐いたため、1 イテレーションで
            # 200 行を超え直近の "iteration #N start" が窓の外へ押し出されえた。
            # heart のビートは HEART_BEAT_SECONDS（ops/heart/config.py 既定 120s、
            # apps/autopilot/heart-deployment.yaml で上書き可）なので 7200s は
            # 数十周分にあたる。ビートを大きく変えたらここも見直すこと
            raw = k8s_get_text(
                "/api/v1/namespaces/{}/pods/{}/log?sinceSeconds=7200".format(
                    AUTOPILOT_NAMESPACE, pod_name
                )
            )
            result["heartbeat"] = parse_heartbeat(raw)
        except Exception as e:  # noqa: BLE001
            result["heartbeat"] = {"error": "{}: {}".format(type(e).__name__, e)}
    else:
        result["heartbeat"] = {
            "error": "app={} の pod が見つからない".format(AUTOPILOT_APP_LABEL)
        }

    return result


# 健全性レポートの置き場所。読み手 (heart / 常駐コア) と同じ namespace に置く。
# 値は latest.json キーに JSON 文字列 1 本。過去分は持たない（最新 1 点のみ）
HEALTH_NAMESPACE = "autopilot"
HEALTH_CONFIGMAP = "ops-health-report"
HEALTH_KEY = "latest.json"


def k8s_request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        K8S_BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + SA_TOKEN,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def put_configmap(namespace, name, data, request=None):
    """GET → resourceVersion 付き PUT、無ければ POST 作成（dashboard-smoke と同じ形）。

    ConfigMap は git に宣言しない。宣言すると ArgoCD の selfHeal が毎回書き戻し、
    30 分ごとの更新と綱引きになる。RBAC は create と、この名前だけの get/update。
    """
    call = request or k8s_request
    path = "/api/v1/namespaces/{}/configmaps/{}".format(namespace, name)
    status, existing = call("GET", path)
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace},
        "data": data,
    }
    if status == 200 and isinstance(existing, dict):
        body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        status, resp = call("PUT", path, body)
    else:
        status, resp = call(
            "POST", "/api/v1/namespaces/{}/configmaps".format(namespace), body
        )
    if status not in (200, 201):
        raise RuntimeError("configmap 書き込みに失敗: {} {}".format(status, resp))


def main():
    namespace = os.environ.get("HEALTH_NAMESPACE", HEALTH_NAMESPACE)
    name = os.environ.get("HEALTH_CONFIGMAP", HEALTH_CONFIGMAP)

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "generated_at": generated_at,
        "applications": collect(collect_applications),
        "pod_issues": collect(collect_pod_issues),
        "pvcs": collect(collect_pvcs),
        "nodes": collect(collect_nodes),
        "pod_metrics": collect(collect_pod_metrics),
        "node_metrics": collect(collect_node_metrics),
        "node_saturation": collect(collect_node_saturation),
        "pvc_usage": collect(collect_pvc_usage),
        "download_budget": collect(collect_download_budget),
        "externalsecrets": collect(collect_externalsecrets),
        "autopilot": collect(collect_autopilot_health),
        "dashboard_smoke": collect(collect_dashboard_smoke),
        "version_drift": collect(collect_version_drift),
        "notes": (
            "コンテナ/ノードの実メモリ・CPU 使用量は metrics-server (metrics.k8s.io) から取得 "
            "(pod_metrics/node_metrics)。PVC の実ディスク使用量は namespace ごとの pvc-usage-reporter "
            "CronJob が ConfigMap に書いた値を pvc_usage として集約（immich/vaultwarden/coder のみ。"
            "Coder workspace ごとの動的 PVC は対象外、T-0078 参照）。RBAC は get/list/(configmaps のみ get) "
            "で、write 系の verb は含まない。この ConfigMap (autopilot/ops-health-report の "
            "latest.json キー) は最新1点のみで上書きされ、履歴は持たない。"
            " nodes[].allocatable/capacity の ephemeral-storage は kubelet が一時ストレージ "
            "監視用に計算する値であり、ルートファイルシステムの全体サイズとは別物（node01 の root "
            "ファイルシステムは実際には約252GiBあるが、この値は約48.9GiBしか出ない）。ディスク容量の "
            "確認にこの値を使わないこと（T-0079, issue #56 2026-08-05 15:45:04 参照）。"
            " autopilot キーは autopilot 自身の健全性で、対象は namespace autopilot の "
            "Deployment autopilot-heart（heart, ops/heart/heart.py）。旧ループの "
            "Deployment autopilot は退役済み（replicas 0）。"
            "heartbeat.last_end が無い/古い、または exit_code が非 0 なら前回のイテレーションが"
            "異常終了またはハング中の疑い（T-0110）。pods/log は autopilot namespace に閉じた"
            "Role でのみ許可し、心拍行だけを正規表現で抽出している。生ログはここに含まれない。"
            " download_budget キーは B2 download cap の帳簿（P-0128）。restic バックアップのある "
            "namespace（immich/vaultwarden/coder/syncthing）の専用 ConfigMap download-budget の "
            "report.json キーに、download-ledger CronJob が書いた run 記録（{date, job, bytes}、"
            "UTC 日付）を集計し、直近7日の日次内訳・月次見積もり・cap 判定（ok/warn/exceed/"
            "unconfigured/no_data）を載せる。bytes は restic の転送統計からではなく操作種別ごとの "
            "推定モデル（産出側 CronJob の LEDGER_RULES）による推定量。"
            "cap の実値は B2 コンソールにしか無いため既定は unconfigured（決め打ちしない）。"
            "産出側がまだ稼働していない namespace は error エントリになる。"
            " externalsecrets キーは ExternalSecret 全件の状態（P-0175）。Doppler が唯一の上流で "
            "refreshInterval は概ね 1h のため、上流が死んでも refresh が巡るまで何も表面化しない。"
            "secret_synced_errors / errored は SecretSyncedError の件数と対象、items[].last_sync_age_seconds は "
            "status.refreshTime（最終成功同期）からの経過秒。「Ready=True のまま last_sync_age_seconds が "
            "refresh_interval_seconds を大きく超え続ける」場合は上流への同期が静かに滞留しているサイン。"
            "Secret 本体の値は取得・記録しない（RBAC も external-secrets.io/externalsecrets の get/list のみ）。"
             " dashboard_smoke キーは Mission Control の headless 描画スモーク（P-0193）。autopilot namespace の "
             "CronJob dashboard-smoke が毎日 headless chromium で実際に描画し、専用 ConfigMap dashboard-smoke の "
             "report.json キーへ書いた断言結果を集約する。status は ok / fail（ページが嘘をついている: "
             "矛盾シグナルの共存・白画面・描画未完了等。failed_checks に名前と detail を載せる）/ "
             "stale（最終記録が 26h より古い = 装置自身が沈黙）/ no_data（産出側未稼働・記録破損）。"
             "fail のうち tool_error を伴うものはスモーク本体自体が異常終了した記録（装置の故障。"
             "ランナーが代役レコードを書いた）で、ページの嘘とは区別できる。"
             "成功日は記録のみで通知予算を消費しない。readiness probe は HTTP 200 しか見ないため、"
             "この検査だけが「実際に描画したときだけ見える破綻」を拾う。スクリーンショット実体は保存せず "
             "記録しない。"
             " node_saturation キーは CPU 飽和前兆の常設計器（P-9037）。全 namespace の pod の "
             "spec.containers[].resources.requests.cpu 合計と node01 の status.allocatable.cpu から "
             "requests 比率を、pod 内から読める /proc/loadavg からホスト load を実測し、"
             "allocatable の 90% 超 または load > vCPU 数で status=warn（読み手の heart が "
             "briefing / incident へ流す）。loadavg は PID namespace で仮想化されないため node01 上の "
             "pod から host 全体の load が読める（2026-08-24 実測）。kubelet stats/summary には host "
             "load が無い（P-9029 審査指摘）ため取得源は /proc に倒している。"
             " version_drift キーは inventory 全対象の上流最新版の観測（P-0126）。version-watcher "
             "namespace の夜間 CronJob が専用 ConfigMap version-drift の report.json キーへ書いたものを "
             "集約する。status は ok / stale（最終観測が 36h より古い = 観測が止まっている）/ "
             "no_data（産出側未稼働・記録破損）。drifted に id / current / latest / upstream の列が並ぶ。"
        ),
    }

    put_configmap(
        namespace,
        name,
        {HEALTH_KEY: json.dumps(report, ensure_ascii=False, indent=2)},
    )
    print(
        "{}/{} の {} を更新しました ({})".format(namespace, name, HEALTH_KEY, generated_at)
    )


if __name__ == "__main__":
    main()
