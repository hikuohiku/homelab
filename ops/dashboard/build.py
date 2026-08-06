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
  - この画面は 1 日数回、数秒だけ見られる。答えるのは 2 問だけ:
    「いま何が起きているか」「自分は何をすればいいか」
  - 主役は backlog の順番待ち。priority 昇順にそのまま並べる（backlog.json の
    着手順そのもの）。集計バーではなく行を出す。次に来るものが読めないと意味がない
  - 誰待ちか（あなた / autopilot / 条件）を行の形と色で示す。色だけに頼らず必ず語で書く
  - 済んだこと・全タスク・クラスタの細部は畳む
  - 書き置きフォームを同一オリジンの POST /feedback に出す。JS 無しで成立させる
  - 数はすべてこのファイルが backlog.json から数える。文章側で数えない（食い違いの元）

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

# 順番待ちに出す状態と、その並び順（priority が同値のときの安定化にも使う）
OPEN_STATUSES = ("in_progress", "needs-human", "todo", "blocked")

STATUS_META = {
    "todo": ("待ち", "idle"),
    "in_progress": ("作業中", "sig"),
    "blocked": ("詰まり", "warn"),
    "needs-human": ("あなた待ち", "crit"),
    "done": ("完了", "ok"),
    "dropped": ("取り下げ", "idle"),
}

# backlog の kind をそのまま出さず、人間の語彙に寄せる。色は割り当てない
# （識別色を増やすと、状態を示す赤/黄/緑が埋もれる）。
KIND_LABEL = {
    "investigate": "調査", "docs": "文書", "ci": "検証", "feature": "機能",
    "upgrade": "版上げ", "chore": "雑務", "refactor": "整理", "security": "安全",
    "meta": "器", "fix": "修正", "update": "版上げ", "infra": "基盤",
    "migrate": "移行", "verify": "確認",
}
RISK_LABEL = {"low": "小", "medium": "中", "high": "大"}


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


def ts(iso):
    """ISO8601 を datetime に。壊れていれば None。"""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def age_seconds(iso, ref: datetime | None = None) -> float | None:
    t = ts(iso)
    if t is None:
        return None
    return ((ref or datetime.now(timezone.utc)) - t).total_seconds()


def _span(sec: float) -> str:
    sec = max(sec, 0)
    if sec < 90:
        return f"{int(sec)} 秒"
    if sec < 5400:
        return f"{int(sec // 60)} 分"
    if sec < 172800:
        return f"{int(sec // 3600)} 時間"
    return f"{int(sec // 86400)} 日"


def rel_time(iso) -> str:
    sec = age_seconds(iso)
    if sec is None:
        return "—" if not iso else str(iso)
    if sec < 0:
        return "この後"
    return _span(sec) + "前"


def until_time(iso) -> str:
    sec = age_seconds(iso)
    if sec is None:
        return "—"
    return _span(-sec) + "後" if sec < 0 else "到来済み"


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


def parse_quantity(v) -> float:
    """k8s の数量表記（Ki / Mi / Gi / n / m / 素の数）をバイトまたはコア数に直す。"""
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTPE]i|[munkKMGT])?", s)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2) or ""
    factor = {
        "": 1.0, "Ki": 1024.0, "Mi": 1024.0**2, "Gi": 1024.0**3,
        "Ti": 1024.0**4, "Pi": 1024.0**5, "Ei": 1024.0**6,
        "n": 1e-9, "u": 1e-6, "m": 1e-3,
        "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
    }.get(unit, 1.0)
    return num * factor


_INLINE_OK = re.compile(r"</?(?:a|code|b|strong|em|i|br|span|kbd|small)(?:\s[^<>]*)?/?>",
                        re.IGNORECASE)


