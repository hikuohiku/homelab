#!/usr/bin/env python3
"""帳簿（backlog / state）の読み書きを 1 箇所に集める。

**なぜ分けるのか**: autopilot は 1 イテレーションごとに VISION / CHARTER /
backlog / state を全部読み直す。2026-08-06 時点でその合計は 587 KiB あり、
うち約 9 割が done/dropped のタスクと過去の起動記録だった。二度と行動に
つながらない履歴を毎回読み直す分だけ、世界を観測する余地が削られている。

そこで **熱い帳簿（今から動かすもの）と archive（済んだもの）を物理的に分ける**。
autopilot が毎回読むのは熱い方だけでよい。参照の整合を検査するもの
（validate.py / check_feedback.py）と、履歴を見せるもの（dashboard/build.py）
だけが archive も併せて読む。

archive は消さない。過去の判断の理由は残す価値がある。読む頻度が違うだけ。
"""

import json
import pathlib

OPS = pathlib.Path(__file__).resolve().parent
ARCHIVE_DIR = OPS / "archive"

# 熱い帳簿に残す状態。ここに無い状態は archive 行き。
LIVE_STATUSES = ("todo", "in_progress", "blocked", "needs-human")

# state.json に残す起動記録の件数。直前の数回が読めれば引き継ぎには足りる
# （CHARTER §2 の「前回の中断を拾う」はオープン PR とブランチを見る作りで、
# runs を遡る必要は無い）。
RUNS_KEPT = 20

# 熱い帳簿のサイズ上限。超えたら validate.py が落ちる。
# 一度掃除するだけでは必ず再び太るので、上限そのものを検査に入れる。
# 2026-08-06 の圧縮直後で backlog ≈ 55 KiB / state ≈ 12 KiB。倍を上限にした。
BACKLOG_MAX_BYTES = 120_000
STATE_MAX_BYTES = 40_000


def _read(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_backlog(include_archive: bool = False) -> dict:
    """熱い backlog を返す。include_archive=True なら archive を合流させる。

    id の重複は熱い方を優先する（archive に居るはずのものが復活した場合、
    熱い方が現在の状態を持つ）。
    """
    hot = _read(OPS / "backlog.json", {"tasks": []})
    if not include_archive:
        return hot
    archived = _read(ARCHIVE_DIR / "backlog-done.json", {"tasks": []})
    seen = {t.get("id") for t in hot.get("tasks", [])}
    merged = dict(hot)
    merged["tasks"] = list(hot.get("tasks", [])) + [
        t for t in archived.get("tasks", []) if t.get("id") not in seen
    ]
    return merged


def load_state(include_archive: bool = False) -> dict:
    hot = _read(OPS / "state.json", {})
    if not include_archive:
        return hot
    archived = _read(ARCHIVE_DIR / "runs.json", {"runs": []})
    merged = dict(hot)
    merged["runs"] = list(archived.get("runs", [])) + list(hot.get("runs", []))
    return merged


def compact() -> list[str]:
    """終わったものを archive へ移す。何度実行してもよい。

    戻り値は人間向けの変更内容。何も動かなければ空。
    """
    changed = []

    backlog = _read(OPS / "backlog.json", None)
    if backlog is not None:
        tasks = backlog.get("tasks", [])
        live = [t for t in tasks if t.get("status") in LIVE_STATUSES]
        done = [t for t in tasks if t.get("status") not in LIVE_STATUSES]
        if done:
            archive = _read(ARCHIVE_DIR / "backlog-done.json", {"tasks": []})
            known = {t.get("id") for t in archive.get("tasks", [])}
            archive["tasks"] = archive.get("tasks", []) + [
                t for t in done if t.get("id") not in known
            ]
            archive["tasks"].sort(key=lambda t: str(t.get("id")))
            _write(ARCHIVE_DIR / "backlog-done.json", archive)
            backlog["tasks"] = live
            _write(OPS / "backlog.json", backlog)
            changed.append(f"backlog: {len(done)} 件を archive へ（残り {len(live)} 件）")

    state = _read(OPS / "state.json", None)
    if state is not None:
        runs = state.get("runs", [])
        if len(runs) > RUNS_KEPT:
            old, keep = runs[:-RUNS_KEPT], runs[-RUNS_KEPT:]
            archive = _read(ARCHIVE_DIR / "runs.json", {"runs": []})
            known = {r.get("n") for r in archive.get("runs", [])}
            archive["runs"] = archive.get("runs", []) + [
                r for r in old if r.get("n") not in known
            ]
            _write(ARCHIVE_DIR / "runs.json", archive)
            state["runs"] = keep
            _write(OPS / "state.json", state)
            changed.append(f"state: 起動記録 {len(old)} 件を archive へ（残り {len(keep)} 件）")

    return changed


if __name__ == "__main__":
    for line in compact() or ["変更なし"]:
        print(line)
    for name, limit in (("backlog.json", BACKLOG_MAX_BYTES), ("state.json", STATE_MAX_BYTES)):
        size = (OPS / name).stat().st_size
        print(f"{name}: {size:,} bytes / 上限 {limit:,}")
