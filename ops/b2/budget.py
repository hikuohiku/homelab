#!/usr/bin/env python3
"""B2 download cap の事前歯止め (P-0216)。

2026-08-22 夜、Backblaze B2 アカウントの download cap 超過
(``download_cap_exceeded``, HTTP 403) で夜間 backup Job が全滅した
(一次原因の調査: ops/projects/logs/P-0111/root_cause.md)。restore drill・bit rot
読み・週次 restic 健康診断など消費者は今後確実に増え、個別には正しいジョブでも
「合計を見ている者がいない」限り cap を超える。このスクリプトは apps/ の manifest
から B2 消費者を機械抽出して台帳 1 枚にし、「cap を食い潰すスケジュール」を CI
が落とすようにする。P-0128 の download-ledger CronJob (完了実績の事後帳簿) とは
別論点で、こちらは静的分析のみ。B2 API は叩かない。

cap の性質 (P-0111 root_cause.md セッション 4–5 で確定・実測済み):

- アカウント単位。鍵の種類 (append-only / full-permission) に無関係
- usage counter は毎日 00:00 UTC にリセット (公式ドキュメント +
  p0111-cap-watch の 2026-08-23T00:04Z 回復実測)
- cap の実値 (何バイトか) は B2 コンソールの Caps & Alerts にしかなく repo 外。
  管理コンソール作業は人間専有 (CHARTER §4) のためここでは決め打ちしない。
  --cap-bytes または環境変数 B2_DAILY_CAP_BYTES を与えたときだけ
  検査 (a) 合計 vs cap が有効になり、無指定なら unconfigured として正直に
  スキップする (download_budget.py の DEFAULT_DAILY_CAP_BYTES=None と同じ線引き)

使い方::

    python3 ops/b2/budget.py                 # 台帳を表示して検査 (違反なら rc=1)
    python3 ops/b2/budget.py --check         # 同上。CI (.github/workflows/ci.yml) はこちら
    python3 ops/b2/budget.py --check --cap-bytes 1073741824

rc: 0 = 違反なし / 1 = 違反あり (実名を挙げる) / 2 = manifest を解釈できない等、
検査以前の問題。黙って通すより落ちる側に倒す。

schedule の評価時刻: node01 の kube-controller-manager は TZ=Asia/Tokyo で動くため、
manifest の cron 式は JST で評価される (spec.timeZone を書く流儀はまだ無い。
docs/backup.md 各節参照)。このスクリプトは cron 式を JST として解釈し UTC へ換算
してから判定する。spec.timeZone に Asia/Tokyo 以外が書かれていたら解析不能として
rc=2 で落ちる — 黙って別の時刻として通すより、評価基準が変わったことに気づける
ほうがよい。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = ROOT / "apps"

MIB = 1024 * 1024

# --- 推定値 (出所ごと書く。docs/backup.md の「想定転送量」と LEDGER_RULES と同値を保つこと) ---

# 出所: apps/{immich,vaultwarden,coder,syncthing}/download-ledger-cronjob.yaml の
# LEDGER_RULES env (P-0128) と同一値。「桁感であり実測ではない」(同ファイル冒頭コメント)。
# backup は repo open 時の config/index 読みが支配的でデータ総量に比例しない
# (日次差分の stored 実測は 158 KiB〜2.4 MiB、2026-08-10、docs/backup.md)。
# retention (forget --prune) は書き戻す pack の読み直し + index 再読みで削除量依存。
EST_BACKUP_BYTES = 32 * MIB
EST_RETENTION_BYTES = 512 * MIB

# 「重い消費者」の閾値。密集・境界の検査対象になる線。retention (512 MiB) が
# 引っかかり backup (32 MiB) が引っかからない位置に出所は設計判断 (数値の根拠ではない)。
HEAVY_THRESHOLD_BYTES = 256 * MIB

# 同じ UTC 曜日に重い消費者がこの間隔以内で開始したら密集とみなす。
# 2026-08-22 の実事故は土曜 18:45–19:50Z に retention 5 本全部だった。
# retention 1 回の所要 (数分〜1 時間程度、root_cause.md のログ観測) を吸収できる幅。
CLUSTER_WINDOW_MINUTES = 60

# 重い消費者が 00:00 UTC のリセット境界からこの幅以内で開始したら落とす。
# 境界直前は枯れかけの枠を竸り、境界直後は新鮮な枠を最初に食い潰す。
# どちらも他の消費者との取り合いを構造的に悪くする。
BOUNDARY_MARGIN_MINUTES = 120

# 検査 (a): 最悪日の想定転送量が 日次 cap × この係数 以内であること。
# 見積もりが「桁感」であることとクラスタ外の未知の消費者 (人間のコンソール利用等)
# の余白を取る設計判断であり、実測に裏付けられた定数ではない。
SAFETY_FACTOR = 0.8

CAP_ENV = "B2_DAILY_CAP_BYTES"

MINUTES_PER_DAY = 24 * 60
JST_UTC_OFFSET_MINUTES = 9 * 60

# cron の曜日番号 (0=日曜 … 6=土曜)。UTC 換算後も同じ番号系で扱う。
DOW_JA = ["日", "月", "火", "水", "木", "金", "土"]

# B2 消費者の判定。「RESTIC_REPOSITORY の b2: プレフィックス または
# *restic*-credentials Secret 参照」(PROJECT.md 設計方針どおり)。b2: リテラルは
# ConfigMap 埋め込みスクリプト内のテンプレート (workspace-home オーケストレータ)
# も拾えるようファイル全体を見る。その場合そのファイルの全 CronJob が消費者扱いに
# なる — 混在ファイルでの見逃しより過剰登録のほうが安全 (fail-closed)。
B2_LITERAL_RE = re.compile(r"\bb2:")
CREDENTIAL_REF_RE = re.compile(r"[a-z0-9-]*restic[a-z0-9-]*-credentials")

# 台帳本体: CronJob 名 → 推定値。ここに無い B2 消費者が manifest に混ざったら
# --check が落とし、逆にここにある名前が manifest から消えたら stale として落とす。
# 推定値を変えるときは docs/backup.md の「想定転送量」と LEDGER_RULES も一緒に。
REGISTRY = {
    "immich-restic-backup": {"bytes": EST_BACKUP_BYTES},
    "vaultwarden-restic-backup": {"bytes": EST_BACKUP_BYTES},
    "coder-restic-backup": {"bytes": EST_BACKUP_BYTES},
    "coder-workspace-home-backup": {"bytes": EST_BACKUP_BYTES},
    "syncthing-restic-backup": {"bytes": EST_BACKUP_BYTES},
    "immich-restic-retention": {"bytes": EST_RETENTION_BYTES},
    "vaultwarden-restic-retention": {"bytes": EST_RETENTION_BYTES},
    "coder-restic-retention": {"bytes": EST_RETENTION_BYTES},
    "coder-workspace-home-backup-retention": {"bytes": EST_RETENTION_BYTES},
    "syncthing-restic-retention": {"bytes": EST_RETENTION_BYTES},
}


class ManifestError(Exception):
    """manifest を解釈できない。rc=2 (検査以前の問題) に対応する。"""


def _expand_field(field: str, lo: int, hi: int, label: str) -> set:
    """cron 1 フィールド ("*", "45", "0,30", "10-20", "*/15") を値集合へ。

    曜日名 (SUN 等) は未対応 — 現行 manifest は数字のみなので、来たら落とす。
    """
    values = set()
    for part in field.split(","):
        range_part, step = part, 1
        if "/" in part:
            range_part, _, raw_step = part.partition("/")
            try:
                step = int(raw_step)
            except ValueError:
                raise ManifestError(f"{label}: ステップが数値でない: {part!r}")
            if step < 1:
                raise ManifestError(f"{label}: ステップが不正: {part!r}")
        if range_part == "*":
            start, end = lo, hi
        elif "-" in range_part:
            a, _, b = range_part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise ManifestError(f"{label}: 範囲が数値でない: {part!r}")
        else:
            try:
                start = int(range_part)
            except ValueError:
                raise ManifestError(
                    f"{label}: 数値・*・範囲以外 (例えば曜日名) は未対応: {part!r}"
                )
            end = hi if step > 1 else start
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ManifestError(f"{label}: 範囲が不正: {part!r}")
        values.update(range(start, end + 1, step))
    return values


def parse_schedule(expr: str) -> list:
    """cron 式 (JST 評価) を発火時刻 (UTC 曜日, UTC 分) のリストへ。

    dom/month の非 "*" は現行 manifest に存在しないので未対応 (rc=2 で落とす)。
    戻り値の要素は (utc_dow, utc_minute_of_day)。毎日のジョブは 7 要素、
    週次のジョブは 1 要素になる (JST→UTC の日跨ぎも含む)。
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ManifestError(f"cron 式が 5 フィールドでない: {expr!r}")
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    if dom_f != "*" or month_f != "*":
        raise ManifestError(
            f"dom/month の非 '*' は未対応 (必要になったら budget.py を広げること): {expr!r}"
        )
    minutes = _expand_field(minute_f, 0, 59, f"minute ({expr!r})")
    hours = _expand_field(hour_f, 0, 23, f"hour ({expr!r})")
    dows = (
        {d % 7 for d in _expand_field(dow_f, 0, 7, f"dow ({expr!r})")}
        if dow_f != "*"
        else set(range(7))
    )
    fires = set()
    for h in hours:
        for m in minutes:
            utc_total = h * 60 + m - JST_UTC_OFFSET_MINUTES
            day_shift = -1 if utc_total < 0 else 0
            for d in dows:
                fires.add(((d + day_shift) % 7, utc_total % MINUTES_PER_DAY))
    return sorted(fires)


