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
  - 主役は heart のプロジェクト台帳（ops-state ブランチの projects.json）。
    予告中 / 実行中 / レビュー中 / 納品済みを状態順にそのまま並べる。集計バーではなく
    行を出す。いま何が動いているかが読めないと意味がない
  - 「自分は何をすればいいか」は ops/projects/seeds.md の『人間の鍵作業』節だけから出す。
    旧 backlog の needs-human は数えない（凍結済みで、権限開放で大半が解消した。
    解消済みの依頼を出すのは嘘になる、P-0014）
  - 誰待ちか（あなた / heart / 条件）を行の形と色で示す。色だけに頼らず必ず語で書く
  - 済んだこと・クラスタの細部は畳む
  - 書き置きフォームを同一オリジンの POST /feedback に出す。JS 無しで成立させる
  - 数はすべてこのファイルが projects.json と seeds.md から数える。文章側で数えない
    （食い違いの元）

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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
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

# プロジェクトの状態。語彙は ops/heart/statefiles.py の PROJECT_STATES が単一の情報源で、
# ここはその 9 個を日本語ラベルと tone に写すだけ。色は「誰待ちか」「正常/注意/異常」に
# しか使わない（識別色は増やさない）。announced は拒否権の窓が開いている＝あなた待ちなので
# warn、stalled は止まっている＝異常で crit、autopilot が回している最中は sig。
PROJECT_STATE_META = {
    "proposed": ("立案", "idle"),
    "announced": ("予告中", "warn"),
    "active": ("実行中", "sig"),
    "in_review": ("レビュー中", "sig"),
    "merging": ("取り込み中", "sig"),
    "soaking": ("様子見", "ok"),
    "delivered": ("納品済み", "ok"),
    "stalled": ("停止", "crit"),
    "vetoed": ("拒否", "idle"),
}

# 上から「人間が見るべき順」。止まっているもの、拒否権の窓が開いているものが先。
PROJECT_ORDER = {
    "stalled": 0, "announced": 1, "in_review": 2, "merging": 3, "active": 4,
    "soaking": 5, "proposed": 6, "delivered": 7, "vetoed": 8,
}


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


def load_projects() -> dict | None:
    """heart が持つプロジェクト台帳（ops-state ブランチにある）。

    load_health() と同型。ops-state を持たない環境（CI の ops job は
    `git fetch --depth=1 origin main` しかしない）では None を返し、
    呼び出し側は節ごと出さない。ここで落ちると CI が赤くなる。
    """
    for ref in ("origin/ops-state", "ops-state"):
        try:
            out = subprocess.run(
                ["git", "show", f"{ref}:projects.json"],
                cwd=ROOT, capture_output=True, text=True, timeout=30, check=True,
            ).stdout
            return json.loads(out)
        except Exception:  # noqa: BLE001
            continue
    return None


def load_heartbeat() -> dict | None:
    """heart の心拍（ops-state ブランチの heartbeat.json）。

    load_projects() と同型。`{"beat": 9, "at": "...", "writer": "heart"}` の 3 キーだけ。
    ops-state を持たない環境（CI）では None を返し、呼び出し側は「観測なし」に倒す。
    """
    for ref in ("origin/ops-state", "ops-state"):
        try:
            out = subprocess.run(
                ["git", "show", f"{ref}:heartbeat.json"],
                cwd=ROOT, capture_output=True, text=True, timeout=30, check=True,
            ).stdout
            return json.loads(out)
        except Exception:  # noqa: BLE001
            continue
    return None


def heart_beat_seconds() -> int:
    """heart のビート周期（秒）。単一の情報源は ops/heart/config.py の HEART_BEAT_SECONDS。

    env は「どこで動いているか」だけを持つ規約（heart/config.py の冒頭）なので、
    ここでは空 env で既定値を読む。ダッシュボードのプロセスの env は heart の env とは
    別物で、そこから読むと嘘になる。apps/autopilot/deployment.yaml はこの変数を
    上書きしていない（2026-08-08 実測）。上書きするならここも直すこと。
    """
    try:
        from heart.config import Config
        return int(Config(ROOT, None, None, {}).beat_seconds)
    except Exception:  # noqa: BLE001
        return 120


HUMAN_KEYS_HEADING = "人間の鍵作業"