def safe_html(s: str) -> str:
    """human_action.steps は HTML を含んでよい（backlog.json の規約）。ただし
    `kubectl logs <pod>` のようなプレースホルダも書かれるため、既知のインライン
    タグ以外は文字として出す。素通しすると文書が壊れる（T-0112 の steps で実際に壊れた）。
    """
    out, last = [], 0
    for m in _INLINE_OK.finditer(str(s)):
        out.append(E(str(s)[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(E(str(s)[last:]))
    return "".join(out)


def chip(text: str, tone: str) -> str:
    return f'<span class="chip chip--{tone}">{E(text)}</span>'


def dot(tone: str) -> str:
    return f'<span class="dot dot--{tone}" aria-hidden="true"></span>'


def meter(label: str, used: float, cap: float, extra: str, tone: str | None = None) -> str:
    pct = (used / cap * 100) if cap else 0
    tone = tone or ("crit" if pct > 85 else "warn" if pct > 70 else "ok")
    return (f'<div class="meter"><div class="meter__top"><span>{E(label)}</span>'
            f'<span class="meter__v">{E(extra)}</span></div>'
            f'<span class="meter__track"><span class="meter__fill meter__fill--{tone}" '
            f'style="width:{min(pct, 100):.1f}%"></span></span></div>')


# ---------------------------------------------------------------- backlog


def _blocked_ids(t: dict) -> list[str]:
    return re.findall(r"T-\d{4}", str(t.get("blocked_by", "")))


def _reason_ids(t: dict) -> list[str]:
    """blocked_by に加えて needs_human_reason 側の T-xxxx も依存として拾う。"""
    ids = _blocked_ids(t)
    for extra in re.findall(r"T-\d{4}", str(t.get("needs_human_reason", ""))):
        if extra not in ids:
            ids.append(extra)
    return ids


def open_tasks(tasks: list[dict]) -> list[dict]:
    """順番待ち。backlog.json の規約どおり priority 昇順、同値は ID 順。"""
    live = [t for t in tasks if t.get("status") in OPEN_STATUSES]
    return sorted(live, key=lambda t: (t.get("priority", 999), t.get("id", "")))


def downstream(tasks: list[dict], tid: str) -> list[dict]:
    """tid が片づくと動き出すもの（推移的）。"""
    waiting = [t for t in tasks if t.get("status") in ("blocked", "needs-human")]
    found, frontier = [], [tid]
    while frontier:
        cur = frontier.pop()
        for w in waiting:
            if w in found or w.get("id") == tid:
                continue
            if cur in _reason_ids(w):
                found.append(w)
                frontier.append(w["id"])
    return found


def wait_state(t: dict, ids_in_queue: set[str]) -> tuple[str, str, str]:
    """(owner ラベル, tone, 待ち理由の一文) を返す。誰が動かすべきかを最初に置く。"""
    status = t.get("status")
    if status == "needs-human":
        ha = t.get("human_action") or {}
        why = ha.get("why") or t.get("needs_human_reason") or t.get("why") or ""
        return "あなた", "crit", str(why)
    if status == "in_progress":
        return "autopilot", "sig", "作業中"
    if status == "todo":
        nb = t.get("not_before")
        if nb is not None and (age_seconds(nb) or 0) < 0:
            # 返す文は呼び出し側でまとめてエスケープするので、ここでは素のまま
            return "時刻待ち", "idle", f"{nb} まで着手しない（{until_time(nb)}）"
        return "autopilot", "sig", "着手できる状態。次の巡回で取る"
    dep = _blocked_ids(t)
    reason = str(t.get("blocked_by") or "")
    known = [d for d in dep if d in ids_in_queue]
    if known:
        return "他タスク待ち", "warn", reason
    return "条件待ち", "warn", reason


def render_queue(tasks: list[dict]) -> tuple[str, dict]:
    live = open_tasks(tasks)
    ids_in_queue = {t["id"] for t in live}
    if not live:
        return ('<li class="q__empty">順番待ちは空です。'
                'autopilot が拾える仕事はいまありません。</li>', {"total": 0, "you": 0, "unblocks": 0})

    rows, you, unblocks_total = [], 0, 0
    for i, t in enumerate(live, 1):
        owner, tone, reason = wait_state(t, ids_in_queue)
        if t.get("status") == "needs-human":
            you += 1
        down = downstream(tasks, t["id"])
        if t.get("status") == "needs-human":
            unblocks_total += len(down)

        ha = t.get("human_action") or {}
        title = ha.get("summary") or t.get("title") or t["id"]

        impact = ""
        if down:
            names = "、".join(E(str(d.get("title", ""))[:26]) for d in down[:3])
            more = f"　ほか {len(down) - 3} 件" if len(down) > 3 else ""
            impact = (f'<p class="q__impact">これが済むと <b>{len(down)} 件</b>が動く'
                      f'<span class="q__impact__names">{names}{more}</span></p>')

        # blocked_by / needs_human_reason 中の T-xxxx は同じ画面内の行へ飛ばす
        def link_ids(s: str) -> str:
            out = E(s)
            for tid in sorted(set(re.findall(r"T-\d{4}", s)), reverse=True):
                if tid in ids_in_queue:
                    out = out.replace(tid, f'<a class="q__dep" href="#{tid}">{tid}</a>')
            return out

        reason_html = ""
        if reason:
            short = reason if len(reason) <= 150 else reason[:150] + "…"
            reason_html = f'<p class="q__why">{link_ids(short)}</p>'
            if len(reason) > 150:
                reason_html += (f'<details class="q__more"><summary>理由の続き</summary>'
                                f'<p>{link_ids(reason)}</p></details>')

        steps = ha.get("steps") or []
        links = ha.get("links") or []
        act = ""
        if t.get("status") == "needs-human":
            if steps:
                act = ('<details class="q__act" open><summary>あなたの手順'
                       f'（{len(steps)} ステップ）</summary><ol>'
                       + "".join(f"<li>{safe_html(s)}</li>" for s in steps) + "</ol>")
                if links:
                    act += ('<p class="q__links">' + " ".join(
                        f'<a href="{E(str(l.get("url", "")))}">{E(str(l.get("label") or l.get("url", "")))}</a>'
                        for l in links) + "</p>")
                act += "</details>"
            else:
                act = ('<p class="q__nosteps">手順がまだ書かれていません。'
                       'autopilot に <code>human_action.steps</code> を書かせてください。</p>')

        meta = [f'<span class="q__id">{E(t["id"])}</span>',
                f'<span>優先度 {E(str(t.get("priority", "—")))}</span>']
        if t.get("kind"):
            meta.append(f'<span>{E(KIND_LABEL.get(t["kind"], t["kind"]))}</span>')
        if t.get("risk"):
            meta.append(f'<span>リスク {E(RISK_LABEL.get(t["risk"], t["risk"]))}</span>')
        if t.get("pr"):
            meta.append(f'<a href="https://github.com/{REPO}/pull/{E(str(t["pr"]))}">'
                        f'PR #{E(str(t["pr"]))}</a>')

        rows.append(f"""
        <li class="q__row q__row--{tone}" id="{E(t['id'])}">
          <span class="q__rank" aria-hidden="true">{i}</span>
          <div class="q__main">
            <div class="q__head"><span class="q__owner q__owner--{tone}">{E(owner)}</span>
              <h3 class="q__title">{E(str(title))}</h3></div>
            <p class="q__meta">{"".join(meta)}</p>
            {reason_html}{impact}{act}
          </div>
        </li>""")
    summary = {"total": len(live), "you": you, "unblocks": unblocks_total}
    return "".join(rows), summary


def render_archive(tasks: list[dict]) -> str:
    rows = []
    order = {"needs-human": 0, "blocked": 1, "in_progress": 2, "todo": 3, "done": 4, "dropped": 5}
    for t in sorted(tasks, key=lambda x: (order.get(x.get("status"), 9),
                                          x.get("priority", 999), x.get("id", ""))):
        label, tone = STATUS_META.get(t.get("status"), (str(t.get("status")), "idle"))
        pr = (f'<a href="https://github.com/{REPO}/pull/{t["pr"]}">#{t["pr"]}</a>'
              if t.get("pr") else "")
        note = t.get("needs_human_reason") or t.get("blocked_by") or ""
        rows.append(f'<tr data-status="{E(str(t.get("status")))}">'
                    f'<td class="at__id">{E(t["id"])}</td><td>{chip(label, tone)}</td>'
                    f'<td class="at__kind">{E(KIND_LABEL.get(t.get("kind", ""), t.get("kind", "")))}</td>'
                    f'<td class="at__title">{E(str(t.get("title", "")))}'
                    + (f'<span class="at__note">{E(str(note)[:110])}</span>' if note else "")
                    + f'</td><td class="at__pr">{pr}</td></tr>')
    return "".join(rows)


# ---------------------------------------------------------------- pulse


def loop_state(state: dict, runs: list[dict]) -> tuple[str, str, str]:
    last = runs[-1] if runs else {}
    gap = age_seconds(last.get("at"))
    if gap is None:
        return "idle", "起動の記録なし", "—"
    tone = "crit" if gap > 7200 else "warn" if gap > 4500 else "ok"
    text = {"ok": "動いています", "warn": "しばらく動きなし", "crit": "止まっているかも"}[tone]
    # runs 配列は古い分が間引かれるので通算回数は最後の n を使う（len では過少になる）
    total = last.get("n") if isinstance(last.get("n"), int) else len(runs)
    return tone, text, f"最終起動 {rel_time(last.get('at'))}・通算 {total} 回"


def health_freshness(h: dict | None) -> tuple[str, str]:
    """レポート自体の鮮度。古いレポートを『いまの状態』として読ませない。"""
    if not h:
        return "crit", "レポート未着"
    sec = age_seconds(h.get("generated_at"))
    if sec is None:
        return "warn", "生成時刻が読めない"
    tone = "crit" if sec > 10800 else "warn" if sec > 5400 else "ok"
    return tone, f"{rel_time(h.get('generated_at'))}の観測"


def render_pulse(state, health, prs, runs) -> str:
    cells = []

    tone, text, sub = loop_state(state, runs)
    cells.append(f'<div class="pulse__cell"><p class="pulse__k">autopilot ループ</p>'
                 f'<p class="pulse__v">{dot(tone)}{E(text)}</p>'
                 f'<p class="pulse__s">{E(sub)}</p></div>')

    apps = (health or {}).get("applications") or []
    bad = [a for a in apps if a.get("sync") != "Synced" or a.get("health") != "Healthy"]
    ftone, fresh = health_freshness(health)
    if apps:
        atone = "crit" if bad else "ok"
        atext = f"{len(apps) - len(bad)} / {len(apps)} 正常"
        asub = ("落ちている: " + "、".join(E(str(a.get("name"))) for a in bad[:4])) if bad else fresh
        if bad and ftone != "ok":
            asub += f"（{fresh}）"
    else:
        atone, atext, asub = "warn", "状態が届いていない", fresh
    cells.append(f'<div class="pulse__cell"><p class="pulse__k">homelab のアプリ</p>'
                 f'<p class="pulse__v">{dot(atone)}{E(atext)}</p>'
                 f'<p class="pulse__s">{asub}</p></div>')

    if prs:
        worst = "ok"
        for p in prs:
            t, _ = ci_state(p)
            if t == "crit" or (t == "warn" and worst != "crit"):
                worst = t
        ptext = f"{len(prs)} 件が審査中"
    else:
        worst, ptext = "idle", "なし"
    cells.append(f'<div class="pulse__cell"><p class="pulse__k">出している変更</p>'
                 f'<p class="pulse__v">{dot(worst)}{E(ptext)}</p>'
                 f'<p class="pulse__s">CI が通れば自分でマージします</p></div>')

    ap = render_autopilot_self(health)
    cells.append(ap)

    pr_rows = ""
    if prs:
        items = []
        for p in prs:
            tone, label = ci_state(p)
            items.append(f'<li class="pr pr--{tone}">{dot(tone)}'
                         f'<a class="pr__t" href="{E(str(p.get("url", "")))}">'
                         f'{E(str(p.get("title", ""))[:70])}</a>'
                         f'<span class="pr__n">#{E(str(p.get("number")))}</span>'
                         f'{chip(label, tone)}</li>')
        pr_rows = f'<ul class="prs">{"".join(items)}</ul>'
    return f'<div class="pulsebox"><div class="pulse">{"".join(cells)}</div>{pr_rows}</div>'


def render_autopilot_self(h) -> str:
    """T-0110: autopilot 自身（namespace autopilot）の readyReplicas と心拍。

    経過時間はレポートの generated_at を基準に測る。now() を基準にすると、
    レポートが古いだけでハング扱いになる（2026-08-06 に実際に誤検知した）。
    """
    ap = (h or {}).get("autopilot") or {}
    if not ap or "error" in ap:
        return ('<div class="pulse__cell"><p class="pulse__k">autopilot 自身</p>'
                '<p class="pulse__v">' + dot("idle") + '観測なし</p>'
                '<p class="pulse__s">健全性レポートに autopilot キーがありません</p></div>')
    ref = ts(h.get("generated_at")) or datetime.now(timezone.utc)
    dep = ap.get("deployment") or {}
    hb = ap.get("heartbeat") or {}
    start, end = hb.get("last_start"), hb.get("last_end")

    tone, text, sub = "ok", "常駐しています", ""
    if (dep.get("readyReplicas") or 0) < 1:
        tone, text = "crit", "Pod が上がっていない"
        sub = f"readyReplicas {dep.get('readyReplicas', 0)} / {dep.get('replicas', '?')}"
    elif start and (not end or start.get("iteration", 0) > end.get("iteration", -1)):
        elapsed = age_seconds(start.get("timestamp"), ref)
        if elapsed is not None and elapsed > 3700:
            tone, text = "crit", "ハングの疑い"
            sub = (f"#{start.get('iteration')} が観測時点で {int(elapsed // 60)} 分実行中"
                   "（timeout 3600 秒を超過）")
        else:
            text = f"#{start.get('iteration')} を実行中"
            sub = f"開始 {rel_time(start.get('timestamp'))}"
    elif end:
        if end.get("exit_code") not in (0, None):
            tone, text = "crit", f"#{end.get('iteration')} が異常終了"
            sub = f"exit={end.get('exit_code')}・{rel_time(end.get('timestamp'))}"
        else:
            text = f"#{end.get('iteration')} まで正常"
            sub = f"{end.get('elapsed_seconds', '?')} 秒で終了・{rel_time(end.get('timestamp'))}"
    else:
        tone, text, sub = "warn", "心拍がまだ無い", "loop.sh の心拍ログが読めていません"

    ftone, fresh = health_freshness(h)
    if ftone != "ok":
        sub = f"{sub}（{fresh}）" if sub else fresh
    return (f'<div class="pulse__cell"><p class="pulse__k">autopilot 自身</p>'
            f'<p class="pulse__v">{dot(tone)}{E(text)}</p>'
            f'<p class="pulse__s">{E(sub)}</p></div>')


# ---------------------------------------------------------------- cluster


def render_cluster(h) -> str:
    if not h:
        return ('<p class="empty">homelab からの状態がまだ届いていません。'
                '<code>ops-health-report</code> ブランチを確認してください。</p>')
    out = []

    apps = h.get("applications") or []
    if apps:
        cells = []
        for a in sorted(apps, key=lambda x: str(x.get("name"))):
            good = a.get("sync") == "Synced" and a.get("health") == "Healthy"
            t = "ok" if good else "crit"
            st = "正常" if good else f'{a.get("health")}/{a.get("sync")}'
            cells.append(f'<li class="app app--{t}">{dot(t)}'
                         f'<span class="app__n">{E(str(a.get("name")))}</span>'
                         f'<span class="app__s">{E(str(st))}</span></li>')
        out.append(f'<ul class="apps">{"".join(cells)}</ul>')

    nodes = h.get("nodes") or []
    nm = h.get("node_metrics") or []
    if nodes and nm:
        cap = nodes[0].get("capacity") or {}
        cpu_cap = parse_quantity(cap.get("cpu"))
        mem_cap = parse_quantity(cap.get("memory"))
        cpu_used = parse_quantity(nm[0].get("cpu"))
        mem_used = parse_quantity(nm[0].get("memory"))
        out.append('<p class="sub">ノード nixos</p>')
        out.append(meter("CPU", cpu_used, cpu_cap, f"{cpu_used:.2f} / {cpu_cap:.0f} コア"))
        out.append(meter("メモリ", mem_used, mem_cap,
                         f"{human_bytes(mem_used)} / {human_bytes(mem_cap)}"))

    # PVC は「要求した容量に対してどれだけ使ったか」で見る。ノードの
    # ephemeral-storage は kubelet の一時領域用の計算値でルート FS の大きさではない
    # （health レポートの notes / T-0079）。分母に使わない。
    used_by_pvc = {}
    for grp in h.get("pvc_usage") or []:
        for u in grp.get("usage") or []:
            used_by_pvc[(grp.get("namespace"), u.get("pvc"))] = u.get("bytes")
    known = []
    for p in h.get("pvcs") or []:
        b = used_by_pvc.get((p.get("namespace"), p.get("name")))
        if b is None:
            continue
        cap = parse_quantity(p.get("capacity") or p.get("requested"))
        known.append(meter(f'{p.get("namespace")}/{p.get("name")}', float(b), cap,
                           f"{human_bytes(b)} / {human_bytes(cap)}"))
    if known:
        unmeasured = len(h.get("pvcs") or []) - len(known)
        note = f"（実使用量が取れていない PVC が {unmeasured} 件）" if unmeasured > 0 else ""
        out.append(f'<p class="sub">保存領域の使用量{E(note)}</p>' + "".join(known))

    issues = h.get("pod_issues") or []
    if issues:
        out.append('<details class="fold"><summary>再起動の多い Pod '
                   f'{len(issues)} 件</summary><ul class="kv">'
                   + "".join(f'<li><span>{E(str(p.get("namespace", "")))}/'
                             f'{E(str(p.get("name", ""))[:34])}</span>'
                             f'<b>{E(str(p.get("restarts", "?")))} 回</b></li>'
                             for p in issues[:12]) + "</ul></details>")
    return "".join(out)


# ---------------------------------------------------------------- misc


def resolve_cadence(state) -> str | None:
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
    tasks = backlog.get("tasks", [])
    prs, merged = fetch_prs()
    health = load_health()
    runs = state.get("runs", []) or []
    journal = parse_journal()

    queue_html, qs = render_queue(tasks)
    n_done = sum(1 for t in tasks if t.get("status") == "done")
    fb = state.get("feedback", {}) or {}

    if qs["you"] == 0:
        lede = ("いまあなたにお願いすることはありません。"
                "上から順に autopilot が自分で片づけます。")
    else:
        extra = (f"それが済むと、さらに {qs['unblocks']} 件が動き出します。"
                 if qs["unblocks"] else "")
        lede = f"あなたの手が要るのは {qs['you']} 件。{extra}"

    stale_html = (f'<p class="banner banner--warn">GitHub から PR 一覧を取得できませんでした。'
                  f'「出している変更」は{E(rel_time(STALE.get("at")))}のキャッシュです。</p>'
                  ) if STALE else ""

    runs_html = ""
    for e in journal[:3]:
        items = [E(re.sub(r"^\s*[-*]\s*", "", l)) for l in e["body"].splitlines()
                 if l.strip().startswith(("-", "*"))][:4]
        runs_html += (f'<li class="jr"><h4>{E(e["head"][:52])}</h4><ul>'
                      + "".join(f"<li>{i[:130]}</li>" for i in items) + "</ul></li>")

    auto_merged = [p for p in merged if str(p.get("headRefName", "")).startswith("autopilot/")]
    done_html = "".join(
        f'<li class="dn"><a href="{E(str(p.get("url", "")))}">{E(str(p.get("title", ""))[:62])}</a>'
        f'<span>#{p["number"]} · {E(rel_time(p.get("mergedAt")))}</span></li>'
        for p in (auto_merged or merged)[:10])

    return TEMPLATE.format(
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        stage=E(str(state.get("vision_stage", "?"))),
        stage_label=E(str(state.get("vision_stage_label", ""))),
        cadence=E(resolve_cadence(state) or "巡回間隔 未設定"),
        pulse=render_pulse(state, health, prs, runs),
        queue=queue_html, n_queue=qs["total"], lede=E(lede),
        cluster=render_cluster(health),
        archive=render_archive(tasks), n_total=len(tasks), n_done=n_done,
        runs=runs_html, done=done_html,
        fb_url=E(str(fb.get("url") or f"https://github.com/{REPO}/issues")),
        fb_issue=E(str(fb.get("issue") or "?")), fb_read=E(rel_time(fb.get("last_read"))),
        stale=stale_html, repo=REPO,
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
/* 台帳（当直日誌）の見立て。罫線で区切り、面で囲わない。
   数字と識別子は等幅、日本語の文は system-ui。飾りの識別色は置かず、
   色は「誰待ちか」と「正常/注意/異常」だけに使う。 */
:root {{
  --paper:#eaedf2; --sheet:#fbfcfd; --sheet2:#f2f4f8; --rule:#d2d8e1; --rule2:#e2e6ed;
  --ink:#11151b; --ink2:#545e6c; --ink3:#7d8695;
  --accent:#1d5876; --accent-soft:#dde9f0;
  --ok:#2b6b4e; --ok-soft:#dcece3;
  --warn:#8a5709; --warn-soft:#f5e9d4;
  --crit:#96303a; --crit-soft:#f7e0e1;
  --sig:#1d5876; --sig-soft:#dde9f0;
  --idle:#69727f; --idle-soft:#e5e8ee;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", "JetBrains Mono", Menlo, Consolas,
          "Noto Sans Mono", monospace;
  --sans: system-ui, -apple-system, "Hiragino Kaku Gothic ProN", "Noto Sans JP",
          "Yu Gothic UI", sans-serif;
  --r:3px;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --paper:#0b0e13; --sheet:#131820; --sheet2:#1a202a; --rule:#242b37; --rule2:#1d232e;
    --ink:#e4e8ef; --ink2:#98a2b1; --ink3:#79828f;
    --accent:#7fc0e2; --accent-soft:#122733;
    --ok:#5fbe91; --ok-soft:#12261d;
    --warn:#ddab58; --warn-soft:#2a2213;
    --crit:#f0908f; --crit-soft:#2e1a1c;
    --sig:#7fc0e2; --sig-soft:#122733;
    --idle:#8b95a4; --idle-soft:#1a202a;
  }}
}}
:root[data-theme="dark"] {{
  --paper:#0b0e13; --sheet:#131820; --sheet2:#1a202a; --rule:#242b37; --rule2:#1d232e;
  --ink:#e4e8ef; --ink2:#98a2b1; --ink3:#79828f;
  --accent:#7fc0e2; --accent-soft:#122733;
  --ok:#5fbe91; --ok-soft:#12261d;
  --warn:#ddab58; --warn-soft:#2a2213;
  --crit:#f0908f; --crit-soft:#2e1a1c;
  --sig:#7fc0e2; --sig-soft:#122733;
  --idle:#8b95a4; --idle-soft:#1a202a;
}}
:root[data-theme="light"] {{
  --paper:#eaedf2; --sheet:#fbfcfd; --sheet2:#f2f4f8; --rule:#d2d8e1; --rule2:#e2e6ed;
  --ink:#11151b; --ink2:#545e6c; --ink3:#7d8695;
  --accent:#1d5876; --accent-soft:#dde9f0;
  --ok:#2b6b4e; --ok-soft:#dcece3;
  --warn:#8a5709; --warn-soft:#f5e9d4;
  --crit:#96303a; --crit-soft:#f7e0e1;
  --sig:#1d5876; --sig-soft:#dde9f0;
  --idle:#69727f; --idle-soft:#e5e8ee;
}}

*,*::before,*::after {{ box-sizing:border-box; }}
body,h1,h2,h3,h4,p,ul,ol,li,table,details,figure,form {{ margin:0; padding:0; }}
ul,ol {{ list-style:none; }}
body {{ background:var(--paper); color:var(--ink); font-family:var(--sans);
  font-size:16px; line-height:1.62; -webkit-font-smoothing:antialiased; }}
a {{ color:var(--accent); }}
a:focus-visible, button:focus-visible, summary:focus-visible, textarea:focus-visible {{
  outline:2px solid var(--accent); outline-offset:2px; border-radius:2px; }}
code {{ font-family:var(--mono); font-size:.78em; background:var(--idle-soft);
  padding:.06rem .3rem; border-radius:2px; }}

.wrap {{ max-width:1120px; margin:0 auto; padding:1.6rem 1.05rem 4rem;
  display:flex; flex-direction:column; gap:1.35rem; }}

/* --- 見出し帯 --- */
.mast {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:.35rem .9rem;
  border-bottom:2px solid var(--ink); padding-bottom:.6rem; }}
.mast__h {{ font-family:var(--mono); font-size:1.02rem; font-weight:600;
  letter-spacing:-.01em; }}
.mast__stage {{ font-family:var(--mono); font-size:.74rem; color:var(--accent);
  background:var(--accent-soft); padding:.05rem .45rem; border-radius:999px; }}
.mast__meta {{ margin-left:auto; font-family:var(--mono); font-size:.72rem;
  color:var(--ink3); display:flex; flex-wrap:wrap; gap:.2rem .9rem; }}

.banner {{ font-size:.83rem; padding:.5rem .8rem; border-radius:var(--r); }}
.banner--warn {{ background:var(--warn-soft); color:var(--warn);
  border:1px solid var(--warn); }}
.banner--ok {{ background:var(--ok-soft); color:var(--ok);
  border:1px solid var(--ok); display:flex; align-items:center; gap:.45rem; }}
.banner[hidden] {{ display:none; }}

/* --- 脈拍 --- */
.pulsebox {{ border:1px solid var(--rule); border-radius:var(--r); overflow:hidden;
  background:var(--rule); display:flex; flex-direction:column; gap:1px; }}
.pulse {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(215px,1fr));
  gap:1px; background:var(--rule); }}