def _touches_b2(doc: dict, file_touches_b2: bool) -> bool:
    if file_touches_b2:
        return True
    text = yaml.safe_dump(doc, allow_unicode=True)
    return bool(CREDENTIAL_REF_RE.search(text))


def collect_consumers(apps_dir: Path) -> list:
    """apps/ 配下の manifest を走査し、B2 消費者 (CronJob) の台帳を作る。

    判定は静的 (B2_LITERAL_RE / CREDENTIAL_REF_RE) で、k8s API には触れない。
    戻り値は dict のリスト: name / namespace / file / cron / fires。
    """
    consumers = []
    seen_names = {}
    for path in sorted(apps_dir.rglob("*.yaml")):
        rel = path.relative_to(apps_dir).as_posix()
        if "/charts/" in f"/{rel}":
            continue
        text = path.read_text()
        file_touches_b2 = bool(B2_LITERAL_RE.search(text))
        try:
            docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
        except yaml.YAMLError as e:
            raise ManifestError(f"apps/{rel}: YAML パースに失敗: {e}")
        for doc in docs:
            if doc.get("kind") != "CronJob":
                continue
            meta = doc.get("metadata") or {}
            name = meta.get("name")
            ns = meta.get("namespace") or "(namespace なし)"
            spec = doc.get("spec") or {}
            sched = spec.get("schedule")
            if not isinstance(sched, str):
                raise ManifestError(f"apps/{rel}: {name}: schedule がないか文字列でない")
            tz = spec.get("timeZone")
            if tz is not None and tz != "Asia/Tokyo":
                raise ManifestError(
                    f"apps/{rel}: {name}: spec.timeZone={tz!r} は解釈できない。"
                    "node01 の流儀は「spec.timeZone を書かず JST 評価」。"
                    "これを変えるなら ops/b2/budget.py の換算も一緒に更新すること"
                )
            if not _touches_b2(doc, file_touches_b2):
                continue
            if name in seen_names:
                raise ManifestError(
                    f"apps/{rel}: CronJob 名 {name!r} が二重 "
                    f"(apps/{seen_names[name]} にもある)"
                )
            seen_names[name] = rel
            consumers.append(
                {
                    "name": name,
                    "namespace": ns,
                    "file": rel,
                    "cron": sched,
                    "fires": parse_schedule(sched),
                }
            )
    return consumers


