#!/usr/bin/env python3
"""ops/ の状態から人間向けダッシュボード HTML を生成する。

    python3 ops/dashboard/build.py

出力: ops/dashboard/index.html。`AUTOPILOT_GITHUB_TOKEN` があれば
`ops-dashboard` ブランチの index.html へも push する（T-0127。クラスタ内常駐の
autopilot には Artifact ツールが無いため、ops-health-reporter が
ops-health-report ブランチへ書き戻すのと同型の経路で人間に公開する）。
token が無い環境（CI、手元実行）では push をスキップし、HTML の生成自体は
従来どおり成功として扱う。

設計方針:
  - 主役は「人間が何をすればいいか」と「何が動いているか」。個々のタスクは既定で隠す
  - 数はすべてこのファイルが backlog.json から数える。文章側で数えない（食い違いの元）
  - PC では 2 カラム、狭い画面では 1 カラム
  - 依存関係・進捗・時間推移は図で示す

標準ライブラリのみ。入力が同じなら出力も同じ（生成時刻を除く）。
"""

from __future__ import annotations

import base64
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

OPS = pathlib.Path(__file__).resolve().parent.parent
ROOT = OPS.parent
OUT = OPS / "dashboard" / "index.html"
REPO = "hikuohiku/homelab"
API = "https://api.github.com"

E = html.escape
STALE: dict[str, str] = {}

STATUS_META = {
    "todo": ("待ち", "idle"),
    "in_progress": ("作業中", "sig"),
    "blocked": ("詰まり", "warn"),
    "needs-human": ("あなた待ち", "crit"),
    "done": ("完了", "ok"),
    "dropped": ("取り下げ", "idle"),
}

# 大きい粒度の「流れ」。色は識別のみを担う（dataviz 参照パレット先頭6スロット、light/dark 検証済み）。
# light では一部が対サーフェス 3:1 未満のため、必ず可視ラベルを併記して色だけに頼らない。
STREAMS = [
    ("kiki", "器をつくる", ("meta", "feature"), "自律運用の土台そのもの"),
    ("kenshou", "検証を固める", ("ci",), "壊れた変更を機械的に止める"),
    ("tsuijuu", "版に追従する", ("upgrade",), "依存を塩漬けにしない"),
    ("seiri", "食い違いを直す", ("refactor", "docs"), "記録と実態のずれを潰す"),
    ("anzen", "安全を上げる", ("security",), "秘密と権限の扱い"),
    ("chousa", "観測して探す", ("investigate",), "見えていないものを見る"),
]
KIND_TO_STREAM = {k: s[0] for s in STREAMS for k in s[2]}
OTHER_STREAM = ("other", "運用記録", (), "journal とダッシュボード")
ALL_STREAMS = STREAMS + [OTHER_STREAM]


def stream_of(task: dict) -> str:
    return task.get("stream") or KIND_TO_STREAM.get(task.get("kind", ""), "other")


def load(name: str, default=None):
    p = OPS / name
    if not p.exists():
        return default
    return json.loads(p.read_text())


def load_health() -> dict | None:
    """homelab から書き戻された健全性レポート（別ブランチにある）。"""
    for ref in ("origin/ops-health-report", "ops-health-report"):
        try:
            out = subprocess.run(
                ["git", "show", f"{ref}:ops/health/latest.json"],
                cwd=ROOT, capture_output=True, text=True, timeout=30, check=True,
            ).stdout
            return json.loads(out)
        except Exception:  # noqa: BLE001
            continue
    return None


MERGED_LIMIT = 60


def _gh_api(path: str) -> list | dict:
    """GitHub REST API を直叩きする。`gh` CLI はこの実行環境に無い（CHARTER §5.2/§5.5）。"""
    token = os.environ.get("AUTOPILOT_GITHUB_TOKEN")
    if not token:
        raise RuntimeError("AUTOPILOT_GITHUB_TOKEN is not set")
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "homelab-autopilot-dashboard",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read())


def _status_rollup(sha: str) -> list[dict]:
    """CI 状態は check-runs と status の両方を見る（外部 App は片方にしか出ないことがある、CHARTER §5.5）。"""
    rollup = []
    try:
        runs = _gh_api(f"/repos/{REPO}/commits/{sha}/check-runs?per_page=100")
        for r in runs.get("check_runs", []):
            rollup.append({"conclusion": (r.get("conclusion") or r.get("status") or "").upper()})
    except Exception:  # noqa: BLE001
        pass
    try:
        status = _gh_api(f"/repos/{REPO}/commits/{sha}/status")
        for s in status.get("statuses", []):
            rollup.append({"conclusion": (s.get("state") or "").upper()})
    except Exception:  # noqa: BLE001
        pass
    return rollup


def _open_prs() -> list[dict]:
    raw = _gh_api(f"/repos/{REPO}/pulls?state=open&per_page=100")
    return [
        {
            "number": p["number"],
            "title": p["title"],
            "url": p["html_url"],
            "isDraft": p.get("draft", False),
            "createdAt": p.get("created_at"),
            "statusCheckRollup": _status_rollup(p["head"]["sha"]),
            "headRefName": p.get("head", {}).get("ref", ""),
            "autoMergeRequest": p.get("auto_merge"),
        }
        for p in raw
    ]


def _merged_prs(limit: int) -> list[dict]:
    # state=closed には未マージの close も混ざるので merged_at で絞る。sort=created&direction=desc
    # で新しい方から辿れば、通常運用（作成後すぐマージ）では数ページで limit 件に届く。
    merged = []
    for page in range(1, 6):
        batch = _gh_api(f"/repos/{REPO}/pulls?state=closed&sort=created&direction=desc&per_page=100&page={page}")
        if not batch:
            break
        merged.extend(p for p in batch if p.get("merged_at"))
        if len(batch) < 100 or len(merged) >= limit:
            break
    merged.sort(key=lambda p: p["merged_at"], reverse=True)
    return [
        {
            "number": p["number"],
            "title": p["title"],
            "url": p["html_url"],
            "mergedAt": p["merged_at"],
            "headRefName": p.get("head", {}).get("ref", ""),
        }
        for p in merged[:limit]
    ]


def fetch_prs() -> tuple[list[dict], list[dict]]:
    cache = OPS / "dashboard" / "prs.json"
    try:
        data = {"open": _open_prs(), "merged": _merged_prs(MERGED_LIMIT)}
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return data["open"], data["merged"]
    except Exception as e:  # noqa: BLE001
        print(f"warning: PR 一覧を取得できずキャッシュを使う ({e})", file=sys.stderr)
        STALE["reason"] = str(e)[:100]
        if cache.exists():
            STALE["at"] = datetime.fromtimestamp(cache.stat().st_mtime, timezone.utc).isoformat()
            data = json.loads(cache.read_text())
            if isinstance(data, list):
                return data, []
            return data.get("open", []), data.get("merged", [])
        return [], []