.pulse__cell {{ background:var(--sheet); padding:.7rem .9rem .75rem;
  display:flex; flex-direction:column; gap:.1rem; min-width:0; }}
.pulse__k {{ font-family:var(--mono); font-size:.68rem; letter-spacing:.09em;
  text-transform:uppercase; color:var(--ink3); }}
.pulse__v {{ font-size:.99rem; font-weight:600; display:flex; align-items:center;
  gap:.4rem; line-height:1.4; }}
.pulse__s {{ font-size:.755rem; color:var(--ink2); line-height:1.45; }}
.dot {{ width:.5rem; height:.5rem; border-radius:50%; flex:none;
  background:var(--idle); box-shadow:0 0 0 3px var(--idle-soft); }}
.dot--ok {{ background:var(--ok); box-shadow:0 0 0 3px var(--ok-soft); }}
.dot--warn {{ background:var(--warn); box-shadow:0 0 0 3px var(--warn-soft); }}
.dot--crit {{ background:var(--crit); box-shadow:0 0 0 3px var(--crit-soft); }}
.dot--sig {{ background:var(--sig); box-shadow:0 0 0 3px var(--sig-soft); }}

.prs {{ display:flex; flex-direction:column; gap:1px; background:var(--rule); }}
.pr {{ background:var(--sheet); display:flex; align-items:center; gap:.5rem;
  padding:.42rem .9rem; font-size:.83rem; min-width:0; }}