def _fmt_utc(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}Z"


def _fmt_fires(fires: list) -> str:
    minutes = sorted({m for _, m in fires})
    if len(minutes) == 1 and len(fires) == 7:
        return f"毎日 {_fmt_utc(minutes[0])}"
    return ", ".join(f"{DOW_JA[d]} {_fmt_utc(m)}" for d, m in sorted(fires))


def evaluate(consumers: list, cap_bytes=None, registry=None) -> list:
    """純関数。台帳と cap から違反メッセージのリストを返す (空 = 全部合格)。

    registry はテストや将来の呼び出し元が合成した台帳を差し込めるようにした
    注入口で、省略時はモジュールの REGISTRY を使う。

    検査の系譜は PROJECT.md DoD(2) どおり:
      (a) 最悪日の想定転送量 vs 日次 cap × 安全係数 (cap 未設定ならスキップ)
      (b) 重い消費者の密集 — 同一 UTC 曜日での接近 / リセット境界への接近
      (c) 台帳未登録の B2 消費者の混入 (+ 台帳側の stale エントリ)
    """
    if registry is None:
        registry = REGISTRY
    problems = []
    heavy_by_dow = defaultdict(list)
    bytes_by_dow = defaultdict(int)

    unknown = []
    for c in sorted(consumers, key=lambda x: x["name"]):
        est = registry.get(c["name"])
        if est is None:
            unknown.append(c["name"])
            continue
        for dow, minute in c["fires"]:
            bytes_by_dow[dow] += est["bytes"]
            if est["bytes"] >= HEAVY_THRESHOLD_BYTES:
                heavy_by_dow[dow].append((minute, c["name"]))
    if unknown:
        problems.append(
            "(c) B2 を使う CronJob が台帳未登録: "
            + ", ".join(sorted(unknown))
            + "。ops/b2/budget.py の REGISTRY に推定値を登録し、docs/backup.md の"
            "「想定転送量」節に根拠を追記すること (登録せず merge すると誰も合計を"
            "見ていない状態に戻る)"
        )

    for name in sorted(set(registry) - set(c["name"] for c in consumers)):
        problems.append(
            f"(c) 台帳の {name} が manifest から消えている。CronJob を削除・改名した"
            "なら REGISTRY からも消すこと (残すと合計見積もりが嘘をつく)"
        )

    worst_dow = max(bytes_by_dow, key=lambda d: bytes_by_dow[d], default=None)
    worst_bytes = bytes_by_dow[worst_dow] if worst_dow is not None else 0
    if cap_bytes is not None and worst_bytes > cap_bytes * SAFETY_FACTOR:
        problems.append(
            f"(a) 最悪日 ({DOW_JA[worst_dow]}曜) の想定転送量 {worst_bytes / MIB:.0f} MiB が"
            f" 日次 cap × 安全係数 ({cap_bytes / MIB:.0f} MiB × {SAFETY_FACTOR}) を超える。"
            "推定値の棚卸し、schedule 分散、または人間による cap 見直しのいずれかが要る"
        )

    for dow in sorted(heavy_by_dow):
        entries = sorted(heavy_by_dow[dow])
        for (m1, n1), (m2, n2) in zip(entries, entries[1:]):
            gap = m2 - m1
            if gap <= CLUSTER_WINDOW_MINUTES:
                problems.append(
                    f"(b) 重い消費者が同一時間帯に密集: {n1} ({_fmt_utc(m1)}) と "
                    f"{n2} ({_fmt_utc(m2)}) が同じ UTC 曜日 ({DOW_JA[dow]}曜) に "
                    f"{gap} 分差で始まる。曜日を分散させるか "
                    f"{CLUSTER_WINDOW_MINUTES} 分以上空けること"
                )

    flagged_boundary = set()
    for dow in sorted(heavy_by_dow):
        for minute, name in sorted(heavy_by_dow[dow], key=lambda x: x[1]):
            if name in flagged_boundary:
                continue
            dist = min(minute, MINUTES_PER_DAY - minute)
            if dist <= BOUNDARY_MARGIN_MINUTES:
                flagged_boundary.add(name)
                problems.append(
                    f"(b) {name} がリセット境界 (00:00 UTC) から "
                    f"{BOUNDARY_MARGIN_MINUTES} 分以内に開始する ({_fmt_utc(minute)})。"
                    "枯れかけ/新鮮な枠を最初に食い潰す位置になるため、"
                    "もっと境界から離すこと"
                )
    return problems


