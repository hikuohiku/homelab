#!/usr/bin/env python3
"""ops/ 配下の状態ファイルの不変条件を検査する。

autopilot が自分の状態を壊さないための最後の砦。CI から呼ばれる。
標準ライブラリのみ（実行環境に何も入っていなくても動くこと）。
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys

import ledger

OPS = pathlib.Path(__file__).parent
ROOT = OPS.parent

STATUSES = {"todo", "in_progress", "blocked", "needs-human", "done", "dropped"}
RISKS = {"low", "medium", "high"}
DOMAINS = {"homelab"}
REQUIRED_TASK_KEYS = {"id", "domain", "title", "kind", "risk", "status", "priority", "why", "dod", "created"}

# CHARTER.md の肥大化検知の上限。backlog.json の上限 (ledger.BACKLOG_MAX_BYTES)
# を参考に決めた（T-0164 DoD(2)）。まだ archive 機構（DoD(1)）が無いので、
# 圧縮の締め切りというより「気づく」ためのしきい値。
CHARTER_MAX_BYTES = 120_000

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
        not_before = t.get("not_before")
        if not_before is not None:
            try:
                datetime.datetime.fromisoformat(str(not_before).replace("Z", "+00:00"))
            except ValueError:
                err(f"{where}: not_before={not_before!r} は ISO8601 として解釈できない")
        if t.get("status") == "done" and not t.get("pr") and not t.get("notes"):
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


CONFLICT_MARKER_PATTERNS = [
    re.compile(r"^<{7}(?:\s|$)"),
    re.compile(r"^={7}$"),
    re.compile(r"^>{7}(?:\s|$)"),
]


def check_conflict_markers() -> None:
    # run #7 (#80): journal のコンフリクト解消で `>>>>>>> ...` の消し忘れをそのまま merge してしまった。
    # kustomize build / terraform validate は markdown/json の中身までは見ないので、ここで拾う。
    for path in sorted(OPS.rglob("*")):
        if not path.is_file() or path.suffix not in (".md", ".json"):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(pat.match(line) for pat in CONFLICT_MARKER_PATTERNS):
                err(f"{path.relative_to(ROOT)}:{lineno}: git の conflict marker が残っている ({line!r})")


REVIEW_LOG_ENTRY_RE = re.compile(r"^## (R-\d+) — .*$", re.MULTILINE)
REVIEW_LOG_STATUS_RE = re.compile(r"^- 状態:\s*(.+)$", re.MULTILINE)
REVIEW_LOG_FILED_RE = re.compile(r"起票済\s*(T-\d{4})")
TASK_R_REF_RE = re.compile(r"R-\d+")


def parse_review_log(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    matches = list(REVIEW_LOG_ENTRY_RE.finditer(text))
    for i, m in enumerate(matches):
        rid = m.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.end():end]
        status_m = REVIEW_LOG_STATUS_RE.search(block)
        entries[rid] = status_m.group(1).strip() if status_m else ""
    return entries


def check_review_log(backlog) -> None:
    # T-0154: review-log.md の R-NNN (状態: 起票済 T-XXXX) と backlog task の why に
    # 含まれる R-NNN 参照が食い違っていないかを検査する。レビュー役はまだ一度も実行されて
    # おらず運用実績が無いため、初回は warning に留める（error 昇格は数サイクル運用後に検討）。
    path = OPS / "review-log.md"
    if not path.exists():
        return
    entries = parse_review_log(path.read_text())
    if not entries:
        return

    tasks = backlog.get("tasks", []) if backlog else []
    tasks_by_id = {t.get("id"): t for t in tasks}

    why_refs: dict[str, list[str]] = {}
    for t in tasks:
        for rid in TASK_R_REF_RE.findall(t.get("why", "") or ""):
            why_refs.setdefault(rid, []).append(t.get("id", "?"))

    for rid, status in entries.items():
        m = REVIEW_LOG_FILED_RE.search(status)
        if not m:
            continue
        tid = m.group(1)
        task = tasks_by_id.get(tid)
        if task is None:
            warn(f"review-log.md: {rid} は「起票済 {tid}」だが backlog.json に {tid} が存在しない")
            continue
        if rid not in (task.get("why", "") or ""):
            warn(f"review-log.md: {rid} は「起票済 {tid}」だが {tid} の why に {rid} が含まれていない")

    for rid, tids in why_refs.items():
        if rid not in entries:
            warn(f"backlog.json: {', '.join(tids)} の why が {rid} を参照しているが review-log.md に {rid} のエントリが無い")


def check_state(s) -> None:
    for k in ("updated", "runs", "feedback"):
        if k not in s:
            err(f"state.json: {k} が無い")
    fb = s.get("feedback", {})
    if not fb.get("issue"):
        warn("state.json: feedback.issue が未設定（フィードバック窓口が無い）")


JOURNAL_HEADING_RE = re.compile(r"^## .* — run #(\d+)", re.MULTILINE)
JOURNAL_RUN_REF_RE = re.compile(r"journal run\s*#(\d+)")


def check_runs_freshness(s) -> None:
    # T-0145: state.json の runs 配列が journal の run 番号を追わず抜け続けていた
    # (run #126〜#132 の7件が欠落したまま気づかれなかった)。runs[].n は歴史的経緯で
    # journal の run 番号と1:1に対応しなくなっている（過去の欠落がそのままズレとして残る
    # ため）ので、n の連番ではなく summary 中の `journal run #N` という明示的な記述
    # (CHARTER §7 で追記を義務化) を頼りに、直近の journal run まで追随できているかを見る。
    runs = s.get("runs")
    if not isinstance(runs, list) or not runs:
        return

    journal_dir = OPS / "journal"
    journal_files = sorted(journal_dir.glob("*.md")) if journal_dir.is_dir() else []
    if not journal_files:
        warn("journal/ にファイルが無く、runs 配列の journal 追随を検証できない")
        return

    latest_journal = journal_files[-1]
    run_numbers = [int(n) for n in JOURNAL_HEADING_RE.findall(latest_journal.read_text())]
    if not run_numbers:
        warn(f"{latest_journal.name}: `## ... — run #N` の見出しが見つからず追随を検証できない")
        return
    journal_max = max(run_numbers)

    documented = [
        int(m.group(1))
        for r in runs
        for m in [JOURNAL_RUN_REF_RE.search(r.get("summary", ""))]
        if m
    ]
    if not documented:
        warn(
            "state.json: runs[].summary に `journal run #N` の記述が1件も無く、"
            "journal との追随を機械検証できない (CHARTER §7)"
        )
        return

    runs_max = max(documented)
    gap = journal_max - runs_max
    if gap > 0:
        err(
            f"state.json: runs 配列が journal の run #{journal_max} まで追随していない"
            f"（runs 側で確認できる最新は run #{runs_max}、{gap} 件分が未反映）。"
            f"journal に追記するのと同じタイミングで runs 配列にも 1 件追記すること (CHARTER §7)"
        )


def check_ledger_size() -> None:
    """熱い帳簿の大きさを縛る。

    autopilot は毎イテレーション帳簿を全部読み直すので、太った分だけ観測の
    余地が削られる。一度掃除するだけでは必ず再び太るため、上限そのものを
    検査に入れる。超えたら `python3 ops/ledger.py` を実行して archive へ移す。
    """
    for name, limit in (
        ("backlog.json", ledger.BACKLOG_MAX_BYTES),
        ("state.json", ledger.STATE_MAX_BYTES),
    ):
        path = OPS / name
        if not path.exists():
            continue
        size = path.stat().st_size
        if size > limit:
            err(
                f"{name}: {size:,} bytes は上限 {limit:,} を超えている。"
                "`python3 ops/ledger.py` で archive へ移すこと"
            )


def check_charter_size() -> None:
    """CHARTER.md の肥大化を検知する。

    2026-08-04 の 10.8 KiB から 2026-08-06 時点で 86.1 KiB まで 2 日で約8倍に
    増えた実績があり（ops/review-log.md R-004）、backlog/state と同じ問題
    （毎イテレーション読み直すコストが観測の余地を削る）を抱える。ルール本体と
    経緯を切り分けて archive 先へ移す仕組み（T-0164 DoD(1)）はまだ実施していない
    ため、超過時にできるのはまだ「気づく」ことだけ。超えたら各ルールの経緯部分
    （「issue #56, <日時>の指摘。...」のような詳細な事例説明）を切り出して圧縮すること。
    """
    path = OPS / "CHARTER.md"
    if not path.exists():
        return
    size = path.stat().st_size
    if size > CHARTER_MAX_BYTES:
        err(
            f"CHARTER.md: {size:,} bytes は上限 {CHARTER_MAX_BYTES:,} を超えている。"
            "各ルールの経緯部分を切り出して圧縮すること (T-0164)"
        )


def main() -> int:
    # 参照の整合（blocked_by / review-log / next_id）は archive も含めて見ないと、
    # 済んだタスクを指す参照が「存在しない」と誤判定される。
    backlog = ledger.load_backlog(include_archive=True)
    inventory = load("inventory.json")
    state = load("state.json")

    check_ledger_size()
    check_charter_size()

    for name in ("VISION.md", "CHARTER.md"):
        if not (OPS / name).exists():
            err(f"{name} が存在しない。autopilot の起動手順が壊れる")

    if backlog:
        check_backlog(backlog)
        check_review_log(backlog)
    if inventory:
        check_inventory(inventory)
    if state:
        check_state(state)
        check_runs_freshness(state)

    check_conflict_markers()

    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"::error::{e}" if "GITHUB_ACTIONS" in __import__("os").environ else f"error: {e}")

    print(f"\n{len(errors)} error, {len(warnings)} warning")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