.pr__t {{ flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; color:var(--ink); text-decoration:none; }}
.pr__t:hover {{ color:var(--accent); text-decoration:underline; }}
.pr__n {{ font-family:var(--mono); font-size:.73rem; color:var(--ink3); }}

/* --- 版面 --- */
/* 狭い画面では書き置きを先頭に出す。順番待ちの下に置くと十数行ぶん
   スクロールしないと辿り着けず、「セッションを開かずに残せる」意味が薄れる。 */
.grid {{ display:flex; flex-direction:column; gap:1.35rem; min-width:0; }}
.grid > .note {{ order:-1; }}
.rail {{ display:flex; flex-direction:column; gap:1.35rem; min-width:0; }}
@media (min-width:940px) {{
  .grid {{ display:grid; grid-template-columns:minmax(0,1.8fr) minmax(0,1fr);
    column-gap:2rem; row-gap:0; align-content:start; }}
  .grid > .q-sec {{ grid-column:1; grid-row:1 / span 2; }}
  .grid > .note {{ grid-column:2; grid-row:1; order:0; align-self:start; }}
  .grid > .rail {{ grid-column:2; grid-row:2; margin-top:1.35rem; align-self:start; }}
}}
.sec {{ display:flex; flex-direction:column; gap:.55rem; min-width:0; }}
.sec__h {{ display:flex; align-items:baseline; gap:.6rem; }}
.sec__h h2 {{ font-family:var(--mono); font-size:.88rem; font-weight:600;
  letter-spacing:.02em; white-space:nowrap; }}