def worst_day(consumers: list, registry=None) -> tuple:
    """(UTC 曜日, その日の想定転送量バイト)。台帳に載らない消費者は 0 扱い。"""
    if registry is None:
        registry = REGISTRY
    per_dow = defaultdict(int)
    for c in consumers:
        est = registry.get(c["name"])
        if est is None:
            continue
        for dow, _ in c["fires"]:
            per_dow[dow] += est["bytes"]
    if not per_dow:
        return None, 0
    dow = max(per_dow, key=lambda d: per_dow[d])
    return dow, per_dow[dow]


def render_cap_status(cap_bytes, consumers: list) -> str:
    """検査 (a) の状態を 1 行で。unconfigured の沈黙は決め打ちをしないための仕様。"""
    dow, worst = worst_day(consumers)
    label = f"{DOW_JA[dow]}曜" if dow is not None else "-"
    if cap_bytes is None:
        return (
            "(a) 合計 vs 日次 cap: unconfigured — cap 実値は B2 コンソールにしかない。"
            f"--cap-bytes または {CAP_ENV} で設定すると有効化される "
            f"(参考: 最悪日 {label} = {worst / MIB:.0f} MiB)"
        )
    budget = int(cap_bytes * SAFETY_FACTOR)
    return (
        f"(a) 合計 vs 日次 cap: 最悪日 {label} = {worst / MIB:.0f} MiB / "
        f"予算 {budget / MIB:.0f} MiB (cap {cap_bytes / MIB:.0f} MiB × 安全係数 {SAFETY_FACTOR})"
    )


