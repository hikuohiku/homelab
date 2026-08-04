#!/usr/bin/env python3
"""ops/ 配下の状態ファイルの不変条件を検査する。

autopilot が自分の状態を壊さないための最後の砦。CI から呼ばれる。
標準ライブラリのみ（実行環境に何も入っていなくても動くこと）。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

OPS = pathlib.Path(__file__).parent
ROOT = OPS.parent

STATUSES = {"todo", "in_progress", "blocked", "needs-human", "done", "dropped"}
RISKS = {"low", "medium", "high"}
DOMAINS = {"homelab"}
REQUIRED_TASK_KEYS = {"id", "domain", "title", "kind", "risk", "status", "priority", "why", "dod", "created"}

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def load(name: str):
    path = OPS / name
    if not path.exists():
        err(f"{name} が存在しない")
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        err(f"{name} が JSON として壊れている: {e}")
        return None


def check_backlog(b) -> None:
    tasks = b.get("tasks")
    if not isinstance(tasks, list):
        err("backlog.json: tasks が配列でない")
        return

    seen: set[str] = set()
    max_num = 0
    for i, t in enumerate(tasks):
        where = f"backlog.json tasks[{i}] ({t.get('id', '?')})"

        missing = REQUIRED_TASK_KEYS - set(t)
        if missing:
            err(f"{where}: 必須キー不足 {sorted(missing)}")

        tid = t.get("id", "")
        if not re.fullmatch(r"T-\d{4}", tid):
            err(f"{where}: id は T-0000 形式であること")
        elif tid in seen:
            err(f"{where}: id が重複している")
        else:
            seen.add(tid)
            max_num = max(max_num, int(tid[2:]))

        if t.get("status") not in STATUSES:
            err(f"{where}: status={t.get('status')!r} は {sorted(STATUSES)} のいずれか")
        if t.get("risk") not in RISKS:
            err(f"{where}: risk={t.get('risk')!r} は {sorted(RISKS)} のいずれか")
        if t.get("domain") not in DOMAINS:
            err(f"{where}: domain={t.get('domain')!r} は {sorted(DOMAINS)} のいずれか（増やすときは VISION.md も更新）")
        if not isinstance(t.get("priority"), int):
            err(f"{where}: priority は整数")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(t.get("created", ""))):
            err(f"{where}: created は YYYY-MM-DD")

        # CHARTER §4: needs-human は「手が届かない」タスクのみ。判断の丸投げは禁止
        if t.get("status") == "needs-human":
            reason = t.get("needs_human_reason", "")
            if not reason:
                err(f"{where}: needs-human には needs_human_reason（人間に何をしてほしいか）が必要")
            elif re.search(r"判断|決め|方針|相談|確認して|どうする", reason):
                err(
                    f"{where}: needs_human_reason が判断の依頼になっている。"
                    f"人間に渡してよいのは権限・認証の手作業だけで、判断は自分の仕事 (CHARTER §4)"
                )
        if t.get("status") == "blocked" and not t.get("blocked_by"):
            err(f"{where}: blocked には blocked_by が必要")
        if t.get("status") == "done" and not t.get("pr"):
            warn(f"{where}: done なのに pr が空（PR を伴わない完了なら理由を notes に）")

        for ref in t.get("refs", []):
            # ディレクトリ表記や注記付きは緩く見る
            base = ref.split(" ")[0].rstrip("/")
            if base and not (ROOT / base).exists():
                warn(f"{where}: refs の {ref} が存在しない（移動・削除されたなら refs を直す）")

    # in_progress が溜まると「やりかけ」が積む
    running = [t["id"] for t in tasks if t.get("status") == "in_progress"]
    if len(running) > 3:
        err(f"backlog.json: in_progress が {len(running)} 件溜まっている {running}。着手より後始末を優先すること (CHARTER §2)")

    nxt = b.get("next_id")
    if not isinstance(nxt, int) or nxt <= max_num:
        err(f"backlog.json: next_id={nxt} は既存の最大 id ({max_num}) より大きいこと")

    if not any(t.get("status") == "todo" for t in tasks):
        warn("backlog.json: todo が 1 件も無い。次の起動で調査タスクを起票すること (CHARTER §3)")


def check_inventory(inv) -> None:
    targets = inv.get("targets")
    if not isinstance(targets, list):
        err("inventory.json: targets が配列でない")
        return
    seen: set[str] = set()
    for i, t in enumerate(targets):
        where = f"inventory.json targets[{i}] ({t.get('id', '?')})"
        for k in ("id", "kind", "name", "current", "file", "upstream", "policy"):
            if not t.get(k):
                err(f"{where}: {k} が空")
        if t.get("id") in seen:
            err(f"{where}: id が重複している")
        seen.add(t.get("id"))
        if t.get("policy") not in {"auto", "manual", "pinned"}:
            err(f"{where}: policy={t.get('policy')!r} は auto/manual/pinned のいずれか")
        f = t.get("file")
        if f and not (ROOT / f).exists():
            err(f"{where}: file {f} が存在しない")


def check_state(s) -> None:
    for k in ("updated", "runs", "feedback"):
        if k not in s:
            err(f"state.json: {k} が無い")
    fb = s.get("feedback", {})
    if not fb.get("issue"):
        warn("state.json: feedback.issue が未設定（フィードバック窓口が無い）")


def main() -> int:
    backlog = load("backlog.json")
    inventory = load("inventory.json")
    state = load("state.json")

    for name in ("VISION.md", "CHARTER.md"):
        if not (OPS / name).exists():
            err(f"{name} が存在しない。autopilot の起動手順が壊れる")

    if backlog:
        check_backlog(backlog)
    if inventory:
        check_inventory(inventory)
    if state:
        check_state(state)

    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"::error::{e}" if "GITHUB_ACTIONS" in __import__("os").environ else f"error: {e}")

    print(f"\n{len(errors)} error, {len(warnings)} warning")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