.sec__h::after {{ content:""; flex:1; height:1px; background:var(--rule); }}
.sec__n {{ font-family:var(--mono); font-size:.72rem; color:var(--ink3);
  font-variant-numeric:tabular-nums; order:3; }}
.lede {{ font-size:.83rem; color:var(--ink2); }}
.empty {{ font-size:.85rem; color:var(--ink2); border:1px dashed var(--rule);
  border-radius:var(--r); padding:.7rem .85rem; }}
.sub {{ font-family:var(--mono); font-size:.7rem; letter-spacing:.08em;
  text-transform:uppercase; color:var(--ink3); margin-top:.9rem; }}

.chip {{ font-family:var(--mono); font-size:.69rem; padding:.05rem .42rem;
  border-radius:999px; background:var(--idle-soft); color:var(--idle);
  white-space:nowrap; }}
.chip--ok {{ background:var(--ok-soft); color:var(--ok); }}
.chip--warn {{ background:var(--warn-soft); color:var(--warn); }}
.chip--crit {{ background:var(--crit-soft); color:var(--crit); }}
.chip--sig {{ background:var(--sig-soft); color:var(--sig); }}

/* --- 順番待ち（台帳） --- */
.q {{ display:flex; flex-direction:column; }}
.q__empty {{ font-size:.85rem; color:var(--ink2); border:1px dashed var(--rule);
  border-radius:var(--r); padding:.7rem .85rem; }}
