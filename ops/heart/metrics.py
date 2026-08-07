"""指標の集計とサーキットブレーカー。

「客観指標で絞る → 異常時のみ精読」(プラン決定 #10) の「客観指標」側。
ここでは安価に数えられるものだけを扱い、判断 (精読) は critic Job に渡す。
"""

import json
import re
from datetime import datetime, timezone

RESULT_RE = re.compile(r'"type"\s*:\s*"result"')


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