def parse_journal() -> list[dict]:
    entries = []
    for f in sorted((OPS / "journal").glob("*.md"), reverse=True):
        for b in re.split(r"^## ", f.read_text(), flags=re.M)[1:]:
            head, _, body = b.partition("\n")
            entries.append({"head": head.strip(), "body": body.strip()})
    return entries


def rel_time(iso) -> str:
    if not iso:
        return "—"
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    s = int((datetime.now(timezone.utc) - t).total_seconds())
    if s < 0:
        return "たった今"
    if s < 3600:
        return f"{s // 60} 分前"
    if s < 86400:
        return f"{s // 3600} 時間前"
    return f"{s // 86400} 日前"


def ci_state(pr: dict) -> tuple[str, str]:
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "idle", "CI 未実行"
    c = [x.get("conclusion") or x.get("state") or "" for x in rollup]
    if any(v in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for v in c):
        return "crit", "CI 失敗"
    if any(v in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED") for v in c):
        return "warn", "CI 実行中"
    return "ok", "CI green"


def human_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.0f} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return "—"


def chip(text: str, tone: str) -> str:
    return f'<span class="chip chip--{tone}">{E(text)}</span>'


def bar(segments, total: int) -> str:
    if not total:
        return ""
    out = []
    for n, tone, label in segments:
        if not n:
            continue
        out.append(f'<span class="seg seg--{tone}" style="width:{n / total * 100:.4f}%" '
                   f'title="{E(label)} {n} 件"></span>')
    return f'<span class="bar">{"".join(out)}</span>'


def _blocked_ids(t: dict):
    return re.findall(r"T-\d{4}", str(t.get("blocked_by", "")))


def render_asks(tasks):
    asks = [t for t in tasks if t["status"] == "needs-human"]
    if not asks:
        return ('<li><p class="empty">いまお願いしたいことはありません。'
                'autopilot が自分で進められる状態です。</p></li>', 0)
    blocked = [t for t in tasks if t["status"] == "blocked"]

    def unblocks(tid):
        direct = [b for b in blocked if tid in _blocked_ids(b)]
        indirect = []
        for d in direct:
            indirect += [b for b in blocked
                         if d["id"] in _blocked_ids(b) and b not in direct and b not in indirect]
        return direct + indirect

    rows = []
    for t in sorted(asks, key=lambda x: (-len(unblocks(x["id"])), x.get("priority", 999))):
        un = unblocks(t["id"])
        ha = t.get("human_action") or {}
        why = ha.get("why") or t.get("needs_human_reason") or t.get("why", "")
        steps = ha.get("steps") or []
        links = ha.get("links") or []
        effect = (f'<span class="ask__effect">これが済むと <b>{len(un)} 件</b>が動き出します</span>'
                  if un else '<span class="ask__effect ask__effect--none">他を止めてはいません</span>')
        if steps:
            steps_html = ('<details class="ask__steps"><summary>手順を開く'
                          f'（{len(steps)} ステップ）</summary><ol>'
                          + "".join(f"<li>{s}</li>" for s in steps) + "</ol></details>")
        else:
            steps_html = ('<p class="ask__nosteps">手順が未整理です。autopilot に '
                          '<code>human_action.steps</code> を書かせてください。</p>')
        links_html = ("<p class=\"ask__links\">" + " ".join(
            f'<a href="{E(l["url"])}">{E(l["label"])}</a>' for l in links) + "</p>") if links else ""
        unlist = ""
        if un:
            unlist = ('<p class="ask__unblocks">解けるもの: '
                      + "、".join(E(b["title"][:24]) for b in un[:4])
                      + (f"　ほか {len(un) - 4} 件" if len(un) > 4 else "") + "</p>")
        rows.append(f"""
      <li class="ask">
        <div class="ask__head"><span class="ask__id">{E(t['id'])}</span>
          <h3 class="ask__title">{E(ha.get('summary') or t['title'])}</h3></div>
        {effect}
        <p class="ask__why">{E(why)}</p>
        {steps_html}{links_html}{unlist}
      </li>""")
    return "".join(rows), len(asks)


def render_chain(tasks):
    blocked = [t for t in tasks if t["status"] == "blocked"]
    asks = [t for t in tasks if t["status"] == "needs-human"]
    if not blocked:
        return '<p class="empty">詰まっているものはありません。</p>'
    roots = []
    for a in asks:
        direct = [b for b in blocked if a["id"] in _blocked_ids(b)]
        if direct:
            roots.append((a, direct))
    if not roots:
        return '<p class="empty">人間待ちが原因の詰まりはありません。</p>'
    cols = []
    for a, direct in sorted(roots, key=lambda x: -len(x[1])):
        mids = []
        total = len(direct)
        for d in direct:
            kids = [b for b in blocked if d["id"] in _blocked_ids(b)]
            total += len(kids)
            leaves = "".join(f'<span class="chain__leaf">{E(k["title"][:28])}</span>' for k in kids)
            mids.append(f'<div class="chain__mid"><span class="chain__node chain__node--blocked">'
                        f'{E(d["title"][:34])}</span>'
                        + (f'<div class="chain__leaves">{leaves}</div>' if kids else "") + "</div>")
        ha = a.get("human_action") or {}
        cols.append(f"""
      <div class="chain">
        <div class="chain__root"><span class="chain__badge">あなた</span>
          <span class="chain__node chain__node--ask">{E(ha.get('summary') or a['title'][:34])}</span>
          <span class="chain__count">→ {total} 件が解ける</span></div>
        <div class="chain__mids">{''.join(mids)}</div>
      </div>""")
    return f'<div class="chains">{"".join(cols)}</div>'


def render_projects(tasks):
    rows = []
    for sid, label, _k, blurb in ALL_STREAMS:
        mine = [t for t in tasks if stream_of(t) == sid]
        if not mine:
            continue
        c = {k: sum(1 for t in mine if t["status"] == k) for k in STATUS_META}
        total = len(mine)
        live = c["todo"] + c["in_progress"] + c["blocked"] + c["needs-human"]
        segs = [(c["done"], "ok", "完了"), (c["dropped"], "idle", "取り下げ"),
                (c["in_progress"], "sig", "作業中"), (c["todo"], "idle", "待ち"),
                (c["blocked"], "warn", "詰まり"), (c["needs-human"], "crit", "あなた待ち")]
        state = ("完了" if not live else f"あなた待ち {c['needs-human']}" if c["needs-human"]
                 else f"詰まり {c['blocked']}" if c["blocked"] else f"進行中 {live}")
        tone = ("ok" if not live else "crit" if c["needs-human"]
                else "warn" if c["blocked"] else "sig")
        rows.append(f"""
      <li class="proj"><span class="proj__dot proj__dot--{sid}" aria-hidden="true"></span>
        <div class="proj__body">
          <div class="proj__head"><h3>{E(label)}</h3>{chip(state, tone)}</div>
          <p class="proj__blurb">{E(blurb)}</p>
          {bar(segs, total)}
          <p class="proj__n">{c['done']} / {total} 完了</p>
        </div></li>""")
    return "".join(rows)