.q__row {{ display:grid; grid-template-columns:2.6rem minmax(0,1fr); gap:.85rem;
  padding:.8rem .2rem .85rem 0; border-top:1px solid var(--rule2); }}
.q__row:first-child {{ border-top:1px solid var(--rule); }}
.q__row:target {{ background:var(--accent-soft); border-radius:var(--r);
  padding-left:.55rem; padding-right:.55rem; }}
.q__rank {{ font-family:var(--mono); font-size:1.32rem; font-weight:600;
  font-variant-numeric:tabular-nums; color:var(--ink3); text-align:right;
  line-height:1.35; letter-spacing:-.03em; }}
.q__row--crit .q__rank {{ color:var(--crit); }}
.q__main {{ display:flex; flex-direction:column; gap:.28rem; min-width:0; }}
.q__head {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:.45rem; }}
.q__owner {{ font-family:var(--mono); font-size:.69rem; padding:.08rem .45rem;
  border-radius:2px; white-space:nowrap; background:var(--idle-soft);
  color:var(--idle); border:1px solid transparent; }}
.q__owner--crit {{ background:var(--crit); color:var(--sheet); font-weight:600; }}
.q__owner--sig {{ background:var(--sig-soft); color:var(--sig);
  border-color:var(--sig); }}
.q__owner--warn {{ background:var(--warn-soft); color:var(--warn); }}
.q__title {{ font-size:.96rem; font-weight:600; line-height:1.5;
  text-wrap:balance; flex:1; min-width:12rem; }}
.q__row--crit .q__title {{ font-size:1.02rem; }}
.q__meta {{ font-family:var(--mono); font-size:.71rem; color:var(--ink3);
  display:flex; flex-wrap:wrap; gap:.15rem .75rem; }}
