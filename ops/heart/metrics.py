"""指標の集計とサーキットブレーカー。

「客観指標で絞る → 異常時のみ精読」(プラン決定 #10) の「客観指標」側。
ここでは安価に数えられるものだけを扱い、判断 (精読) は critic Job に渡す。
"""

import json
import re
from datetime import datetime, timedelta, timezone

from .statefiles import TERMINAL_STATES, now_iso, parse_iso

RESULT_RE = re.compile(r'"type"\s*:\s*"result"')

# ビート 1 回に帰属させる持続時間の上限。実測のビート間隔は中央値 65s / 最大 78s
# (2026-08-10) なので、これを超える空きは「間隔のばらつき」ではなく heart が
# 止まっていた区間。滞留に混ぜると「詰まっていた」と「動いていなかった」を
# 取り違えるため downtime として分ける (P-0045)
MAX_BEAT_GAP_SECONDS = 600


def scan_transcript_costs(transcripts_dir, day):
    """当日分 transcript の result イベントから total_cost_usd を合計する。
    ファイル名は <YYYY-MM-DD>T...jsonl (runner/loop が日付プレフィクスで書く)。"""
    total = 0.0
    sessions = 0
    if not transcripts_dir.is_dir():
        return total, sessions
    for path in transcripts_dir.rglob(f"{day}*.jsonl"):
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    if '"result"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "result":
                        total += float(ev.get("total_cost_usd") or 0.0)
                        sessions += 1
        except OSError:
            continue
    return total, sessions


def _beat_at(rec):
    try:
        return parse_iso(rec["at"])
    except (KeyError, TypeError, ValueError):
        return None


def summarize_beats(records, now, window_hours=24, max_gap_seconds=MAX_BEAT_GAP_SECONDS):
    """metrics.jsonl の直近 window_hours 分を集計する (純関数。I/O を書かない)。

    critic に渡す「候補区間」の材料 — 器が自分の詰まり (どの状態で時間が溶けたか)
    とアイドル率を、人間より先に見られるようにするための数え上げ (P-0045)。

    時間は **必ず at の差分で数える**。踏むと結果が静かに嘘になる罠が 2 つある:
      - `beat` は heart の再起動でリセットされる (今日の先頭は beat 2)。
        番号を順序・件数・経過時間の根拠にできない
      - ビート間隔は一定ではない (実測: 中央値 65s / 最大 78s)。
        「ビート数 × 固定間隔」で秒に換算してはいけない

    滞留 (state_seconds) は非終端の状態だけを数える。delivered/stalled は
    projects.json に残り続けるので、延べ秒を数えると終端の山が全部を覆い隠す
    (実測: 今日 1 日で stalled 1732 / delivered 3248 ビート延べ)。終端は
    「今いくつあるか」のスナップショット (terminal_now) で足りる。
    """
    start = now - timedelta(hours=window_hours)
    beats = []
    for rec in records:
        at = _beat_at(rec)
        if at is None or at < start or at > now:
            continue
        beats.append((at, rec))
    beats.sort(key=lambda item: item[0])

    summary = {
        "window_hours": window_hours,
        "from": now_iso(start),
        "to": now_iso(now),
        "beats": len(beats),
        "idle_beats": 0,
        "idle_ratio": 0.0,
        "elapsed_seconds": 0,
        "downtime_seconds": 0,
        "busy_seconds": 0,
        "window_blocked_seconds": 0,
        "state_seconds": {},
        "project_seconds": {},
        "terminal_now": {},
        "actions": {},
    }
    if not beats:
        return summary

    elapsed = downtime = busy = blocked = 0.0
    state_seconds, project_seconds, action_counts = {}, {}, {}
    for i, (at, rec) in enumerate(beats):
        nxt = beats[i + 1][0] if i + 1 < len(beats) else now
        raw = max(0.0, (nxt - at).total_seconds())
        dur = min(raw, max_gap_seconds)
        downtime += raw - dur
        elapsed += dur

        actions = rec.get("actions") or []
        for kind in actions:
            action_counts[kind] = action_counts.get(kind, 0) + 1
        if actions:
            busy += dur
        else:
            summary["idle_beats"] += 1

        states = rec.get("projects") or {}
        for pid, state in states.items():
            if state in TERMINAL_STATES:
                continue
            state_seconds[state] = state_seconds.get(state, 0.0) + dur
            per = project_seconds.setdefault(pid, {})
            per[state] = per.get(state, 0.0) + dur
        if any(s == "announced" for s in states.values()):
            # 1 件以上が拒否権窓で待っていた実時間 (延べではなく壁時計)。
            # 窓待ちが実作業を上回っていないかを見るための指標
            blocked += dur

    last_states = beats[-1][1].get("projects") or {}
    summary.update(
        {
            "idle_ratio": round(summary["idle_beats"] / len(beats), 4),
            "elapsed_seconds": int(elapsed),
            "downtime_seconds": int(downtime),
            "busy_seconds": int(busy),
            "window_blocked_seconds": int(blocked),
            "state_seconds": {
                k: int(v)
                for k, v in sorted(state_seconds.items(), key=lambda kv: -kv[1])
            },
            "project_seconds": {
                pid: {k: int(v) for k, v in sorted(per.items(), key=lambda kv: -kv[1])}
                for pid, per in project_seconds.items()
            },
            "terminal_now": {
                s: sum(1 for st in last_states.values() if st == s)
                for s in TERMINAL_STATES
            },
            "actions": dict(
                sorted(action_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
        }
    )
    return summary


def summarize_stalled(projects_doc):
    """stalled の内訳 {reason: 件数} を projects.json から数える (純関数)。

    metrics.jsonl には state しか入っておらず stalled_reason が無いので、
    「なぜ止まったか」はこちらからしか数えられない (実測で確認済み)。
    """
    counts = {}
    for p in (projects_doc or {}).get("projects", []):
        if p.get("state") != "stalled":
            continue
        reason = p.get("stalled_reason") or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def breaker_tripped(statefiles, rules, transcripts_dir, now=None):
    """当日の名目コスト合計が閾値超なら True。走行中を殺す判断はここでしない
    (decide が『新規を作らない』だけに使う)。"""
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    cost, sessions = scan_transcript_costs(transcripts_dir, day)
    tripped = cost > rules["breaker"]["daily_cost_usd"]
    return tripped, {"day": day, "cost_usd": round(cost, 4), "sessions": sessions}


def rotate_transcripts(transcripts_dir, rules, now=None):
    """保持期間 + 合計サイズ上限で transcript を削除する (PVC を溢れさせない)。

    retention_days だけでは 30 日以内の大量書き込みで PVC (20Gi) を溢れさせ得る
    (レビュー指摘 [13])。max_total_gb 超過分は古い順に追加削除する。"""
    from datetime import timedelta

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=rules["transcripts"]["retention_days"])
    removed = 0
    if not transcripts_dir.is_dir():
        return removed
    entries = []  # (mtime, size, path)
    for path in transcripts_dir.rglob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        else:
            entries.append((mtime, st.st_size, path))
    max_bytes = int(rules["transcripts"]["max_total_gb"] * 1024**3)
    total = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if total <= max_bytes:
            break
        try:
            path.unlink()
            removed += 1
            total -= size
        except OSError:
            continue
    return removed