def render_ledger(consumers: list) -> str:
    lines = [
        "=== B2 download 消費者台帳 (manifest から機械抽出、JST cron → UTC 換算) ===",
        f"{'CronJob':<42} {'ns':<11} {'想定/回':>9}  {'発火 (UTC)':<28} {'リセットまで':>7}",
    ]
    for c in sorted(consumers, key=lambda x: x["name"]):
        est = REGISTRY.get(c["name"])
        size = f"{est['bytes'] / MIB:.0f} MiB" if est else "未登録!"
        reset_h = (
            f"{(MINUTES_PER_DAY - c['fires'][0][1]) / 60:.0f}h"
            if len({m for _, m in c["fires"]}) == 1
            else "-"
        )
        lines.append(
            f"{c['name']:<42} {c['namespace']:<11} {size:>9}  "
            f"{_fmt_fires(c['fires']):<28} {reset_h:>7}"
        )
    return "\n".join(lines)


def resolve_cap(cli_value) -> int | None:
    if cli_value is not None:
        return cli_value
    env = os.environ.get(CAP_ENV, "").strip()
    if not env:
        return None
    try:
        return int(env)
    except ValueError:
        raise ManifestError(
            f"{CAP_ENV} が整数でない: {env!r} (unconfigured のままにするなら unset すること)"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="B2 download cap の事前歯止め (P-0216)。--check は CI 用。"
    )
    parser.add_argument("--check", action="store_true", help="CI 用。意味上の違いはない (常に rc が判定を伝える)")
    parser.add_argument("--cap-bytes", type=int, default=None,
                        help=f"日次 cap の実値 (バイト)。未指定なら {CAP_ENV}、それも無ければ unconfigured")
    args = parser.parse_args(argv)
    try:
        cap_bytes = resolve_cap(args.cap_bytes)
        consumers = collect_consumers(APPS_DIR)
    except ManifestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(render_ledger(consumers))
    baseline = sum(
        est["bytes"]
        for c in consumers
        if (est := REGISTRY.get(c["name"])) and len({d for d, _ in c["fires"]}) == 7
    )
    print(f"--- 毎日固定分: {baseline / MIB:.0f} MiB/日 (日次 backup 5 本)")
    print(render_cap_status(cap_bytes, consumers))
    problems = evaluate(consumers, cap_bytes)
    if problems:
        print("--- 違反:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("--- 違反なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