.q__id {{ color:var(--ink2); }}
.q__why {{ font-size:.815rem; color:var(--ink2); }}
.q__dep {{ font-family:var(--mono); font-size:.95em; }}
.q__more summary, .q__act summary {{ cursor:pointer; font-size:.79rem;
  color:var(--accent); padding:.15rem 0; }}
.q__more p {{ font-size:.8rem; color:var(--ink2); }}
.q__impact {{ font-size:.79rem; color:var(--ink2); }}
.q__impact b {{ color:var(--ink); }}
.q__impact__names {{ display:block; font-size:.73rem; color:var(--ink3); }}
.q__act {{ background:var(--sheet); border-left:2px solid var(--crit);
  border-radius:0 var(--r) var(--r) 0; padding:.4rem .7rem .5rem; margin-top:.2rem; }}
.q__act summary {{ color:var(--crit); font-weight:600; }}
.q__act ol {{ list-style:decimal; padding-left:1.25rem; display:flex;
  flex-direction:column; gap:.32rem; font-size:.83rem; margin-top:.3rem; }}
.q__act li::marker {{ font-family:var(--mono); font-size:.76rem; color:var(--ink3); }}
.q__links {{ margin-top:.4rem; font-size:.79rem; display:flex; flex-wrap:wrap;
  gap:.2rem .8rem; }}
.q__nosteps {{ font-size:.79rem; color:var(--warn); }}

/* --- 書き置き --- */
.note {{ display:flex; flex-direction:column; gap:.5rem;
  background:var(--sheet); border:1px solid var(--accent);
  border-top:3px solid var(--accent); border-radius:var(--r);
  padding:.85rem .9rem .9rem; }}
.note__h {{ font-family:var(--mono); font-size:.92rem; font-weight:600;
  color:var(--accent); }}
.note__p {{ font-size:.8rem; color:var(--ink2); }}
.note textarea {{ font:inherit; font-size:.88rem; line-height:1.55; width:100%;
  min-height:6.5rem; resize:vertical; color:var(--ink); background:var(--paper);
  border:1px solid var(--rule); border-radius:var(--r); padding:.5rem .6rem; }}
.note textarea::placeholder {{ color:var(--ink3); }}
.note__foot {{ display:flex; flex-wrap:wrap; align-items:center; gap:.5rem .8rem; }}
.note button {{ font:inherit; font-size:.85rem; font-weight:600; cursor:pointer;
  color:var(--paper); background:var(--accent); border:1px solid var(--accent);
  border-radius:var(--r); padding:.36rem 1.1rem; }}
.note button:hover {{ filter:brightness(1.12); }}
.note__alt {{ font-size:.74rem; color:var(--ink3); }}
.note__alt a {{ color:var(--ink2); }}

/* --- 計器 --- */
.apps {{ display:flex; flex-direction:column; gap:.1rem; }}
.app {{ display:flex; align-items:center; gap:.45rem; font-size:.79rem;
  padding:.12rem 0; min-width:0; }}
.app__n {{ font-family:var(--mono); flex:1; min-width:0; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }}
.app__s {{ font-size:.72rem; color:var(--ink3); }}
.app--crit .app__s {{ color:var(--crit); }}
.meter {{ display:flex; flex-direction:column; gap:.12rem; margin-top:.4rem; }}
.meter__top {{ display:flex; justify-content:space-between; gap:.5rem;
  font-size:.77rem; }}
.meter__top span:first-child {{ overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; font-family:var(--mono); font-size:.73rem; }}
.meter__v {{ font-family:var(--mono); font-size:.72rem; color:var(--ink3);
  font-variant-numeric:tabular-nums; white-space:nowrap; }}
.meter__track {{ height:.36rem; background:var(--idle-soft); border-radius:999px;
  overflow:hidden; }}
.meter__fill {{ display:block; height:100%; border-radius:999px; }}
.meter__fill--ok {{ background:var(--ok); }}
.meter__fill--warn {{ background:var(--warn); }}
.meter__fill--crit {{ background:var(--crit); }}
.fold {{ margin-top:.7rem; }}
.fold > summary {{ cursor:pointer; font-size:.78rem; color:var(--ink2); }}
.kv {{ display:flex; flex-direction:column; gap:.08rem; margin-top:.3rem;
  font-family:var(--mono); font-size:.73rem; color:var(--ink3); }}
.kv li {{ display:flex; justify-content:space-between; gap:.6rem; }}
.kv b {{ color:var(--ink2); font-weight:600; font-variant-numeric:tabular-nums; }}

/* --- 記録 --- */
.dn {{ display:flex; flex-direction:column; gap:.02rem; padding:.35rem 0;
  border-top:1px solid var(--rule2); }}
.dn:first-child {{ border-top:none; }}
.dn a {{ font-size:.81rem; color:var(--ink); text-decoration:none; }}
.dn a:hover {{ color:var(--accent); text-decoration:underline; }}
.dn span {{ font-family:var(--mono); font-size:.69rem; color:var(--ink3); }}
.jr {{ padding:.45rem 0; border-top:1px solid var(--rule2); }}
.jr:first-child {{ border-top:none; }}
.jr h4 {{ font-family:var(--mono); font-size:.74rem; color:var(--accent); }}
.jr ul {{ font-size:.78rem; display:flex; flex-direction:column; gap:.1rem;
  margin-top:.15rem; }}
.jr li {{ padding-left:.8rem; position:relative; color:var(--ink2); }}
.jr li::before {{ content:"›"; position:absolute; left:0; color:var(--ink3); }}

/* --- 全タスク --- */
.archive {{ border-top:1px solid var(--rule); padding-top:.8rem; }}
.archive > summary {{ cursor:pointer; font-family:var(--mono); font-size:.85rem;
  font-weight:600; }}
.filters {{ display:flex; flex-wrap:wrap; gap:.3rem; margin:.7rem 0 .4rem; }}
.filters button {{ font:inherit; font-family:var(--mono); font-size:.73rem;
  padding:.15rem .6rem; border-radius:999px; border:1px solid var(--rule);
  background:var(--sheet2); color:var(--ink2); cursor:pointer; }}
