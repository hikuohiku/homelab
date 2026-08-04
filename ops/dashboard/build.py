#!/usr/bin/env python3
"""ops/ の状態から人間向けダッシュボード HTML を生成する。

    python3 ops/dashboard/build.py

出力: ops/dashboard/index.html （Artifact として publish される）
標準ライブラリのみ。入力が同じなら出力も同じ（決定的）。
"""

from __future__ import annotations

import html
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

OPS = pathlib.Path(__file__).resolve().parent.parent
ROOT = OPS.parent
OUT = OPS / "dashboard" / "index.html"

E = html.escape


def load(name: str, default=None):
    p = OPS / name
    if not p.exists():
        return default
    return json.loads(p.read_text())


def fetch_prs() -> list[dict]:
    """オープン PR を取得。gh が無い/失敗したらキャッシュにフォールバックする。"""
    cache = OPS / "dashboard" / "prs.json"
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", "30", "--json",
             "number,title,url,isDraft,createdAt,statusCheckRollup,headRefName,autoMergeRequest"],
            cwd=ROOT, capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        prs = json.loads(out)
        cache.write_text(json.dumps(prs, ensure_ascii=False, indent=2) + "\n")
        return prs
    except Exception as e:  # noqa: BLE001
        print(f"warning: gh pr list に失敗したのでキャッシュを使う ({e})", file=sys.stderr)
        return json.loads(cache.read_text()) if cache.exists() else []


