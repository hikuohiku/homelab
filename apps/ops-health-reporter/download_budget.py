"""Backblaze B2 download cap の帳簿 (P-0128)。

2026-08-22 の backup 全滅の一次原因は B2 アカウント単位の download cap 超過
(download_cap_exceeded, ops/projects/logs/P-0111/root_cause.md)。だが cap を
消費するのは誰かを測る者はいなかった。このモジュールはその帳簿の集計・見積もり・
閾値判定だけを担う純関数群で、クラスタやネットワークに触れない。

- 産出側 (各 namespace の CronJob): restic Job ログから推定量を作り、ConfigMap へ
  「runs: [{date: "YYYY-MM-DD", job: 名前, bytes: N}, ...]」(UTC 日付。cap のリセットが
  毎日 00:00 UTC のため UTC が唯一の自然な区切り) の形で書き込む。推定の仕方
  (restic はダウンロードバイト数を表示しないため操作種別ごとのモデルになる) は
  産出側の責務で、ここでは検査しない
- 集約側 (apps/ops-health-reporter/report.py): ConfigMap 群を読んで build_report() に
  渡し、返り値を latest.json / history jsonl の `download_budget` キーへ載せる

report.py と同じく標準ライブラリのみで動く。import 副作用を持たない
(report.py と違い ServiceAccount token を読まないので、cluster 外の unit test から
importlib で直接ロードできる)。
"""

import datetime

# 直近何日分を集計するか。「直近 N 日」の N。週次の消費者 (retention) が
# 少なくとも 1 回は窓に入るよう 7 日。
DEFAULT_WINDOW_DAYS = 7
# 月次見積もりの視野 (日数)。窓の平均をこの日数分へ外挿する。
MONTHLY_HORIZON_DAYS = 30
# 上限の何割で「近傍」とみなすか。上限ちょうどで鳴る計器では手遅れなので。
WARN_RATIO = 0.8

# cap の実値 (download bandwidth / Class C transactions の具体的な数値) は B2 コンソールに
# しか無く repo の docs には無い (P-0111 root_cause.md が docs 化しているのは性質のみ:
# アカウント単位・鍵の種類に無関係・毎日 00:00 UTC リセット)。実値が判明したらここか
# 呼び出し側で設定すること — None の間は判定を「unconfigured」として正直に見せるだけで、
# 適当な値を決め打ちして警報を鳴らしたり沈黙させたりしない。
DEFAULT_DAILY_CAP_BYTES = None


def parse_date(value):
    """YYYY-MM-DD を datetime.date へ。厳格に失敗時 None (例外を出さない)。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def coerce_bytes(value):
    """bytes フィールドの検査。0 以上の int のみ受け付ける。

    bool は int の派生なので明示的に弾く。負値・文字列・None は不正として None。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def sum_window(runs, today, window_days=DEFAULT_WINDOW_DAYS):
    """run 記録のリストを直近 window_days 日 (today 含む) で集計する。

    runs の各要素は {date, job, bytes} (+ 任意の追加フィールド)。壊れた記録は
    例外を出さず skipped に数えて捨てる — レポートの1項目が壊れても残りは
    出したい (report.py collect() と同じ思想)。

    未来日付は clock skew の疑いがあるため窓に入れず skipped に数える
    (heartbeat judge() の skew 扱いと同じ倒し方)。
    """
    if isinstance(today, str):
        today = parse_date(today)
        if today is None:
            raise ValueError("today は YYYY-MM-DD または date")
    oldest = today - datetime.timedelta(days=window_days - 1)
    daily_bytes = {}
    by_job = {}
    total = 0
    covered = set()
    skipped = 0
    for run in runs or []:
        if not isinstance(run, dict):
            skipped += 1
            continue
        run_date = parse_date(run.get("date"))
        bytes_ = coerce_bytes(run.get("bytes"))
        if run_date is None or bytes_ is None:
            skipped += 1
            continue
        if run_date > today or run_date < oldest:
            # 未来日は記録自体が疑わしい。古いものは単に窓の外
            skipped += 1
            continue
        key = run_date.isoformat()
        daily_bytes[key] = daily_bytes.get(key, 0) + bytes_
        job = run.get("job")
        if not isinstance(job, str) or not job:
            job = "unknown"
        by_job[job] = by_job.get(job, 0) + bytes_
        total += bytes_
        covered.add(key)
    return {
        "daily_bytes": dict(sorted(daily_bytes.items())),
        "by_job": dict(sorted(by_job.items())),
        "window_total_bytes": total,
        "days_covered": len(covered),
        "skipped_records": skipped,
    }