def load_human_keys() -> list[dict]:
    """ops/projects/seeds.md の『人間の鍵作業として残るもの』節から、人間に残る依頼を採る。

    この節だけが「あなたの手が要る」の情報源（P-0014）。旧 backlog の needs-human は
    数えない — 凍結済みで、権限開放後に解消したものが大半のため。

    パーサは節の中の行頭 `- ` の行だけを採り、次の `## ` 見出しか非リスト行で打ち切る。
    節の直後に別リストの番号付き項目が紛れ込んでいる（seeds.md 実測）ので、
    「リストが続く限り」で読むと拾ってしまう。節もファイルも無ければ 0 件に倒す。
    """
    p = OPS / "projects" / "seeds.md"
    if not p.exists():
        return []
    items: list[dict] = []
    inside = False
    for line in p.read_text().splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = HUMAN_KEYS_HEADING in line
            continue
        if not inside:
            continue
        if line.startswith("- "):
            body = line[2:].strip()
            m = re.match(r"^(T-\d{4})\s*[:：]\s*(.*)$", body)
            items.append({"id": m.group(1) if m else "", "text": m.group(2) if m else body})
        elif line.strip():
            break
    return items


def load_project_specs() -> dict[str, dict]:
    """archive.jsonl から id → 立案（why / cell / dod）。

    追記専用で同じ id の行が複数ありうる。runner と同じく最後の行を採る
    （ops/projects/README.md）。
    """
    p = OPS / "projects" / "archive.jsonl"
    specs: dict[str, dict] = {}
    if not p.exists():
        return specs
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("id"):
            specs[rec["id"]] = rec
    return specs


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


def clip(s, n: int) -> str:
    """n 文字で切る。切ったときは必ず … を付ける（無いと壊れて見える）。"""
    s = str(s or "")
    return s if len(s) <= n else s[:n].rstrip() + "…"


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


# ------------------------------------------------------------- 人間の鍵作業


def _md_inline(s: str) -> str:
    """seeds.md の 1 行を HTML に。エスケープしてから **強調** だけ復元する。

    素通しはしない（seeds.md には `.envrc` のようなパスも書かれる）。
    """
    out = E(str(s))
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)


def render_human_keys(items: list[dict]) -> str:
    """あなたの手が要ること。seeds.md の『人間の鍵作業』節がそのまま行になる。

    旧 backlog の queue 節（順位・優先度・依存グラフ）はここに引き継がない。
    ここに並ぶのは「autopilot に手が届かないので人間が動くしかないもの」だけで、
    優先度の列も待ち行列も持たない（数が一桁なら順位は要らない）。
    """
    if not items:
        return ('<p class="empty">いまあなたにお願いすることはありません。'
                '動いているものは heart が自分で進めます。</p>')
    rows = []
    for it in items:
        head = (f'<span class="hk__id">{E(it["id"])}</span>' if it.get("id") else "")
        rows.append(f'<li class="hk">{head}'
                    f'<span class="hk__t">{_md_inline(it["text"])}</span></li>')
    return f'<ul class="hk-list">{"".join(rows)}</ul>'


# ---------------------------------------------------------------- projects


