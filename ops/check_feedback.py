#!/usr/bin/env python3
"""ops/feedback.json（人間からのフィードバックの台帳）の不変条件を検査する。

台帳が「書き置きがどうなったか」を人間に見せる唯一の面（ops/CHARTER.md §6）なので、
状態が中途半端なまま止まっているものを機械的に見つける。ops/validate.py と同じく
標準ライブラリのみ、CI（`ops` job）から呼ばれる。

- error: 構造が壊れている・台帳と backlog が食い違っている（直さないと先へ進めない）
- warning: 放置されている（人間の書き置きが読まれたまま動いていない）

`ops/validate.py` に混ぜず別ファイルにしているのは、既存の check_*.py（version_sync /
pvc_usage_script_sync / doc_commands）と同じ粒度に揃えるため。
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys

import ledger

OPS = pathlib.Path(__file__).parent

STATUSES = {"new", "accepted", "done", "declined"}
ID_RE = re.compile(r"^F-[A-Za-z0-9._-]{1,64}$")
TASK_RE = re.compile(r"^T-\d{4}$")
REQUIRED_KEYS = {"id", "source", "received", "body", "status"}

# new のまま何時間置かれたら「読まれたが動いていない」とみなすか。
# 計画役は PLAN_EVERY(6) イテレーション（実測 1 イテレーション約 9 分）ごとに回るので、
# 正常なら 1 時間以内に new から動く。倍の余裕を見る
STALE_NEW_HOURS = 3

errors: list[str] = []
warnings: list[str] = []


def parse_iso(v):
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def check(feedback, backlog) -> None:
    items = feedback.get("items")
    if not isinstance(items, list):
        errors.append("feedback.json: items が配列でない")
        return

    tasks = {t.get("id"): t for t in (backlog.get("tasks") or [])} if backlog else {}
    now = datetime.datetime.now(datetime.timezone.utc)
    seen: set[str] = set()
    refs: set[str] = set()

    for i, it in enumerate(items):
        where = f"feedback.json items[{i}] ({it.get('id', '?')})"

        missing = REQUIRED_KEYS - set(it)
        if missing:
            errors.append(f"{where}: 必須キー不足 {sorted(missing)}")
            continue

        fid = str(it.get("id"))
        if not ID_RE.match(fid):
            errors.append(f"{where}: id は F- で始まる英数字（書かれた時点の id を付け替えない）")
        elif fid in seen:
            errors.append(f"{where}: id が重複している（同じ書き置きを二重に取り込んでいる）")
        seen.add(fid)

        ref = it.get("ref")
        if ref:
            if ref in refs:
                errors.append(f"{where}: ref {ref} が他のエントリと重複している（同じ原本を二重に取り込んでいる）")
            refs.add(str(ref))

        if not str(it.get("body", "")).strip():
            errors.append(f"{where}: body が空（人間が書いた文章をそのまま残すこと）")

        received = parse_iso(it.get("received"))
        if received is None:
            errors.append(f"{where}: received={it.get('received')!r} は ISO8601 として解釈できない")

        status = it.get("status")
        if status not in STATUSES:
            errors.append(f"{where}: status={status!r} は {sorted(STATUSES)} のいずれか")
            continue

        task = it.get("task")
        if status in ("accepted", "done"):
            if not task:
                errors.append(f"{where}: {status} には task（起票した T-XXXX）が必要")
            elif not TASK_RE.match(str(task)):
                errors.append(f"{where}: task={task!r} は T-0000 形式であること")
            elif tasks and task not in tasks:
                errors.append(f"{where}: task {task} が backlog.json に無い")
        elif task:
            errors.append(f"{where}: status={status} なのに task={task} が付いている")

        if status in ("done", "declined") and not str(it.get("resolution", "") or "").strip():
            errors.append(
                f"{where}: {status} には resolution（何をしたか / なぜやらないかを人間に返す一行）が必要。"
                f"これが無いと書き置きの顛末がダッシュボードに出ない (CHARTER §6)"
            )

        # 放置の検出。error にしないのは、遅れているだけの回を落として
        # ループを止めるほうが害が大きいため（VISION「ループが止まる選択をしない」）
        if status == "new" and received is not None:
            hours = (now - received).total_seconds() / 3600
            if hours > STALE_NEW_HOURS:
                warnings.append(
                    f"{where}: new のまま {hours:.1f} 時間経っている。"
                    f"計画役は起票するか declined にすること (CHARTER §6)"
                )
        if status == "accepted" and task in tasks:
            st = tasks[task].get("status")
            if st == "done":
                warnings.append(f"{where}: task {task} は done だが台帳が accepted のまま。done にして resolution を書く")
            elif st == "dropped":
                warnings.append(f"{where}: task {task} は dropped だが台帳が accepted のまま。declined にして理由を書く")

    for key, sub in (("inbox", ("branch", "dir")), ("issue", ("number", "cutoff"))):
        block = feedback.get(key)
        if not isinstance(block, dict):
            errors.append(f"feedback.json: {key} が無い（書き置きの取り込み元が特定できない）")
            continue
        for k in sub:
            if not block.get(k):
                errors.append(f"feedback.json: {key}.{k} が空")


def main() -> int:
    path = OPS / "feedback.json"
    if not path.exists():
        print("error: ops/feedback.json が無い。人間からのフィードバックの台帳が失われている (CHARTER §6)")
        print("\n1 error, 0 warning")
        return 1
    try:
        feedback = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"error: ops/feedback.json が JSON として壊れている: {e}")
        print("\n1 error, 0 warning")
        return 1

    try:
        # done になった task は archive へ移るが、台帳からの参照は生き続ける
        backlog = ledger.load_backlog(include_archive=True)
    except (OSError, json.JSONDecodeError):
        backlog = None  # backlog 自体の異常は ops/validate.py が報告する

    check(feedback, backlog)

    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    print(f"\n{len(errors)} error, {len(warnings)} warning")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