def monthly_estimate(window_total_bytes, days_covered, horizon_days=MONTHLY_HORIZON_DAYS):
    """窓の合計から月次見積もり (bytes) を外挿する。データが無ければ None。

    無理な精度は出さない — 単純な比例外挿であり、「このまま 30 日続いたら」の意味。
    """
    if not days_covered or days_covered < 0:
        return None
    return window_total_bytes * horizon_days / days_covered


def _fmt_bytes(value):
    """人間用の粗い表現 (briefing/reason 文面向け)。機械読み取り用のフィールドは生 bytes。"""
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{:.1f} TiB".format(value)


def judge(daily_avg_bytes, daily_cap_bytes=DEFAULT_DAILY_CAP_BYTES,
          warn_ratio=WARN_RATIO, horizon_days=MONTHLY_HORIZON_DAYS):
    """直近の 1 日あたり推定量を設定値に対して判定する。

    cap は毎日 00:00 UTC リセット (実測済み) のため比較軸は「1日あたり」が正。
    月次見積もりは monthly_estimate() が別途出し、reason 文面には換算値として載せる
    (月次見積もり vs 月次換算 cap は daily_avg vs cap と数学的に同値なので判定軸を
    増やさない)。

    status:
      no_data       窓に記録が無い
      unconfigured  cap の実値が未設定 (repo の docs に無い。決め打ちしない)
      ok            設定値の warn_ratio 未満
      warn          warn_ratio 以上・設定値未満 (境界含む、鳴る側に倒す)
      exceed        設定値以上
    """
    ratio_label = "{:.0%}".format(warn_ratio)
    if daily_avg_bytes is None:
        return {"status": "no_data",
                "reason": "直近の集計窓に記録が無く、消費量を見積もれない"}
    if daily_cap_bytes is None:
        return {"status": "unconfigured",
                "reason": ("閾値が未設定 (cap の実値は B2 コンソールにしか無い)。"
                           "判明したら download_budget.py の DEFAULT_DAILY_CAP_BYTES か"
                           "呼び出し側に設定すること")}
    monthly = daily_avg_bytes * horizon_days
    cap_monthly = daily_cap_bytes * horizon_days
    base = (
        "直近平均 {} /日 ({} 日換算 {})、設定値 {} /日".format(
            _fmt_bytes(daily_avg_bytes), horizon_days,
            _fmt_bytes(monthly), _fmt_bytes(daily_cap_bytes))
    )
    if daily_avg_bytes >= daily_cap_bytes:
        return {"status": "exceed", "reason": base + "。設定値超過 — cap 超過日の再来に直結する"}
    if daily_avg_bytes >= daily_cap_bytes * warn_ratio:
        return {"status": "warn", "reason": base + f"。設定値の {ratio_label} 近傍 — 新しいダウンロード消費者の投入は控えること"}
    return {"status": "ok", "reason": base + f"。設定値の {ratio_label} 未満"}


def build_report(namespace_reports, today=None, window_days=DEFAULT_WINDOW_DAYS,
                 daily_cap_bytes=DEFAULT_DAILY_CAP_BYTES):
    """namespace ごとの ConfigMap 中身のリスト → latest.json の `download_budget` キーの中身。

    namespace_reports の各要素は report.py の collect_pvc_usage() が作るのと同型:
    正常 {"namespace": ..., "runs": [...]} / 収集失敗 {"namespace": ..., "error": ...}。
    error エントリは他 namespace の集計を止めない (同じく collect() の思想)。
    """
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()
    namespaces = {}
    total_runs = []
    for entry in namespace_reports or []:
        if not isinstance(entry, dict):
            continue
        ns = entry.get("namespace") or "unknown"
        if "error" in entry:
            namespaces[ns] = {"error": entry["error"]}
            continue
        runs = entry.get("runs")
        summary = sum_window(runs if isinstance(runs, list) else [], today, window_days)
        namespaces[ns] = summary
        for job, bytes_ in summary["by_job"].items():
            total_runs.append((f"{ns}/{job}", bytes_))
    all_daily = {}
    for summary in namespaces.values():
        for day, bytes_ in summary.get("daily_bytes", {}).items():
            all_daily[day] = all_daily.get(day, 0) + bytes_
    window_total = sum(all_daily.values())
    days_covered = len(all_daily)
    daily_avg = window_total / days_covered if days_covered else None
    estimate = monthly_estimate(window_total, days_covered)
    return {
        "window_days": window_days,
        "namespaces": dict(sorted(namespaces.items())),
        "total": {
            "daily_bytes": dict(sorted(all_daily.items())),
            "by_job": dict(sorted(total_runs)),
            "window_total_bytes": window_total,
            "days_covered": days_covered,
            "daily_avg_bytes": daily_avg,
        },
        "monthly_estimate_bytes": estimate,
        "budget": judge(
            daily_avg, daily_cap_bytes=daily_cap_bytes,
            horizon_days=MONTHLY_HORIZON_DAYS,
        ),
    }