def _tokens(n) -> str:
    """予算は桁が大きい。1.5M / 320k まで丸めて読める幅にする。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def render_projects(doc: dict | None, specs: dict[str, dict]) -> str:
    """heart のプロジェクト台帳を主列の節として描く。

    doc が None（ops-state を持たない環境）なら空文字を返し、節そのものを出さない。
    projects.json に載っていない案（archive.jsonl の棄却案）は出さない。ここは
    「いま動いているもの」の画面であって、立案の全記録ではない。
    """
    if doc is None:
        return ""

    projects = doc.get("projects") or []
    live = sorted(projects, key=lambda p: (PROJECT_ORDER.get(p.get("state"), 9),
                                           str(p.get("id", ""))))

    if not live:
        body = ('<p class="empty">動いているプロジェクトはありません。'
                '次の curriculum が立案します。</p>')
    else:
        rows = []
        for p in live:
            pid = str(p.get("id", "—"))
            state = str(p.get("state", ""))
            label, tone = PROJECT_STATE_META.get(state, (state or "—", "idle"))
            spec = specs.get(pid, {})

            meta = [f'<span class="pj__id">{E(pid)}</span>']
            cell = spec.get("cell") or []
            if cell:
                meta.append(f'<span>{E("・".join(str(c) for c in cell))}</span>')
            # 拒否権の期限は窓が閉じたあとも「もう止められない」を示すので終端以外は出す。
            # 終端（納品済み/停止/拒否）では過ぎた期限は雑音にしかならない。
            deadline = p.get("veto_deadline")
            if deadline and state not in ("delivered", "stalled", "vetoed"):
                meta.append(f'<span>拒否権の期限 {E(until_time(deadline))}</span>')
            for n in (p.get("prs") or []):
                meta.append(f'<a href="https://github.com/{REPO}/pull/{E(str(n))}">'
                            f'PR #{E(str(n))}</a>')

            why = spec.get("why") or ""
            why_html = f'<p class="pj__why">{E(clip(why, 130))}</p>' if why else ""

            budget = p.get("budget") or {}
            used, cap = budget.get("used_tokens", 0) or 0, budget.get("soft_cap", 0) or 0
            bar = meter("予算", used, cap, f"{_tokens(used)} / {_tokens(cap)} tok")

            rows.append(f"""
          <li class="pj">
            <div class="pj__head">{chip(label, tone)}
              <h3 class="pj__title">{E(clip(p.get("title"), 78))}</h3></div>
            <p class="pj__meta">{"".join(meta)}</p>
            {why_html}{bar}
          </li>""")
        body = f'<ul class="pj-list">{"".join(rows)}</ul>'

    n_open = sum(1 for p in live if p.get("state") not in ("delivered", "stalled", "vetoed"))
    return f"""<section class="sec" id="heart-projects">
      <div class="sec__h"><h2>プロジェクト</h2>
        <span class="sec__n">{len(live)} 件・進行中 {n_open}</span></div>
      {body}
    </section>"""


# ---------------------------------------------------------------- pulse


def heart_state(hb: dict | None, beat_sec: int) -> tuple[str, str, str]:
    """heart が生きているか。ops-state の heartbeat.json が一次情報（P-0014）。

    以前は state.json の `runs` 最終要素を見ていたが、あれは旧 loop.sh の起動記録で
    2026-08-07 で凍結しており、常に「止まっているかも」を出していた。heart は
    beat_seconds ごとに heartbeat.json を書き戻すので、その鮮度で判定する。
    """
    if not hb:
        return "idle", "観測なし", "ops-state の heartbeat.json が読めていません"
    gap = age_seconds(hb.get("at"))
    if gap is None:
        return "warn", "時刻が読めない", f"heartbeat.at = {hb.get('at')}"
    # 1 拍落ちただけで赤くしない。4 拍で注意、10 拍で異常（既定 120 秒なら 8 分 / 20 分）
    tone = "crit" if gap > beat_sec * 10 else "warn" if gap > beat_sec * 4 else "ok"
    text = {"ok": "鼓動しています", "warn": "鼓動が遅れています",
            "crit": "止まっているかも"}[tone]
    return tone, text, f"拍 #{hb.get('beat', '?')}・最終 {rel_time(hb.get('at'))}"


def health_freshness(h: dict | None) -> tuple[str, str]:
    """レポート自体の鮮度。古いレポートを『いまの状態』として読ませない。"""
    if not h:
        return "crit", "レポート未着"
    sec = age_seconds(h.get("generated_at"))
    if sec is None:
        return "warn", "生成時刻が読めない"
    tone = "crit" if sec > 10800 else "warn" if sec > 5400 else "ok"
    return tone, f"{rel_time(h.get('generated_at'))}の観測"


def render_pulse(hb, health, prs, beat_sec) -> str:
    cells = []

    tone, text, sub = heart_state(hb, beat_sec)
    cells.append(f'<div class="pulse__cell"><p class="pulse__k">heart の鼓動</p>'
                 f'<p class="pulse__v">{dot(tone)}{E(text)}</p>'
                 f'<p class="pulse__s">{E(sub)}</p></div>')

    apps = (health or {}).get("applications") or []
    bad = [a for a in apps if a.get("sync") != "Synced" or a.get("health") != "Healthy"]
    ftone, fresh = health_freshness(health)
    if apps:
        atone = "crit" if bad else "ok"
        atext = f"{len(apps) - len(bad)} / {len(apps)} 正常"
        asub = ("落ちている: " + "、".join(E(str(a.get("name"))) for a in bad[:4])) if bad else fresh
        # T-0162: coder/immich/vaultwarden の Degraded は Doppler 未登録の
        # ExternalSecret（T-0106）由来と分かっている既知事象。健全性レポートは
        # externalsecret の詳細まで持たないので機械判定はできず、静的な注記に留める。
        bad_names = {str(a.get("name")) for a in bad}
        known_t0106 = {"coder", "immich", "vaultwarden"}
        if bad_names and bad_names <= known_t0106:
            asub += "（既知の原因あり: T-0106、実サービスへの影響なし）"
        elif bad_names & known_t0106:
            asub += (f"（うち {'、'.join(sorted(bad_names & known_t0106))} は"
                      "既知の原因あり: T-0106、実サービスへの影響なし。他は要確認）")
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

    cells.append(render_heart_pod(health))

    pr_rows = ""
    if prs:
        items = []
        for p in prs:
            tone, label = ci_state(p)
            items.append(f'<li class="pr pr--{tone}">{dot(tone)}'
                         f'<a class="pr__t" href="{E(str(p.get("url", "")))}">'
                         f'{E(clip(p.get("title"), 70))}</a>'
                         f'<span class="pr__n">#{E(str(p.get("number")))}</span>'
                         f'{chip(label, tone)}</li>')
        pr_rows = f'<ul class="prs">{"".join(items)}</ul>'
    return f'<div class="pulsebox"><div class="pulse">{"".join(cells)}</div>{pr_rows}</div>'


def render_heart_pod(h) -> str:
    """T-0110 / P-0011: heart の Pod（namespace autopilot）が k8s 側からどう見えているか。

    「生きているか」はもう heart_state（heartbeat.json）が答えるので、ここは
    heartbeat.json では分からないことだけを言う: Pod が上がっているか、拍が
    異常終了・ハングしていないか。正常時に反復番号を出すと拍番号の二重表示になるので出さない。

    経過時間はレポートの generated_at を基準に測る。now() を基準にすると、
    レポートが古いだけでハング扱いになる（2026-08-06 に実際に誤検知した）。
    """
    ap = (h or {}).get("autopilot") or {}
    if not ap or "error" in ap:
        return ('<div class="pulse__cell"><p class="pulse__k">heart の Pod</p>'
                '<p class="pulse__v">' + dot("idle") + '観測なし</p>'
                '<p class="pulse__s">健全性レポートに autopilot キーがありません</p></div>')
    ref = ts(h.get("generated_at")) or datetime.now(timezone.utc)
    dep = ap.get("deployment") or {}
    hb = ap.get("heartbeat") or {}
    start, end = hb.get("last_start"), hb.get("last_end")

    ready, want = dep.get("readyReplicas") or 0, dep.get("replicas", "?")
    tone, text, sub = "ok", "常駐しています", f"readyReplicas {ready} / {want}"
    if ready < 1:
        tone, text = "crit", "Pod が上がっていない"
    elif start and (not end or start.get("iteration", 0) > end.get("iteration", -1)):
        elapsed = age_seconds(start.get("timestamp"), ref)
        if elapsed is not None and elapsed > 3700:
            tone, text = "crit", "ハングの疑い"
            sub = (f"#{start.get('iteration')} が観測時点で {int(elapsed // 60)} 分実行中"
                   "（timeout 3600 秒を超過）")
    elif end and end.get("exit_code") not in (0, None):
        tone, text = "crit", f"#{end.get('iteration')} が異常終了"
        sub = f"exit={end.get('exit_code')}・{rel_time(end.get('timestamp'))}"

    ftone, fresh = health_freshness(h)
    if ftone != "ok":
        sub = f"{sub}（{fresh}）" if sub else fresh
    return (f'<div class="pulse__cell"><p class="pulse__k">heart の Pod</p>'
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
                             f'{E(clip(p.get("name"), 34))}</span>'
                             f'<b>{E(str(p.get("restarts", "?")))} 回</b></li>'
                             for p in issues[:12]) + "</ul></details>")
    return "".join(out)


# ---------------------------------------------------------------- misc


def resolve_cadence(beat_sec: int) -> str:
    """人間に見せる周期。heart のビート周期そのもの（P-0014）。

    以前は state.json の routines / in_cluster_loop を見ていたが、routines は全て
    enabled: false、in_cluster_loop は退役した loop.sh の値で、どちらも今の実体を
    指していなかった。
    """
    return f"{_span(beat_sec)}ごと"


def build() -> str:
    state = load("state.json", {}) or {}
    prs, merged = fetch_prs()
    health = load_health()
    projects = load_projects()
    heartbeat = load_heartbeat()
    beat_sec = heart_beat_seconds()
    journal = parse_journal()

    human_keys = load_human_keys()
    fb = state.get("feedback", {}) or {}

    if not human_keys:
        lede = ("いまあなたにお願いすることはありません。"
                "動いているものは heart が自分で進めます。")
    else:
        lede = (f"あなたの手が要るのは {len(human_keys)} 件。"
                "どれも heart の手が物理的に届かないもので、"
                "ops/projects/seeds.md の『人間の鍵作業』がそのまま出ています。")

    stale_html = (f'<p class="banner banner--warn">GitHub から PR 一覧を取得できませんでした。'
                  f'「出している変更」は{E(rel_time(STALE.get("at")))}のキャッシュです。</p>'
                  ) if STALE else ""

    runs_html = ""
    for e in journal[:3]:
        items = [E(re.sub(r"^\s*[-*]\s*", "", l)) for l in e["body"].splitlines()
                 if l.strip().startswith(("-", "*"))][:4]
        runs_html += (f'<li class="jr"><h4>{E(clip(e["head"], 52))}</h4><ul>'
                      + "".join(f"<li>{clip(i, 130)}</li>" for i in items) + "</ul></li>")

    # 「直近の納品」は heart-projects 節の delivered 行が既に答えている。ここは PR 単位の
    # 記録（プロジェクトになっていない heart 自身の変更も入る）として役割を分ける。
    # 同じ事実を 2 か所に出さない（P-0014）。
    auto_merged = [p for p in merged if str(p.get("headRefName", "")).startswith("autopilot/")]
    done_html = "".join(
        f'<li class="dn"><a href="{E(str(p.get("url", "")))}">{E(clip(p.get("title"), 62))}</a>'
        f'<span>#{p["number"]} · {E(rel_time(p.get("mergedAt")))}</span></li>'
        for p in (auto_merged or merged)[:10])

    return TEMPLATE.format(
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        stage=E(str(state.get("vision_stage", "?"))),
        stage_label=E(str(state.get("vision_stage_label", ""))),
        cadence=E(resolve_cadence(beat_sec)),
        pulse=render_pulse(heartbeat, health, prs, beat_sec),
        projects=render_projects(projects, load_project_specs()),
        human_keys=render_human_keys(human_keys), n_keys=len(human_keys), lede=E(lede),
        cluster=render_cluster(health),
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
  /* system-ui を先頭に置かない。環境によっては serif 系（DejaVu Math TeX Gyre 等）に
     解決され、本文の欧文だけセリフになる（2026-08-06 に headless chromium で実際に出た）。
     日本語が主なので、和文サンセリフを明示して並べ、総称は sans-serif で締める。 */
  --mono: ui-monospace, SFMono-Regular, "SF Mono", "JetBrains Mono", Menlo, Consolas,
          "Noto Sans Mono", "DejaVu Sans Mono", monospace;
  --sans: -apple-system, "Hiragino Kaku Gothic ProN", "Noto Sans JP", "Noto Sans CJK JP",
          "Yu Gothic UI", Meiryo, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
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
  border:1px solid var(--ok); display:flex; flex-wrap:wrap; align-items:baseline;
  gap:.2rem .7rem; }}
.banner--ok #sentid {{ font-family:var(--mono); font-size:.73rem; opacity:.85; }}
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
/* 狭い画面では書き置きを先頭に出す。主列の節の下に置くと十数行ぶん
   スクロールしないと辿り着けず、「セッションを開かずに残せる」意味が薄れる。
   .side を display:contents で透過させ、note だけを order で引き上げる。
   広い画面では素直な 2 カラム。行をまたぐ配置（grid-row: 1 / span 2）は使わない:
   背の高い主列の高さが 1 行目にも配分され、右側に数百 px の空白が空く
   （2026-08-06 に 1280px で実際に 440px 空いた）。 */
.grid {{ display:flex; flex-direction:column; gap:1.35rem; min-width:0; }}
/* 主列は必ずこの箱でまとめる。.grid の直下に節を増やすと、広い画面の
   2 カラムが 3 セル目に折り返して版面が崩れる。 */
.col {{ display:flex; flex-direction:column; gap:1.35rem; min-width:0; }}
.side {{ display:contents; }}
.grid .note {{ order:-1; }}
.rail {{ display:flex; flex-direction:column; gap:1.35rem; min-width:0; }}
@media (min-width:940px) {{
  .grid {{ display:grid; grid-template-columns:minmax(0,1.8fr) minmax(0,1fr);
    gap:2rem; align-items:start; }}
  .side {{ display:flex; flex-direction:column; gap:1.35rem; min-width:0; }}
  .grid .note {{ order:0; }}
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

/* --- あなたの手が要ること（台帳） --- */
.hk-list {{ display:flex; flex-direction:column; }}
.hk {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:.2rem .6rem;
  padding:.6rem 0 .65rem; border-top:1px solid var(--rule2); font-size:.86rem;
  line-height:1.6; border-left:2px solid var(--crit); padding-left:.7rem; }}
.hk:first-child {{ border-top:1px solid var(--rule); }}
.hk__id {{ font-family:var(--mono); font-size:.72rem; color:var(--crit);
  white-space:nowrap; }}
.hk__t {{ flex:1; min-width:12rem; }}
.hk__t b {{ color:var(--crit); }}

/* --- プロジェクト --- */
/* 台帳の罫線。順位の数字は付けない（プロジェクトは優先度順ではなく
   状態順で、番号を振ると着手順に読めてしまう）。 */
.pj-list {{ display:flex; flex-direction:column; }}
.pj {{ display:flex; flex-direction:column; gap:.24rem; min-width:0;
  padding:.75rem 0 .8rem; border-top:1px solid var(--rule2); }}
.pj:first-child {{ border-top:1px solid var(--rule); }}
.pj__head {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:.45rem; }}
.pj__title {{ font-size:.92rem; font-weight:600; line-height:1.5;
  text-wrap:balance; flex:1; min-width:12rem; }}
.pj__meta {{ font-family:var(--mono); font-size:.71rem; color:var(--ink3);
  display:flex; flex-wrap:wrap; gap:.15rem .75rem; }}
.pj__id {{ color:var(--ink2); }}
.pj__why {{ font-size:.8rem; color:var(--ink2); }}
@media (max-width:560px) {{
  .pj__head {{ flex-direction:column; align-items:flex-start; gap:.22rem; }}
  .pj__title {{ min-width:0; }}
}}

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
    <span class="mast__meta"><span>心拍 {cadence}</span><span>生成 {generated}</span></span>
  </header>

  <p class="banner banner--ok" id="sent" hidden>書き置きを預かりました。次の鼓動で読まれます。<span id="sentid"></span></p>

  {stale}
  {pulse}

  <div class="grid">
    <div class="col">
    {projects}
    <section class="sec" id="human-keys">
      <div class="sec__h"><h2>あなたの手が要ること</h2>
        <span class="sec__n">{n_keys} 件</span></div>
      <p class="lede">{lede}</p>
      {human_keys}
    </section>

    <section class="sec" id="legacy-backlog">
      <div class="sec__h"><h2>旧 backlog</h2><span class="sec__n">凍結</span></div>
      <p class="lede">旧体制のタスクキュー（<code>ops/backlog.json</code>）は凍結しました。
        もう誰も取りません。生きている論点は
        <a href="https://github.com/{repo}/blob/main/ops/projects/seeds.md">ops/projects/seeds.md</a>
        に移してあり、そこから curriculum がプロジェクトを立てます。</p>
    </section>
    </div>

    <div class="side">
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
  </div>

  <footer>
    <span>{repo}</span>
    <a href="https://github.com/{repo}/blob/main/ops/VISION.md">VISION</a>
    <a href="https://github.com/{repo}/blob/main/ops/CHARTER.md">CHARTER</a>
    <a href="https://github.com/{repo}/tree/main/ops/journal">journal</a>
  </footer>
</div>

<script>
/* ページの情報は JS 無しで全部読める（書き置きの送信も含む）。JS は送信後の印だけ */
/* 送信後にバックエンドが 303 で /?feedback=ok&id=... に戻す。受け取った印と控えの id を
   出し、再読み込みで残らないよう URL からは落とす。JS 無効なら出ないだけ（送信は成立する。
   書けなかったときはバックエンドがエラーページを返すので、この印は出ない） */
(function () {{
  if (window.location.search.indexOf('feedback=ok') === -1) return;
  var el = document.getElementById('sent');
  if (el) el.hidden = false;
  var m = window.location.search.match(/[?&]id=([^&]+)/);
  var idEl = document.getElementById('sentid');
  if (m && idEl) idEl.textContent = '控え: ' + decodeURIComponent(m[1]);
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