def render_health(h):
    if not h:
        return ('<p class="empty">homelab からの状態がまだ届いていません。'
                '<code>ops-health-report</code> ブランチを確認してください。</p>')
    apps = h.get("applications", []) or []
    bad = [a for a in apps if a.get("sync") != "Synced" or a.get("health") != "Healthy"]
    issues = h.get("pod_issues", []) or []
    nodes = h.get("nodes") or []
    nm = h.get("node_metrics") or []
    node_html = ""
    if nodes:
        cap = (nodes[0].get("capacity") or {})

        def ki(v):
            try:
                return int(str(v).replace("Ki", "")) * 1024
            except (TypeError, ValueError):
                return 0

        mem_cap, disk_cap = ki(cap.get("memory")), ki(cap.get("ephemeral-storage"))
        try:
            cpu_cap = float(cap.get("cpu") or 0)
        except (TypeError, ValueError):
            cpu_cap = 0.0
        mem_used = ki(nm[0].get("memory")) if nm else 0
        try:
            cpu_used = int(str(nm[0]["cpu"]).replace("n", "")) / 1e9 if nm else 0.0
        except (TypeError, ValueError, KeyError):
            cpu_used = 0.0
        pvc_bytes = 0
        for grp in h.get("pvc_usage") or []:
            for u in (grp.get("usage") or []):
                try:
                    pvc_bytes += int(u.get("bytes") or 0)
                except (TypeError, ValueError):
                    pass

        def meter(label, used, cap_, extra):
            pct = (used / cap_ * 100) if cap_ else 0
            tone = "crit" if pct > 85 else "warn" if pct > 70 else "ok"
            return (f'<div class="meter"><div class="meter__top"><span>{E(label)}</span>'
                    f'<span class="meter__v">{E(extra)}</span></div>'
                    f'<span class="meter__track"><span class="meter__fill meter__fill--{tone}" '
                    f'style="width:{min(pct, 100):.1f}%"></span></span></div>')

        node_html = (meter("CPU", cpu_used, cpu_cap, f"{cpu_used:.2f} / {cpu_cap:.0f} コア")
                     + meter("メモリ", mem_used, mem_cap,
                             f"{human_bytes(mem_used)} / {human_bytes(mem_cap)}")
                     + meter("アプリのデータ", pvc_bytes, disk_cap,
                             f"{human_bytes(pvc_bytes)} / {human_bytes(disk_cap)}"))
    issue_html = ""
    if issues:
        issue_html = ('<details class="hl__issues"><summary>再起動の多い Pod '
                      f'{len(issues)} 件</summary><ul>'
                      + "".join(f'<li>{E(str(p.get("namespace", "")))}/'
                                f'{E(str(p.get("name", ""))[:32])} '
                                f'<span>{E(str(p.get("restarts", "?")))} 回</span></li>'
                                for p in issues[:10]) + "</ul></details>")
    return (f'<p class="hl__apps">'
            f'{chip(f"アプリ {len(apps) - len(bad)}/{len(apps)} 正常", "ok" if not bad else "crit")}'
            f'<span class="hl__at">{E(rel_time(h.get("generated_at")))}の状態</span></p>'
            f"{render_autopilot_self(h)}{node_html}{issue_html}")