def ci_state(pr: dict) -> tuple[str, str]:
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "idle", "CI 未実行"
    concl = [c.get("conclusion") or c.get("state") or "" for c in rollup]
    if any(c in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for c in concl):
        return "crit", "CI 失敗"
    if any(c in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED") for c in concl):
        return "warn", "CI 実行中"
    return "ok", "CI green"


def parse_journal() -> list[dict]:
    """journal の見出しを新しい順に拾う。"""
    entries = []
    for f in sorted((OPS / "journal").glob("*.md"), reverse=True):
        text = f.read_text()
        blocks = re.split(r"^## ", text, flags=re.M)[1:]
        for b in blocks:
            head, _, body = b.partition("\n")
            entries.append({"head": head.strip(), "body": body.strip(), "file": f.name})
    return entries


def rel_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    d = datetime.now(timezone.utc) - t
    s = int(d.total_seconds())
    if s < 3600:
        return f"{s // 60} 分前"
    if s < 86400:
        return f"{s // 3600} 時間前"
    return f"{s // 86400} 日前"


# ---------------------------------------------------------------- rendering

RISK_LABEL = {"low": "低", "medium": "中", "high": "高"}
STATUS_META = {
    # status: (表示名, 意味色)
    "todo": ("待ち", "idle"),
    "in_progress": ("作業中", "sig"),
    "blocked": ("詰まり", "warn"),
    "needs-human": ("要判断", "crit"),
    "done": ("完了", "ok"),
    "dropped": ("取り下げ", "idle"),
}
KIND_LABEL = {
    "ci": "CI", "upgrade": "更新", "refactor": "整理", "docs": "記録",
    "feature": "機能", "investigate": "調査", "security": "安全", "meta": "自己点検",
}


def chip(text: str, tone: str) -> str:
    return f'<span class="chip chip--{tone}">{E(text)}</span>'


def task_row(t: dict) -> str:
    label, tone = STATUS_META.get(t["status"], (t["status"], "idle"))
    refs = "".join(f"<code>{E(r)}</code>" for r in t.get("refs", [])[:3])
    pr = t.get("pr")
    pr_html = f' <a class="prlink" href="{E(str(pr))}">PR</a>' if pr else ""
    note = t.get("needs_human_reason") or t.get("blocked_by") or ""
    note_html = f'<p class="task__note">{E(note)}</p>' if note else ""
    return f"""
      <li class="task task--{tone}">
        <div class="task__head">
          <span class="task__id">{E(t['id'])}</span>
          <h3 class="task__title">{E(t['title'])}{pr_html}</h3>
        </div>
        <p class="task__why">{E(t.get('why',''))}</p>
        {note_html}
        <div class="task__meta">
          {chip(label, tone)}
          {chip(KIND_LABEL.get(t.get('kind',''), t.get('kind','')), 'quiet')}
          {chip('リスク ' + RISK_LABEL.get(t.get('risk',''), '?'), 'quiet')}
          <span class="task__refs">{refs}</span>
        </div>
      </li>"""


def pr_row(p: dict) -> str:
    tone, ci_label = ci_state(p)
    auto = "auto-merge 予約済" if p.get("autoMergeRequest") else "手動マージ待ち"
    draft = " · draft" if p.get("isDraft") else ""
    return f"""
      <li class="pr pr--{tone}">
        <a class="pr__title" href="{E(p['url'])}">#{p['number']} {E(p['title'])}</a>
        <div class="task__meta">
          {chip(ci_label, tone)}
          {chip(auto, 'quiet')}
          <span class="pr__age">{E(rel_time(p.get('createdAt')))}{E(draft)}</span>
        </div>
      </li>"""


def journal_row(e: dict) -> str:
    lines = [l for l in e["body"].splitlines() if l.strip()]
    body = "\n".join(lines[:12])
    # markdown の箇条書きだけ最低限拾う
    items = [E(re.sub(r"^\s*[-*]\s*", "", l)) for l in body.splitlines() if l.strip().startswith(("-", "*"))]
    inner = "".join(f"<li>{i}</li>" for i in items) or f"<li>{E(body[:300])}</li>"
    return f"""
      <li class="run">
        <h3 class="run__head">{E(e['head'])}</h3>
        <ul class="run__body">{inner}</ul>
      </li>"""


def inv_row(t: dict) -> str:
    pol = {"auto": ("自動可", "ok"), "manual": ("要判断", "warn"), "pinned": ("固定", "idle")}[t["policy"]]
    mirrors = t.get("mirrors") or []
    mirror_html = f'<span class="inv__mirror">+{len(mirrors)} 箇所に重複</span>' if mirrors else ""
    return f"""
      <tr>
        <td class="inv__name">{E(t['name'])}{mirror_html}</td>
        <td><code>{E(str(t['current']))}</code></td>
        <td class="inv__file"><code>{E(t['file'])}</code></td>
        <td>{chip(pol[0], pol[1])}</td>
      </tr>"""


def build() -> str:
    state = load("state.json", {})
    backlog = load("backlog.json", {"tasks": []})
    inventory = load("inventory.json", {"targets": []})
    tasks = backlog["tasks"]
    prs = fetch_prs()
    journal = parse_journal()

    by_status = {k: [t for t in tasks if t["status"] == k] for k in STATUS_META}
    for k in by_status:
        by_status[k].sort(key=lambda t: t.get("priority", 999))

    runs = state.get("runs", [])
    last_run = runs[-1] if runs else {}
    fb = state.get("feedback", {})
    fb_url = fb.get("url") or "https://github.com/hikuohiku/homelab/issues"
    routines = state.get("routines", [])
    next_run = routines[0].get("next") if routines else None

    failing = [p for p in prs if ci_state(p)[0] == "crit"]

    # ---- 一番上の要約。人間が朝 5 秒で読む部分
    def stat(value, label, tone="sig"):
        return f'<div class="stat stat--{tone}"><span class="stat__v">{E(str(value))}</span><span class="stat__l">{E(label)}</span></div>'

    stats = "".join([
        stat(rel_time(last_run.get("at")), "最終起動"),
        stat(len(prs), "オープン PR", "crit" if failing else "sig"),
        stat(len(by_status["needs-human"]), "要判断", "crit" if by_status["needs-human"] else "idle"),
        stat(len(by_status["todo"]), "待ちタスク", "idle"),
    ])

    attention = by_status["needs-human"] + by_status["blocked"]
    attention_html = (
        "".join(task_row(t) for t in attention)
        if attention else
        '<li class="empty">人間に渡っているものはありません。</li>'
    )

    queue = by_status["in_progress"] + by_status["todo"]
    queue_html = "".join(task_row(t) for t in queue[:14]) or '<li class="empty">キューは空です。次の起動で調査タスクを起票します。</li>'
    more = f'<p class="more">ほか {len(queue) - 14} 件</p>' if len(queue) > 14 else ""

    prs_html = "".join(pr_row(p) for p in prs) or '<li class="empty">オープンな PR はありません。</li>'
    runs_html = "".join(journal_row(e) for e in journal[:5]) or '<li class="empty">まだ記録がありません。</li>'
    done_html = "".join(task_row(t) for t in by_status["done"][-6:]) or '<li class="empty">まだ完了したタスクはありません。</li>'
    inv_html = "".join(inv_row(t) for t in inventory["targets"])

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    stage = state.get("vision_stage", "?")
    stage_label = state.get("vision_stage_label", "")
    next_html = f"<span>次の起動 {E(next_run)}</span>" if next_run else "<span>定期実行 未設定</span>"

    return TEMPLATE.format(
        generated=generated, stage=stage, stage_label=E(stage_label), next_html=next_html,
        stats=stats, attention=attention_html, queue=queue_html, more=more,
        prs=prs_html, runs=runs_html, done=done_html, inv=inv_html,
        fb_url=E(fb_url), fb_issue=E(str(fb.get("issue") or "?")),
        fb_read=E(rel_time(fb.get("last_read"))),
    )


TEMPLATE = """<title>autopilot — 当直記録</title>
<style>
:root {{
  --ground:#eef0f4; --surface:#fbfcfe; --line:#d8dce5; --ink:#161a21; --muted:#5b6473;
  --sig:#41528c; --sig-soft:#e3e7f4;
  --ok:#2c6f52; --ok-soft:#dfeee7;
  --warn:#8a5c0d; --warn-soft:#f6ebd7;
  --crit:#983737; --crit-soft:#f7e2e2;
  --idle:#6a7382; --idle-soft:#e6e9ef;
  --serif: ui-serif, "Iowan Old Style", "Source Serif 4", "Hiragino Mincho ProN", Georgia, serif;
  --sans: ui-sans-serif, system-ui, -apple-system, "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  --radius:3px;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --ground:#0d0f14; --surface:#151922; --line:#272d3a; --ink:#e6e9f0; --muted:#8e97a7;
    --sig:#96a6e0; --sig-soft:#1e2439;
    --ok:#63bd93; --ok-soft:#152a22;
    --warn:#dcac5e; --warn-soft:#2c2415;
    --crit:#ef9190; --crit-soft:#2f1c1e;
    --idle:#8e97a7; --idle-soft:#1c212b;
  }}
}}
:root[data-theme="dark"] {{
  --ground:#0d0f14; --surface:#151922; --line:#272d3a; --ink:#e6e9f0; --muted:#8e97a7;
  --sig:#96a6e0; --sig-soft:#1e2439;
  --ok:#63bd93; --ok-soft:#152a22;
  --warn:#dcac5e; --warn-soft:#2c2415;
  --crit:#ef9190; --crit-soft:#2f1c1e;
  --idle:#8e97a7; --idle-soft:#1c212b;
}}
:root[data-theme="light"] {{
  --ground:#eef0f4; --surface:#fbfcfe; --line:#d8dce5; --ink:#161a21; --muted:#5b6473;
  --sig:#41528c; --sig-soft:#e3e7f4;
  --ok:#2c6f52; --ok-soft:#dfeee7;
  --warn:#8a5c0d; --warn-soft:#f6ebd7;
  --crit:#983737; --crit-soft:#f7e2e2;
  --idle:#6a7382; --idle-soft:#e6e9ef;
}}

body {{ background:var(--ground); color:var(--ink); font-family:var(--sans);
  line-height:1.65; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1000px; margin:0 auto; padding:2.5rem 1.25rem 5rem;
  display:flex; flex-direction:column; gap:2.75rem; }}

/* ---- masthead */
.mast {{ display:flex; flex-direction:column; gap:.6rem;
  border-bottom:2px solid var(--ink); padding-bottom:1rem; }}
.mast__row {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:.75rem 1.25rem; }}
.mast h1 {{ font-family:var(--serif); font-size:clamp(1.7rem,4vw,2.4rem); font-weight:600;
  letter-spacing:-.01em; text-wrap:balance; }}
.mast__sub {{ color:var(--muted); font-size:.86rem; font-family:var(--mono);
  display:flex; flex-wrap:wrap; gap:.4rem 1rem; }}
.mast__stage {{ color:var(--sig); font-weight:600; }}

.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px;
  background:var(--line); border:1px solid var(--line); border-radius:var(--radius); overflow:hidden; }}
.stat {{ background:var(--surface); padding:1rem 1.1rem; display:flex; flex-direction:column; gap:.15rem;
  border-top:3px solid var(--idle); }}
.stat--sig {{ border-top-color:var(--sig); }}
.stat--crit {{ border-top-color:var(--crit); }}
.stat--ok {{ border-top-color:var(--ok); }}
.stat__v {{ font-family:var(--mono); font-size:1.5rem; font-variant-numeric:tabular-nums;
  letter-spacing:-.02em; }}
.stat__l {{ font-size:.78rem; color:var(--muted); letter-spacing:.06em; }}

/* ---- sections */
section {{ display:flex; flex-direction:column; gap:1rem; }}
h2 {{ font-family:var(--serif); font-size:1.15rem; font-weight:600;
  display:flex; align-items:baseline; gap:.6rem; }}
h2::after {{ content:""; flex:1; height:1px; background:var(--line); }}
.lede {{ color:var(--muted); font-size:.88rem; margin-top:-.5rem; }}

ul.list {{ list-style:none; display:flex; flex-direction:column; gap:.6rem; }}
.empty {{ color:var(--muted); font-size:.9rem; background:var(--surface);
  border:1px dashed var(--line); border-radius:var(--radius); padding:.9rem 1rem; }}
.more {{ color:var(--muted); font-size:.85rem; }}

/* ---- task */
.task {{ background:var(--surface); border:1px solid var(--line); border-left:3px solid var(--idle);
  border-radius:var(--radius); padding:.85rem 1rem; display:flex; flex-direction:column; gap:.4rem; }}
.task--crit {{ border-left-color:var(--crit); }}
.task--warn {{ border-left-color:var(--warn); }}
.task--sig  {{ border-left-color:var(--sig); }}
.task--ok   {{ border-left-color:var(--ok); }}
.task__head {{ display:flex; gap:.6rem; align-items:baseline; flex-wrap:wrap; }}
.task__id {{ font-family:var(--mono); font-size:.78rem; color:var(--muted);
  font-variant-numeric:tabular-nums; }}
.task__title {{ font-size:.98rem; font-weight:600; text-wrap:balance; }}
.task__why {{ font-size:.88rem; color:var(--muted); }}
.task__note {{ font-size:.85rem; color:var(--crit); background:var(--crit-soft);
  padding:.4rem .6rem; border-radius:var(--radius); }}
.task__meta {{ display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; margin-top:.15rem; }}
.task__refs {{ display:flex; flex-wrap:wrap; gap:.35rem; }}

code {{ font-family:var(--mono); font-size:.76rem; color:var(--muted);
  background:var(--idle-soft); padding:.1rem .35rem; border-radius:2px; }}

.chip {{ font-size:.73rem; letter-spacing:.04em; padding:.12rem .5rem; border-radius:999px;
  background:var(--idle-soft); color:var(--idle); white-space:nowrap; }}
.chip--ok {{ background:var(--ok-soft); color:var(--ok); }}
.chip--warn {{ background:var(--warn-soft); color:var(--warn); }}
.chip--crit {{ background:var(--crit-soft); color:var(--crit); }}
.chip--sig {{ background:var(--sig-soft); color:var(--sig); }}
.chip--quiet {{ background:transparent; color:var(--muted); border:1px solid var(--line); }}

/* ---- pr */
.pr {{ background:var(--surface); border:1px solid var(--line); border-left:3px solid var(--idle);
  border-radius:var(--radius); padding:.75rem 1rem; display:flex; flex-direction:column; gap:.35rem; }}
.pr--ok {{ border-left-color:var(--ok); }}
.pr--warn {{ border-left-color:var(--warn); }}
.pr--crit {{ border-left-color:var(--crit); }}
.pr__title {{ font-weight:600; font-size:.95rem; color:var(--ink); text-decoration:none; }}
.pr__title:hover {{ color:var(--sig); text-decoration:underline; }}
.pr__age {{ font-size:.78rem; color:var(--muted); font-family:var(--mono); }}
.prlink {{ font-size:.78rem; font-family:var(--mono); color:var(--sig); }}

/* ---- runs */
.run {{ background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:.85rem 1rem; display:flex; flex-direction:column; gap:.4rem; }}
.run__head {{ font-family:var(--mono); font-size:.82rem; color:var(--sig);
  font-variant-numeric:tabular-nums; }}
.run__body {{ list-style:none; display:flex; flex-direction:column; gap:.25rem;
  font-size:.88rem; color:var(--ink); }}
.run__body li {{ padding-left:1rem; position:relative; }}
.run__body li::before {{ content:"›"; position:absolute; left:.15rem; color:var(--muted); }}

/* ---- inventory */
.tablewrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:var(--radius);
  background:var(--surface); }}
table {{ border-collapse:collapse; width:100%; font-size:.85rem; }}
th, td {{ text-align:left; padding:.5rem .75rem; border-bottom:1px solid var(--line);
  vertical-align:top; white-space:nowrap; }}
th {{ font-size:.74rem; letter-spacing:.08em; color:var(--muted); font-weight:600;
  background:var(--idle-soft); }}
tr:last-child td {{ border-bottom:none; }}
.inv__name {{ font-weight:600; white-space:normal; }}
.inv__mirror {{ display:block; font-size:.72rem; color:var(--warn); font-weight:400; }}
.inv__file code {{ font-size:.72rem; }}

/* ---- feedback */
.fb {{ background:var(--sig-soft); border:1px solid var(--sig); border-radius:var(--radius);
  padding:1.25rem 1.35rem; display:flex; flex-direction:column; gap:.7rem; }}
.fb h2 {{ color:var(--sig); }}
.fb h2::after {{ background:var(--sig); opacity:.3; }}
.fb p {{ font-size:.9rem; }}
.fb__cta {{ align-self:flex-start; background:var(--sig); color:var(--surface);
  padding:.55rem 1.1rem; border-radius:var(--radius); text-decoration:none;
  font-weight:600; font-size:.9rem; }}
.fb__cta:hover {{ opacity:.88; }}
.fb__cta:focus-visible {{ outline:2px solid var(--ink); outline-offset:2px; }}
.fb__meta {{ font-family:var(--mono); font-size:.78rem; color:var(--muted); }}

footer {{ color:var(--muted); font-size:.8rem; font-family:var(--mono);
  border-top:1px solid var(--line); padding-top:1rem;
  display:flex; flex-wrap:wrap; gap:.4rem 1.2rem; }}
a {{ color:var(--sig); }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important; animation:none !important; }} }}
</style>

<div class="wrap">
  <header class="mast">
    <div class="mast__row">
      <h1>autopilot — 当直記録</h1>
      <span class="chip chip--sig">段階 {stage}・{stage_label}</span>
    </div>
    <div class="mast__sub">
      <span>homelab を自律運用するエージェントの記録</span>
      {next_html}
      <span>生成 {generated}</span>
    </div>
  </header>

  <div class="stats">{stats}</div>

  <section>
    <h2>あなたの判断を待っているもの</h2>
    <p class="lede">エージェントが自分では決めないと判断したもの。ここが空なら、朝に見るものはありません。</p>
    <ul class="list">{attention}</ul>
  </section>

  <section>
    <h2>レビュー待ちの PR</h2>
    <ul class="list">{prs}</ul>
  </section>

  <section class="fb">
    <h2>フィードバックを書く</h2>
    <p>殴り書きで構いません。進め方への指摘は憲章に、やってほしいことはタスクとして取り込まれます。
       読んだら 3 行以内で返信します。<strong>返信が無い＝まだ読んでいない</strong>と判断してください。</p>
    <a class="fb__cta" href="{fb_url}">issue #{fb_issue} に書く</a>
    <span class="fb__meta">最後に読んだ: {fb_read}</span>
  </section>

  <section>
    <h2>これからやること</h2>
    <p class="lede">上から順に着手します。1 タスク = 1 PR。</p>
    <ul class="list">{queue}</ul>
    {more}
  </section>

  <section>
    <h2>直近の起動</h2>
    <ul class="list">{runs}</ul>
  </section>

  <section>
    <h2>終わったこと</h2>
    <ul class="list">{done}</ul>
  </section>

  <section>
    <h2>バージョン監視の対象</h2>
    <p class="lede">上流に新しい版が出ていないか見張っている箇所。「要判断」は自動更新せず必ず人間に渡します。</p>
    <div class="tablewrap">
      <table>
        <thead><tr><th>対象</th><th>現在</th><th>場所</th><th>更新方針</th></tr></thead>
        <tbody>{inv}</tbody>
      </table>
    </div>
  </section>

  <footer>
    <span>hikuohiku/homelab</span>
    <a href="https://github.com/hikuohiku/homelab/blob/main/ops/VISION.md">VISION.md</a>
    <a href="https://github.com/hikuohiku/homelab/blob/main/ops/CHARTER.md">CHARTER.md</a>
    <a href="https://github.com/hikuohiku/homelab/tree/main/ops/journal">journal</a>
  </footer>
</div>
"""


if __name__ == "__main__":
    OUT.write_text(build())
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")