.filters button[aria-pressed="true"] {{ background:var(--accent);
  border-color:var(--accent); color:var(--paper); }}
.acount {{ font-family:var(--mono); font-size:.72rem; color:var(--ink3); }}
.tablewrap {{ overflow-x:auto; max-height:62vh; overflow-y:auto;
  border:1px solid var(--rule); border-radius:var(--r); margin-top:.4rem; }}
.tablewrap table {{ border-collapse:collapse; width:100%; font-size:.79rem;
  background:var(--sheet); }}
.tablewrap th, .tablewrap td {{ text-align:left; padding:.34rem .55rem;
  border-bottom:1px solid var(--rule2); vertical-align:top; }}
.tablewrap th {{ font-family:var(--mono); font-size:.7rem; color:var(--ink3);
  background:var(--sheet2); position:sticky; top:0; z-index:1; }}
.at__id {{ font-family:var(--mono); font-size:.72rem; color:var(--ink3);
  white-space:nowrap; }}
.at__kind {{ white-space:nowrap; color:var(--ink3); font-size:.74rem; }}
.at__title {{ min-width:16rem; }}
.at__note {{ display:block; font-size:.71rem; color:var(--ink3); margin-top:.1rem; }}
.at__pr {{ font-family:var(--mono); font-size:.72rem; white-space:nowrap; }}
.tablewrap tr[hidden] {{ display:none; }}

footer {{ color:var(--ink3); font-size:.73rem; font-family:var(--mono);
  border-top:1px solid var(--rule); padding-top:.7rem;
  display:flex; flex-wrap:wrap; gap:.25rem 1.1rem; }}
@media (prefers-reduced-motion:reduce) {{
  * {{ transition:none !important; animation:none !important; }}
}}
</style>
</head>
<body>

<div class="wrap">
  <header class="mast">
    <span class="mast__h">autopilot — homelab 当直記録</span>
    <span class="mast__stage">段階 {stage}・{stage_label}</span>
    <span class="mast__meta"><span>巡回 {cadence}</span><span>生成 {generated}</span></span>
  </header>

  <p class="banner banner--ok" id="sent" hidden>書き置きを預かりました。次の巡回で読まれます。</p>

  {stale}
  {pulse}

  <div class="grid">
    <section class="sec q-sec">
      <div class="sec__h"><h2>順番待ち</h2><span class="sec__n">{n_queue} 件・優先度順</span></div>
      <p class="lede">{lede}</p>
      <ol class="q">{queue}</ol>
    </section>

    <form class="note" method="post" action="/feedback">
        <h2 class="note__h">書き置き</h2>
        <p class="note__p">殴り書きで構いません。指示も苦情も、ここに残せばセッションを開かずに届きます。</p>
        <textarea name="body" required rows="5" maxlength="20000"
          aria-label="autopilot への書き置き"
          placeholder="例: ダッシュボードのここが見づらい / immich の写真が開けない / 今週は触らないで"></textarea>
      <div class="note__foot">
        <button type="submit">送る</button>
        <span class="note__alt">届かないときは
          <a href="{fb_url}">issue #{fb_issue}</a>（最後に読んだ: {fb_read}）</span>
      </div>
    </form>

    <div class="rail">
      <section class="sec">
        <div class="sec__h"><h2>homelab の計器</h2></div>
        {cluster}
      </section>

      <section class="sec">
        <div class="sec__h"><h2>反映された変更</h2></div>
        <ul>{done}</ul>
      </section>

      <section class="sec">
        <div class="sec__h"><h2>直近の当直</h2></div>
        <ul>{runs}</ul>
      </section>
    </div>
  </div>

  <details class="archive">
    <summary>全タスクを開く（{n_total} 件、うち完了 {n_done} 件）</summary>
    <div class="filters" role="group" aria-label="状態で絞り込む">
      <button type="button" data-f="all" aria-pressed="true">すべて</button>
      <button type="button" data-f="needs-human" aria-pressed="false">あなた待ち</button>
      <button type="button" data-f="blocked" aria-pressed="false">詰まり</button>
      <button type="button" data-f="todo" aria-pressed="false">待ち</button>
      <button type="button" data-f="in_progress" aria-pressed="false">作業中</button>
      <button type="button" data-f="done" aria-pressed="false">完了</button>
      <button type="button" data-f="dropped" aria-pressed="false">取り下げ</button>
    </div>
    <p class="acount" id="acount"></p>
    <div class="tablewrap">
      <table>
        <thead><tr><th>ID</th><th>状態</th><th>種別</th><th>内容</th><th>PR</th></tr></thead>
        <tbody id="abody">{archive}</tbody>
      </table>
    </div>
  </details>

  <footer>
    <span>{repo}</span>
    <a href="https://github.com/{repo}/blob/main/ops/VISION.md">VISION</a>
    <a href="https://github.com/{repo}/blob/main/ops/CHARTER.md">CHARTER</a>
    <a href="https://github.com/{repo}/tree/main/ops/journal">journal</a>
  </footer>
</div>

<script>
/* 絞り込みだけ。ページの情報は JS 無しで全部読める（書き置きの送信も含む） */
(function () {{
  var btns = Array.prototype.slice.call(document.querySelectorAll('.filters button'));
  var rows = Array.prototype.slice.call(document.querySelectorAll('#abody tr'));
  var count = document.getElementById('acount');
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

/* 送信後にバックエンドが 303 で /?feedback=ok に戻す。受け取った印を出し、
   再読み込みで残らないよう URL からは落とす。JS 無効なら出ないだけ（送信は成立する） */
(function () {{
  if (window.location.search.indexOf('feedback=ok') === -1) return;
  var el = document.getElementById('sent');
  if (el) el.hidden = false;
  if (window.history && window.history.replaceState) {{
    window.history.replaceState(null, '', window.location.pathname);
  }}
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
            print(f"published to branch {DASHBOARD_BRANCH}")
