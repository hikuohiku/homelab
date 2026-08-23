"""「忘れると困る日」の台帳を人間の言葉で告げる (P-0231)。

台帳は main の ops/reminders.json (単一情報源)。このモジュールはそのうち
直近 48 時間以内に来る日だけを抜き出し、1 行 1 件の日本語文面にする。

- due の計算と文面はここだけで行う。ダッシュボード (apps/ops-dashboard) は
  完成した文面を表示するだけで、日付計算を複製しない
  (「同じ事実が 2 箇所に書かれていない」CHARTER §1)
- 「今日 / 明日」の起点は JST (ダッシュボードの表示時刻方針と同じ。heart の
  pod は UTC で動くので、UTC のまま判定すると人間の夜に「明日」が入れ替わる)
- now を必ず注入できる形にして、境界 (今日・明日・明後日の窓の端) は
  ops/tests/test_reminders.py で固定する

CLI: `python3 -m ops.life.reminders [--ledger PATH] [--now ISO8601]`
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

WINDOW_HOURS = 48
MAX_LINES = 3
# 固定オフセットでよい (日本は DST が無い)。ZoneInfo は環境によって tz database を
# 持たないので使わない (stdlib のみでどこでも動くこと)
JST = datetime.timezone(datetime.timedelta(hours=9), "Asia/Tokyo")

LEDGER_PATH = Path(__file__).resolve().parents[2] / "ops" / "reminders.json"

_LABELS = {0: "今日", 1: "明日", 2: "明後日"}


def _as_jst(now: datetime.datetime) -> datetime.datetime:
    """aware へ寄せて JST で見る。naive は JST と解釈する。"""
    if now.tzinfo is None:
        return now.replace(tzinfo=JST)
    return now.astimezone(JST)


def parse_date(raw: str) -> datetime.date:
    """YYYY-MM-DD のみ受ける (ISO の緩い解釈を許すと schema が曖昧になる)。"""
    if not isinstance(raw, str) or len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise ValueError(f"date は YYYY-MM-DD で書く: {raw!r}")
    return datetime.date.fromisoformat(raw)


def _in_year(month: int, day: int, year: int) -> datetime.date:
    """その年の month/day。2/29 は平年なら 3/1 に振る (誕生日の慣行)。"""
    if (month, day) == (2, 29):
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        if not leap:
            return datetime.date(year, 3, 1)
    return datetime.date(year, month, day)


def occurrence_date(anchor: datetime.date, repeat: str, today: datetime.date) -> datetime.date | None:
    """次に来る日付 (今日以降)。もう来ないなら None。

    repeat="year" は anchor の年を無視して毎年同月日 (誕生日・定期行事用)。
    repeat="none" はその日限り。
    """
    if repeat == "year":
        candidate = _in_year(anchor.month, anchor.day, today.year)
        if candidate < today:
            candidate = _in_year(anchor.month, anchor.day, today.year + 1)
        return candidate
    return anchor if anchor >= today else None


def collect(entries: list, *, now: datetime.datetime) -> list[dict]:
    """48h 窓内の due を近い順に返す純関数。

    窓は [JST 今日 00:00, now + WINDOW_HOURS]。始端を今日 00:00 にするのは、
    「今日の日」を夜になっても告げ続けるため (深夜ビートで消えない)。
    終端は境界を含む。entries は validate.py を通った形を期待するが、
    壊れたエントリは黙って飛ばさず ValueError を投げる (告げる側が勝手に
    間引きすると、抜け落ちた日に誰も気づかない)。
    """
    jst_now = _as_jst(now)
    today = jst_now.date()
    horizon = jst_now + datetime.timedelta(hours=WINDOW_HOURS)
    start = datetime.datetime.combine(today, datetime.time.min, tzinfo=JST)

    due: list[tuple[datetime.date, dict]] = []
    for entry in entries:
        try:
            anchor = parse_date(entry["date"])
            repeat = entry.get("repeat", "none")
            occ = occurrence_date(anchor, repeat, today)
        except KeyError as e:
            raise ValueError(f"必須キーが無い: {entry!r} ({e})") from None
        except (TypeError, ValueError) as e:
            raise ValueError(f"{entry.get('title', '?')}: {e}") from None
        if occ is None:
            continue
        occ_dt = datetime.datetime.combine(occ, datetime.time.min, tzinfo=JST)
        if start <= occ_dt <= horizon:
            due.append((occ, entry))
    due.sort(key=lambda pair: pair[0])
    return [
        {
            "date": occ.isoformat(),
            "days_ahead": (occ - today).days,
            "label": _LABELS.get((occ - today).days, f"{occ.month}/{occ.day}"),
            "title": entry["title"],
            "note": entry.get("note", ""),
        }
        for occ, entry in due
    ]


def format_line(item: dict) -> str:
    d = datetime.date.fromisoformat(item["date"])
    line = f"{item['label']} {d.month}/{d.day} {item['title']}"
    if item["note"]:
        line += f"（{item['note']}）"
    return line


def render(entries: list, *, now: datetime.datetime) -> str:
    """1〜3 行の日本語文面。何も無ければそのことを 1 行で告げる。"""
    items = collect(entries, now=now)
    if not items:
        return f"直近 {WINDOW_HOURS} 時間で告げる日はありません。"
    lines = [format_line(item) for item in items]
    rest = len(lines) - MAX_LINES + 1
    if len(lines) > MAX_LINES:
        # 全体で最大 3 行に収める: 2 件見せて残りは件数だけ
        lines = lines[:MAX_LINES - 1] + [f"ほか {rest} 件"]
    return "\n".join(lines)


def load_ledger(path: Path) -> list:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path}: エントリの配列であること")
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--now", default=None, help="ISO8601。省略時は現在時刻")
    args = parser.parse_args(argv)
    now = (
        datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.datetime.now(JST)
    )
    print(render(load_ledger(args.ledger), now=now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