def render_autopilot_self(h):
    """T-0110: autopilot 自身（namespace autopilot）が report.py の autopilot キーで返す
    readyReplicas とハートビート（loop.sh の心拍ログ）から、静かなハング/異常終了を示す。"""
    ap = (h or {}).get("autopilot") or {}
    if not ap or "error" in ap:
        return ""
    dep = ap.get("deployment") or {}
    hb = ap.get("heartbeat") or {}
    last_start, last_end = hb.get("last_start"), hb.get("last_end")

    tone, msgs = "ok", []
    if (dep.get("readyReplicas") or 0) < 1:
        tone = "crit"
        msgs.append("readyReplicas 0")

    running = last_start and (not last_end or last_start["iteration"] > last_end["iteration"])
    if running:
        started = last_start["timestamp"]
        try:
            elapsed = int(
                (datetime.now(timezone.utc)
                 - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
            )
        except ValueError:
            elapsed = None
        if elapsed is not None and elapsed > 3700:
            tone = "crit"
            msgs.append(f"iteration #{last_start['iteration']} が {elapsed // 60} 分実行中"
                        " (timeout 3600s 超、ハングの疑い)")
        else:
            msgs.append(f"iteration #{last_start['iteration']} 実行中（開始 {rel_time(started)}）")
    elif last_end:
        if last_end.get("exit_code") not in (0, None):
            tone = "crit" if tone != "crit" else tone
            msgs.append(f"iteration #{last_end['iteration']} exit={last_end['exit_code']}"
                        f"（{rel_time(last_end['timestamp'])}）")
        else:
            msgs.append(f"iteration #{last_end['iteration']} 正常終了（{rel_time(last_end['timestamp'])}）")
    else:
        tone = "warn" if tone == "ok" else tone
        msgs.append("心拍ログがまだ無い")

    return f'<p class="hl__apps">{chip("autopilot " + " / ".join(msgs), tone)}</p>'


def render_gantt(tasks, merged, runs):
    pr2stream = {}
    for t in tasks:
        if t.get("pr"):
            try:
                pr2stream[int(t["pr"])] = stream_of(t)
            except (TypeError, ValueError):
                pass
    marks = []
    for p in merged:
        ts = p.get("mergedAt")
        if not ts:
            continue
        try:
            marks.append((datetime.fromisoformat(ts.replace("Z", "+00:00")),
                          pr2stream.get(p["number"], "other"), p))
        except ValueError:
            pass
    if not marks:
        return ""
    run_times = []
    for r in runs:
        try:
            run_times.append(datetime.fromisoformat(str(r.get("at", "")).replace("Z", "+00:00")))
        except ValueError:
            pass
    t0 = min([m[0] for m in marks] + run_times)
    t1 = datetime.now(timezone.utc)
    span = max((t1 - t0).total_seconds(), 1)

    def x(w):
        return max(0.0, min(100.0, (w - t0).total_seconds() / span * 100))

    ticks, cur = [], t0.replace(minute=0, second=0, microsecond=0)
    while cur <= t1:
        if cur >= t0:
            ticks.append(f'<span class="gtick" style="left:{x(cur):.3f}%"><i></i>'
                         f'<em>{cur.strftime("%H:%M")}</em></span>')
        cur = datetime.fromtimestamp(cur.timestamp() + 6 * 3600, tz=timezone.utc)
    lanes = []
    for sid, label, _k, _b in ALL_STREAMS:
        mine = [m for m in marks if m[1] == sid]
        if not mine:
            continue
        dots = "".join(f'<span class="gmark gmark--{sid}" style="left:{x(w):.3f}%" '
                       f'title="#{p["number"]} {E(p["title"])}"></span>'
                       for w, _s, p in sorted(mine, key=lambda z: z[0]))
        lanes.append(f'<div class="glane"><span class="glane__label">'
                     f'<span class="proj__dot proj__dot--{sid}" aria-hidden="true"></span>'
                     f'{E(label)}</span><span class="glane__track">{dots}</span>'
                     f'<span class="glane__n">{len(mine)}</span></div>')
    runs_html = "".join(f'<span class="grun" style="left:{x(w):.3f}%"></span>'
                        for w in sorted(run_times))
    return (f'<div class="gantt"><div class="glane gantt__runs">'
            f'<span class="glane__label">起動</span>'
            f'<span class="glane__track">{runs_html}</span>'
            f'<span class="glane__n">{len(run_times)}</span></div>{"".join(lanes)}'
            f'<div class="glane gantt__axis"><span class="glane__label"></span>'
            f'<span class="glane__track">{"".join(ticks)}</span>'
            f'<span class="glane__n"></span></div></div>')


def render_detail(tasks):
    rows = []
    order = {"needs-human": 0, "blocked": 1, "in_progress": 2, "todo": 3, "done": 4, "dropped": 5}
    for t in sorted(tasks, key=lambda x: (order.get(x["status"], 9), x.get("priority", 999), x["id"])):
        label, tone = STATUS_META.get(t["status"], (t["status"], "idle"))
        sid = stream_of(t)
        sl = next((s[1] for s in ALL_STREAMS if s[0] == sid), sid)
        pr = (f'<a href="https://github.com/hikuohiku/homelab/pull/{t["pr"]}">#{t["pr"]}</a>'
              if t.get("pr") else "")
        note = t.get("needs_human_reason") or t.get("blocked_by") or ""
        rows.append(f'<tr data-status="{E(t["status"])}">'
                    f'<td class="dt__id">{E(t["id"])}</td><td>{chip(label, tone)}</td>'
                    f'<td class="dt__stream">{E(sl)}</td>'
                    f'<td class="dt__title">{E(t["title"])}'
                    + (f'<span class="dt__note">{E(str(note)[:110])}</span>' if note else "")
                    + f'</td><td class="dt__pr">{pr}</td></tr>')
    return "".join(rows)



def render_now(prs, health, runs, cadence_min):
    """いま何が動いているか。ページ内 JS で経過時間を刻む。"""
    items = []
    for p in prs:
        tone, label = ci_state(p)
        items.append(f'<li class="now__row now__row--{tone}"><span class="now__k">PR #{p["number"]}</span>'
                     f'<span class="now__v">{E(p["title"][:44])}</span>'
                     f'{chip(label, tone)}</li>')
    jobs = (health or {}).get("running_jobs") or []
    for j in jobs:
        items.append(f'<li class="now__row now__row--sig"><span class="now__k">Job</span>'
                     f'<span class="now__v">{E(str(j))}</span>{chip("実行中", "sig")}</li>')
    if not items:
        items.append('<li class="now__row now__row--idle"><span class="now__v">'
                     'GitHub 上で動いているものはありません</span></li>')
    last = runs[-1] if runs else {}
    return (f'<ul class="now">{"".join(items)}</ul>'
            f'<p class="now__meta">'
            f'<span>最終起動 <b data-since="{E(str(last.get("at","")))}">—</b></span>'
            f'<span>次の定期起動まで <b id="nextrun">—</b></span>'
            f'<span>この画面は <b data-since="{datetime.now(timezone.utc).isoformat()}">—</b>の情報</span>'
            f'</p>')


def resolve_cadence(state):
    """人間に見せる実行間隔。無効化されたクラウド routine の頻度をそのまま出さない。"""
    routines = state.get("routines") or []
    active = [r for r in routines if r.get("enabled", True)]
    if active:
        return active[0].get("cron_human") or active[0].get("cron")
    loop_cfg = state.get("in_cluster_loop") or {}
    cadence = loop_cfg.get("interval_human")
    if not cadence:
        return None
    disabled = [r for r in routines if not r.get("enabled", True)]
    if disabled:
        backup = disabled[0].get("cron_human") or disabled[0].get("cron")
        if backup:
            cadence = f"{cadence} ・ バックストップ: {backup}"
    return cadence


def build() -> str:
    state = load("state.json", {}) or {}
    backlog = load("backlog.json", {"tasks": []}) or {"tasks": []}
    tasks = backlog["tasks"]
    prs, merged = fetch_prs()
    health = load_health()
    runs = state.get("runs", []) or []
    journal = parse_journal()

    counts = {k: sum(1 for t in tasks if t["status"] == k) for k in STATUS_META}
    asks_html, n_asks = render_asks(tasks)
    auto_merged = [p for p in merged if str(p.get("headRefName", "")).startswith("autopilot/")]
    last_run = runs[-1] if runs else {}
    cadence = resolve_cadence(state)
    fb = state.get("feedback", {}) or {}

    alive_tone, alive_text = "idle", "起動の記録がありません"
    try:
        gap = (datetime.now(timezone.utc) - datetime.fromisoformat(
            str(last_run.get("at")).replace("Z", "+00:00"))).total_seconds()
        alive_tone, alive_text = (("crit", "止まっているかも") if gap > 7200 else
                                  ("warn", "しばらく動きなし") if gap > 4500 else
                                  ("ok", "動いています"))
    except Exception:  # noqa: BLE001
        pass

    now_html = render_now(prs, health, runs, 60)
    stale_html = (f'<p class="stale">PR 一覧を取得できませんでした。表示は'
                  f'{E(rel_time(STALE.get("at")))}のキャッシュです。</p>') if STALE else ""

    runs_html = ""
    for e in journal[:3]:
        items = [E(re.sub(r"^\s*[-*]\s*", "", l)) for l in e["body"].splitlines()
                 if l.strip().startswith(("-", "*"))][:4]
        runs_html += (f'<li class="run"><h4>{E(e["head"][:44])}</h4><ul>'
                      + "".join(f"<li>{i[:110]}</li>" for i in items) + "</ul></li>")

    done_html = "".join(
        f'<li class="done"><a href="{E(p["url"])}">{E(p["title"][:54])}</a>'
        f'<span>#{p["number"]} · {E(rel_time(p.get("mergedAt")))}</span></li>'
        for p in (auto_merged or merged)[:8])

    return TEMPLATE.format(
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        stage=state.get("vision_stage", "?"), stage_label=E(state.get("vision_stage_label", "")),
        alive_tone=alive_tone, alive_text=E(alive_text),
        last_run=E(rel_time(last_run.get("at"))), cadence=E(cadence or "定期実行 未設定"),
        n_asks=n_asks, n_blocked=counts["blocked"], n_done=counts["done"],
        n_total=len(tasks), n_runs=len(runs),
        asks=asks_html, chain=render_chain(tasks), projects=render_projects(tasks),
        health=render_health(health), gantt=render_gantt(tasks, merged, runs),
        detail=render_detail(tasks), runs=runs_html, done=done_html,
        fb_url=E(fb.get("url") or "https://github.com/hikuohiku/homelab/issues"),
        fb_issue=E(str(fb.get("issue") or "?")), fb_read=E(rel_time(fb.get("last_read"))),
        stale=stale_html, now=now_html,
    )


# 完全な HTML 文書として出す。クラスタ内の ops-dashboard は python の http.server で
# 配信していて Content-Type に charset を付けないため、文書側に <meta charset> が無いと
# ブラウザが文字コードを推測して日本語が化ける（2026-08-06 に実際に化けた）。
# サーバ設定に依存させないよう、文書側で完結させる。
TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>autopilot — homelab 当直記録</title>
<style>
:root {{
  --ground:#eef0f4; --surface:#fbfcfe; --surface2:#f3f5f9; --line:#d8dce5;
  --ink:#161a21; --muted:#5b6473;
  --sig:#41528c; --sig-soft:#e3e7f4; --ok:#2c6f52; --ok-soft:#dfeee7;
  --warn:#8a5c0d; --warn-soft:#f6ebd7; --crit:#983737; --crit-soft:#f7e2e2;
  --idle:#6a7382; --idle-soft:#e6e9ef;
  --serif: ui-serif, "Iowan Old Style", "Source Serif 4", "Hiragino Mincho ProN", Georgia, serif;
  --sans: ui-sans-serif, system-ui, -apple-system, "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  --r:4px;
}}
.proj__dot--kiki,.gmark--kiki {{ background:#2a78d6; }}
.proj__dot--kenshou,.gmark--kenshou {{ background:#eb6834; }}
.proj__dot--tsuijuu,.gmark--tsuijuu {{ background:#1baf7a; }}
.proj__dot--seiri,.gmark--seiri {{ background:#eda100; }}
.proj__dot--anzen,.gmark--anzen {{ background:#e87ba4; }}
.proj__dot--chousa,.gmark--chousa {{ background:#008300; }}
.proj__dot--other,.gmark--other {{ background:#6a7382; }}
@media (prefers-color-scheme: dark) {{
  :root {{
    --ground:#0d0f14; --surface:#151922; --surface2:#1b202b; --line:#272d3a;
    --ink:#e6e9f0; --muted:#8e97a7;
    --sig:#96a6e0; --sig-soft:#1e2439; --ok:#63bd93; --ok-soft:#152a22;
    --warn:#dcac5e; --warn-soft:#2c2415; --crit:#ef9190; --crit-soft:#2f1c1e;
    --idle:#8e97a7; --idle-soft:#1c212b;
  }}
  .proj__dot--kiki,.gmark--kiki {{ background:#3987e5; }}
  .proj__dot--kenshou,.gmark--kenshou {{ background:#d95926; }}
  .proj__dot--tsuijuu,.gmark--tsuijuu {{ background:#199e70; }}
  .proj__dot--seiri,.gmark--seiri {{ background:#c98500; }}
  .proj__dot--anzen,.gmark--anzen {{ background:#d55181; }}
}}
:root[data-theme="dark"] {{
  --ground:#0d0f14; --surface:#151922; --surface2:#1b202b; --line:#272d3a;
  --ink:#e6e9f0; --muted:#8e97a7;
  --sig:#96a6e0; --sig-soft:#1e2439; --ok:#63bd93; --ok-soft:#152a22;
  --warn:#dcac5e; --warn-soft:#2c2415; --crit:#ef9190; --crit-soft:#2f1c1e;
  --idle:#8e97a7; --idle-soft:#1c212b;
}}
:root[data-theme="dark"] .proj__dot--kiki,:root[data-theme="dark"] .gmark--kiki{{background:#3987e5}}
:root[data-theme="dark"] .proj__dot--kenshou,:root[data-theme="dark"] .gmark--kenshou{{background:#d95926}}
:root[data-theme="dark"] .proj__dot--tsuijuu,:root[data-theme="dark"] .gmark--tsuijuu{{background:#199e70}}
:root[data-theme="dark"] .proj__dot--seiri,:root[data-theme="dark"] .gmark--seiri{{background:#c98500}}
:root[data-theme="dark"] .proj__dot--anzen,:root[data-theme="dark"] .gmark--anzen{{background:#d55181}}

*,*::before,*::after {{ box-sizing:border-box; }}
body,h1,h2,h3,h4,p,ul,ol,li,table,details {{ margin:0; padding:0; }}
ul,ol {{ list-style:none; }}
body {{ background:var(--ground); color:var(--ink); font-family:var(--sans); line-height:1.65;
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:2rem 1.1rem 4rem;
  display:flex; flex-direction:column; gap:1.5rem; }}
a {{ color:var(--sig); }}
.nowbox {{ background:var(--surface); border:1px solid var(--sig); border-radius:var(--r);
  padding:.8rem .95rem; }}
.now {{ display:flex; flex-direction:column; gap:.3rem; margin-top:.5rem; }}
.now__row {{ display:flex; align-items:center; gap:.5rem; font-size:.85rem;
  padding:.3rem .5rem; border-radius:var(--r); background:var(--surface2);
  border-left:3px solid var(--idle); }}
.now__row--ok{{border-left-color:var(--ok)}} .now__row--warn{{border-left-color:var(--warn)}}
.now__row--crit{{border-left-color:var(--crit)}} .now__row--sig{{border-left-color:var(--sig)}}
.now__row--idle {{ color:var(--muted); }}
.now__k {{ font-family:var(--mono); font-size:.75rem; color:var(--muted); white-space:nowrap; }}
.now__v {{ flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.now__meta {{ display:flex; flex-wrap:wrap; gap:.3rem 1.2rem; margin-top:.6rem;
  font-size:.78rem; color:var(--muted); font-family:var(--mono); }}
.now__meta b {{ color:var(--ink); font-weight:600; }}
.mast {{ border-bottom:2px solid var(--ink); padding-bottom:.85rem;
  display:flex; flex-wrap:wrap; align-items:baseline; gap:.5rem 1rem; }}
.mast h1 {{ font-family:var(--serif); font-size:clamp(1.45rem,3vw,1.95rem); font-weight:600;
  letter-spacing:-.01em; }}
.mast__meta {{ font-family:var(--mono); font-size:.76rem; color:var(--muted);
  display:flex; flex-wrap:wrap; gap:.3rem .9rem; margin-left:auto; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(132px,1fr)); gap:1px;
  background:var(--line); border:1px solid var(--line); border-radius:var(--r); overflow:hidden; }}
.stat {{ background:var(--surface); padding:.7rem .9rem; display:flex; flex-direction:column;
  gap:.08rem; border-top:3px solid var(--idle); }}
.stat--ok{{border-top-color:var(--ok)}} .stat--crit{{border-top-color:var(--crit)}}
.stat--warn{{border-top-color:var(--warn)}} .stat--sig{{border-top-color:var(--sig)}}
.stat__v {{ font-family:var(--mono); font-size:1.22rem; font-variant-numeric:tabular-nums;
  letter-spacing:-.02em; }}
.stat__l {{ font-size:.72rem; color:var(--muted); }}
.cols {{ display:grid; grid-template-columns:1fr; gap:1.5rem; }}
@media (min-width:920px) {{ .cols {{ grid-template-columns:1.6fr 1fr; align-items:start; }} }}
.col {{ display:flex; flex-direction:column; gap:1.5rem; min-width:0; }}
section {{ display:flex; flex-direction:column; gap:.65rem; }}
h2 {{ font-family:var(--serif); font-size:1.06rem; font-weight:600;
  display:flex; align-items:baseline; gap:.5rem; }}
h2::after {{ content:""; flex:1; height:1px; background:var(--line); }}
.lede {{ color:var(--muted); font-size:.82rem; margin-top:-.3rem; }}
.empty {{ color:var(--muted); font-size:.86rem; background:var(--surface);
  border:1px dashed var(--line); border-radius:var(--r); padding:.8rem .95rem; }}
.card {{ background:var(--surface); border:1px solid var(--line); border-radius:var(--r); }}
.chip {{ font-size:.71rem; padding:.08rem .46rem; border-radius:999px;
  background:var(--idle-soft); color:var(--idle); white-space:nowrap; }}
.chip--ok{{background:var(--ok-soft);color:var(--ok)}}
.chip--warn{{background:var(--warn-soft);color:var(--warn)}}
.chip--crit{{background:var(--crit-soft);color:var(--crit)}}
.chip--sig{{background:var(--sig-soft);color:var(--sig)}}
code {{ font-family:var(--mono); font-size:.75rem; background:var(--idle-soft);
  padding:.06rem .3rem; border-radius:2px; }}
.bar {{ display:flex; gap:2px; height:.48rem; }}
.seg {{ display:block; border-radius:2px; min-width:3px; }}
.seg--ok{{background:var(--ok)}} .seg--idle{{background:var(--idle)}}
.seg--warn{{background:var(--warn)}} .seg--crit{{background:var(--crit)}} .seg--sig{{background:var(--sig)}}
.asks {{ display:flex; flex-direction:column; gap:.65rem; }}
.ask {{ background:var(--surface); border:1px solid var(--crit); border-left:4px solid var(--crit);
  border-radius:var(--r); padding:.85rem .95rem; display:flex; flex-direction:column; gap:.4rem; }}
.ask__head {{ display:flex; gap:.45rem; align-items:baseline; flex-wrap:wrap; }}
.ask__id {{ font-family:var(--mono); font-size:.73rem; color:var(--muted); }}
.ask__title {{ font-size:.99rem; font-weight:600; text-wrap:balance; }}
.ask__effect {{ font-size:.8rem; color:var(--crit); background:var(--crit-soft);
  align-self:flex-start; padding:.12rem .55rem; border-radius:var(--r); }}
.ask__effect--none {{ color:var(--muted); background:var(--idle-soft); }}
.ask__why {{ font-size:.85rem; color:var(--muted); }}
.ask__steps summary {{ cursor:pointer; font-size:.84rem; font-weight:600; color:var(--sig);
  padding:.25rem 0; }}
.ask__steps ol {{ list-style:decimal; padding-left:1.35rem; display:flex; flex-direction:column;
  gap:.35rem; font-size:.86rem; margin-top:.25rem; }}
.ask__steps li::marker {{ color:var(--muted); font-family:var(--mono); font-size:.78rem; }}
.ask__nosteps {{ font-size:.81rem; color:var(--warn); }}
.ask__links a {{ font-size:.81rem; margin-right:.8rem; }}
.ask__unblocks {{ font-size:.75rem; color:var(--muted); }}
.chains {{ display:flex; flex-direction:column; gap:.7rem; }}
.chain {{ background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  padding:.75rem .85rem; display:flex; flex-direction:column; gap:.45rem; overflow-x:auto; }}
.chain__root {{ display:flex; align-items:center; gap:.45rem; flex-wrap:wrap; }}
.chain__badge {{ font-size:.67rem; background:var(--crit); color:var(--surface);
  padding:.06rem .45rem; border-radius:999px; }}
.chain__node {{ font-size:.83rem; padding:.24rem .55rem; border-radius:var(--r);
  border:1px solid var(--line); background:var(--surface2); }}
.chain__node--ask {{ border-color:var(--crit); background:var(--crit-soft); color:var(--crit);
  font-weight:600; }}
.chain__node--blocked {{ border-color:var(--warn); background:var(--warn-soft); color:var(--warn); }}
.chain__count {{ font-size:.77rem; color:var(--muted); font-family:var(--mono); }}
.chain__mids {{ display:flex; flex-direction:column; gap:.35rem; padding-left:1rem;
  border-left:2px solid var(--line); margin-left:.45rem; }}
.chain__mid {{ display:flex; flex-direction:column; gap:.25rem; }}
.chain__leaves {{ display:flex; flex-wrap:wrap; gap:.25rem; padding-left:1rem; }}
.chain__leaf {{ font-size:.74rem; color:var(--muted); background:var(--idle-soft);
  padding:.12rem .42rem; border-radius:var(--r); }}
.projs {{ display:flex; flex-direction:column; gap:.5rem; }}
.proj {{ display:flex; gap:.55rem; background:var(--surface); border:1px solid var(--line);
  border-radius:var(--r); padding:.65rem .85rem; }}
.proj__dot {{ width:.58rem; height:.58rem; border-radius:2px; margin-top:.42rem; flex:none; }}
.proj__body {{ flex:1; display:flex; flex-direction:column; gap:.25rem; min-width:0; }}
.proj__head {{ display:flex; align-items:baseline; gap:.45rem; flex-wrap:wrap; }}
.proj__head h3 {{ font-size:.92rem; font-weight:600; }}
.proj__blurb {{ font-size:.77rem; color:var(--muted); }}
.proj__n {{ font-size:.73rem; color:var(--muted); font-family:var(--mono);
  font-variant-numeric:tabular-nums; }}
.hl__apps {{ display:flex; align-items:center; gap:.45rem; flex-wrap:wrap; }}
.hl__at {{ font-size:.74rem; color:var(--muted); font-family:var(--mono); }}
.meter {{ display:flex; flex-direction:column; gap:.15rem; margin-top:.5rem; }}
.meter__top {{ display:flex; justify-content:space-between; font-size:.79rem; gap:.5rem; }}
.meter__v {{ font-family:var(--mono); font-size:.74rem; color:var(--muted);
  font-variant-numeric:tabular-nums; }}
.meter__track {{ height:.42rem; background:var(--idle-soft); border-radius:999px; overflow:hidden; }}
.meter__fill {{ display:block; height:100%; border-radius:999px; }}
.meter__fill--ok{{background:var(--ok)}} .meter__fill--warn{{background:var(--warn)}}
.meter__fill--crit{{background:var(--crit)}}
.hl__issues summary {{ cursor:pointer; font-size:.79rem; color:var(--muted); margin-top:.55rem; }}
.hl__issues ul {{ font-size:.76rem; color:var(--muted); font-family:var(--mono);
  display:flex; flex-direction:column; gap:.12rem; margin-top:.25rem; }}
.gantt {{ background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  padding:.7rem .85rem; display:flex; flex-direction:column; gap:.28rem; overflow-x:auto; }}
.glane {{ display:grid; grid-template-columns:8.2rem 1fr 1.9rem; align-items:center; gap:.5rem;
  min-width:28rem; }}
.glane__label {{ font-size:.77rem; display:flex; align-items:center; gap:.3rem; white-space:nowrap; }}
.glane__track {{ position:relative; height:.95rem; border-left:1px solid var(--line);
  border-right:1px solid var(--line); }}
.glane__track::before {{ content:""; position:absolute; inset:50% 0 auto 0; height:1px;
  background:var(--line); }}
.glane__n {{ font-family:var(--mono); font-size:.71rem; color:var(--muted); text-align:right; }}
.gmark {{ position:absolute; top:50%; transform:translate(-50%,-50%); width:6px; height:10px;
  border-radius:2px; box-shadow:0 0 0 2px var(--surface); }}
.grun {{ position:absolute; top:2px; bottom:2px; width:2px; transform:translateX(-50%);
  background:var(--muted); opacity:.45; }}
.gantt__axis .glane__track {{ border:none; }}
.gantt__axis .glane__track::before {{ display:none; }}
.gtick {{ position:absolute; top:0; transform:translateX(-50%); display:flex;
  flex-direction:column; align-items:center; }}
.gtick i {{ width:1px; height:4px; background:var(--line); }}
.gtick em {{ font-style:normal; font-family:var(--mono); font-size:.63rem; color:var(--muted); }}
.run {{ background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  padding:.55rem .8rem; }}
.run h4 {{ font-family:var(--mono); font-size:.75rem; color:var(--sig); }}
.run ul {{ font-size:.79rem; display:flex; flex-direction:column; gap:.12rem; margin-top:.2rem; }}
.run li {{ padding-left:.75rem; position:relative; color:var(--muted); }}
.run li::before {{ content:"›"; position:absolute; left:0; }}
.done {{ display:flex; flex-direction:column; gap:.05rem; padding:.42rem .7rem;
  background:var(--surface); border:1px solid var(--line); border-left:3px solid var(--ok);
  border-radius:var(--r); }}
.done a {{ font-size:.83rem; color:var(--ink); text-decoration:none; }}
.done a:hover {{ color:var(--sig); text-decoration:underline; }}
.done span {{ font-family:var(--mono); font-size:.69rem; color:var(--muted); }}
.fb {{ background:var(--sig-soft); border:1px solid var(--sig); border-radius:var(--r);
  padding:.95rem 1.05rem; display:flex; flex-direction:column; gap:.5rem; }}
.fb h2 {{ color:var(--sig); }} .fb h2::after {{ background:var(--sig); opacity:.3; }}
.fb p {{ font-size:.84rem; }}
.fb__cta {{ align-self:flex-start; background:var(--sig); color:var(--surface);
  padding:.42rem 1rem; border-radius:var(--r); text-decoration:none; font-weight:600;
  font-size:.85rem; }}
.fb__cta:focus-visible {{ outline:2px solid var(--ink); outline-offset:2px; }}
.fb__meta {{ font-family:var(--mono); font-size:.73rem; color:var(--muted); }}
.detail {{ background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  padding:.85rem .95rem; }}
.detail > summary {{ cursor:pointer; font-family:var(--serif); font-size:1rem; font-weight:600; }}
.filters {{ display:flex; flex-wrap:wrap; gap:.3rem; margin:.75rem 0 .5rem; }}
.filters button {{ font:inherit; font-size:.75rem; padding:.18rem .6rem; border-radius:999px;
  border:1px solid var(--line); background:var(--surface2); color:var(--muted); cursor:pointer; }}
.filters button[aria-pressed="true"] {{ background:var(--sig); color:var(--surface);
  border-color:var(--sig); }}
.filters button:focus-visible {{ outline:2px solid var(--ink); outline-offset:2px; }}
.tablewrap {{ overflow-x:auto; max-height:60vh; overflow-y:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:.81rem; }}
th,td {{ text-align:left; padding:.38rem .55rem; border-bottom:1px solid var(--line);
  vertical-align:top; }}
th {{ font-size:.71rem; color:var(--muted); background:var(--idle-soft); position:sticky; top:0; }}
.dt__id {{ font-family:var(--mono); font-size:.73rem; color:var(--muted); white-space:nowrap; }}
.dt__stream {{ white-space:nowrap; color:var(--muted); font-size:.75rem; }}
.dt__title {{ min-width:15rem; }}
.dt__note {{ display:block; font-size:.71rem; color:var(--muted); margin-top:.12rem; }}
.dt__pr {{ font-family:var(--mono); font-size:.73rem; white-space:nowrap; }}
tr[hidden] {{ display:none; }}
.detail__count {{ font-family:var(--mono); font-size:.73rem; color:var(--muted); }}
.stale {{ background:var(--warn-soft); color:var(--warn); border:1px solid var(--warn);
  border-radius:var(--r); padding:.45rem .8rem; font-size:.81rem; }}
footer {{ color:var(--muted); font-size:.75rem; font-family:var(--mono);
  border-top:1px solid var(--line); padding-top:.75rem; display:flex; flex-wrap:wrap;
  gap:.3rem 1rem; }}
@media (prefers-reduced-motion:reduce) {{ *{{transition:none!important;animation:none!important}} }}
</style>
</head>
<body>

<div class="wrap">
  <header class="mast">
    <h1>autopilot — homelab 当直記録</h1>
    <span class="chip chip--sig">段階 {stage}・{stage_label}</span>
    <span class="mast__meta"><span>{cadence}</span><span>生成 {generated}</span></span>
  </header>

  {stale}

  <div class="stats">
    <div class="stat stat--{alive_tone}"><span class="stat__v">{alive_text}</span>
      <span class="stat__l">ループの状態（最終起動 {last_run}）</span></div>
    <div class="stat stat--crit"><span class="stat__v">{n_asks}</span>
      <span class="stat__l">あなたにお願いしたいこと</span></div>
    <div class="stat stat--warn"><span class="stat__v">{n_blocked}</span>
      <span class="stat__l">詰まっているもの</span></div>
    <div class="stat stat--ok"><span class="stat__v">{n_done} / {n_total}</span>
      <span class="stat__l">完了したタスク</span></div>
    <div class="stat stat--sig"><span class="stat__v">{n_runs}</span>
      <span class="stat__l">これまでの起動</span></div>
  </div>

  <section class="nowbox">
    <h2>いま動いているもの</h2>
    {now}
  </section>

  <div class="cols">
    <div class="col">
      <section>
        <h2>あなたにお願いしたいこと</h2>
        <p class="lede">autopilot には手が届かない作業だけです。判断を求めることはありません。</p>
        <ul class="asks">{asks}</ul>
      </section>

      <section>
        <h2>何が何を止めているか</h2>
        <p class="lede">左のひとつが済むと、右の連鎖がまとめて動き出します。</p>
        {chain}
      </section>

      <section>
        <h2>いま進んでいること</h2>
        <p class="lede">大きい流れごとの進み具合。個々のタスクは下の「詳細」にあります。</p>
        <ul class="projs">{projects}</ul>
      </section>

      <section>
        <h2>これまでの流れ</h2>
        <p class="lede">印ひとつが homelab に反映された変更 1 件。横位置が時刻（UTC）。</p>
        {gantt}
      </section>
    </div>

    <div class="col">
      <section class="card" style="padding:.85rem .95rem">
        <h2>homelab の今</h2>
        {health}
      </section>

      <section class="fb">
        <h2>気づいたことを書く</h2>
        <p>殴り書きで構いません。進め方への指摘は憲章に、やってほしいことはタスクとして取り込まれます。
           読んだら 3 行以内で返信します。</p>
        <a class="fb__cta" href="{fb_url}">issue #{fb_issue} に書く</a>
        <span class="fb__meta">最後に読んだ: {fb_read}</span>
      </section>

      <section>
        <h2>最近やったこと</h2>
        <ul class="projs">{done}</ul>
      </section>

      <section>
        <h2>直近の起動</h2>
        <ul class="projs">{runs}</ul>
      </section>
    </div>
  </div>

  <details class="detail">
    <summary>タスクの詳細を開く（{n_total} 件）</summary>
    <div class="filters" role="group" aria-label="状態で絞り込む">
      <button type="button" data-f="all" aria-pressed="true">すべて</button>
      <button type="button" data-f="needs-human" aria-pressed="false">あなた待ち</button>
      <button type="button" data-f="blocked" aria-pressed="false">詰まり</button>
      <button type="button" data-f="todo" aria-pressed="false">待ち</button>
      <button type="button" data-f="in_progress" aria-pressed="false">作業中</button>
      <button type="button" data-f="done" aria-pressed="false">完了</button>
    </div>
    <p class="detail__count" id="dcount"></p>
    <div class="tablewrap">
      <table>
        <thead><tr><th>ID</th><th>状態</th><th>流れ</th><th>内容</th><th>PR</th></tr></thead>
        <tbody id="dbody">{detail}</tbody>
      </table>
    </div>
  </details>

  <footer>
    <span>hikuohiku/homelab</span>
    <a href="https://github.com/hikuohiku/homelab/blob/main/ops/VISION.md">VISION</a>
    <a href="https://github.com/hikuohiku/homelab/blob/main/ops/CHARTER.md">CHARTER</a>
    <a href="https://github.com/hikuohiku/homelab/tree/main/ops/journal">journal</a>
  </footer>
</div>

<script>
(function () {{
  var btns = Array.prototype.slice.call(document.querySelectorAll('.filters button'));
  var rows = Array.prototype.slice.call(document.querySelectorAll('#dbody tr'));
  var count = document.getElementById('dcount');
  function apply(f) {{
    var n = 0;
    rows.forEach(function (r) {{
      var show = f === 'all' || r.getAttribute('data-status') === f;
      r.hidden = !show;
      if (show) n++;
    }});
    if (count) count.textContent = n + ' 件を表示';
  }}
  btns.forEach(function (b) {{
    b.addEventListener('click', function () {{
      btns.forEach(function (o) {{ o.setAttribute('aria-pressed', String(o === b)); }});
      apply(b.getAttribute('data-f'));
    }});
  }});
  apply('all');
}})();

// 経過時間と次回起動までを 1 秒ごとに更新する（データは静的だが、鮮度は生きた表示にする）
(function () {{
  function fmt(sec) {{
    if (sec < 0) sec = 0;
    if (sec < 60) return Math.floor(sec) + ' 秒';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分';
    return Math.floor(sec / 3600) + ' 時間 ' + Math.floor((sec % 3600) / 60) + ' 分';
  }}
  var els = Array.prototype.slice.call(document.querySelectorAll('[data-since]'));
  var next = document.getElementById('nextrun');
  function tick() {{
    var now = Date.now();
    els.forEach(function (el) {{
      var t = Date.parse(el.getAttribute('data-since'));
      if (isNaN(t)) {{ el.textContent = '—'; return; }}
      el.textContent = fmt((now - t) / 1000) + '前';
    }});
    if (next) {{
      var d = new Date(now);
      var mins = d.getUTCMinutes(), secs = d.getUTCSeconds();
      var until = ((19 - mins + 60) % 60) * 60 - secs;
      if (until <= 0) until += 3600;
      next.textContent = fmt(until);
    }}
  }}
  tick();
  setInterval(tick, 1000);
}})();
</script>
</body>
</html>
"""


DASHBOARD_BRANCH = os.environ.get("DASHBOARD_BRANCH", "ops-dashboard")


def _gh_write(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "homelab-autopilot-dashboard",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def _ensure_branch(token: str, branch: str, base_branch: str = "main") -> None:
    status, _ = _gh_write("GET", f"/repos/{REPO}/git/ref/heads/{branch}", token)
    if status == 200:
        return
    status, base = _gh_write("GET", f"/repos/{REPO}/git/ref/heads/{base_branch}", token)
    if status != 200:
        raise RuntimeError(f"base branch ref の取得に失敗: {status} {base}")
    base_sha = base["object"]["sha"]
    status, resp = _gh_write(
        "POST", f"/repos/{REPO}/git/refs", token,
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    if status not in (200, 201):
        raise RuntimeError(f"branch 作成に失敗: {status} {resp}")


def publish(html_bytes: bytes) -> None:
    """生成した index.html を DASHBOARD_BRANCH へ push する（T-0127）。

    main へは直 push せず、ops-health-report と同じ「専用ブランチへの Contents API
    書き込み」パターンを使う。token が無ければ静かに何もしない（CI・手元実行対策）。
    """
    token = os.environ.get("AUTOPILOT_GITHUB_TOKEN")
    if not token:
        return
    _ensure_branch(token, DASHBOARD_BRANCH)
    status, existing = _gh_write(
        "GET", f"/repos/{REPO}/contents/index.html?ref={DASHBOARD_BRANCH}", token
    )
    sha = existing.get("sha") if status == 200 and isinstance(existing, dict) else None
    payload = {
        "message": f"dashboard {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "content": base64.b64encode(html_bytes).decode(),
        "branch": DASHBOARD_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    status, resp = _gh_write("PUT", f"/repos/{REPO}/contents/index.html", token, payload)
    if status not in (200, 201):
        raise RuntimeError(f"index.html の push に失敗: {status} {resp}")


if __name__ == "__main__":
    rendered = build()
    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")
    if os.environ.get("AUTOPILOT_GITHUB_TOKEN"):
        try:
            publish(rendered.encode("utf-8"))
        except Exception as e:  # noqa: BLE001 — push 失敗させても HTML 生成自体は成功のまま終える
            print(f"warning: {DASHBOARD_BRANCH} への publish に失敗 ({e})", file=sys.stderr)
        else:
            print(f"published to {DASHBOARD_BRANCH} branch")
