#!/usr/bin/env python3
"""ops-health-report の履歴から argocd application-controller のメモリ実系列を集計する (P-0181)。

なぜ要るか: apps/argocd/values.yaml の controller.resources.limits.memory=512Mi は
出典のない推測値で、2026-08-23 に argocd-application-controller-0 の OOMKilled
(exit_code 137, restarts 4) として事故になった (substrate 規則「memory limits は
実測の裏付けなしに付けない」(T-0055) の裏側 — 実測なしに**小さく**付ける破綻)。
一方で origin/ops-health-report ブランチには pod_metrics の履歴が 30 分間隔で
日々積まれているのに、このコンポーネントの実使用量を時系列で読んで limit を
決めた者はいなかった。このモジュールはその読みを計器にする。

データ源: `git show <branch>:ops/health/history/YYYY-MM-DD.jsonl` (CHARTER §2 の
確立経路)。1 行 1 レポートの JSON で、pod_metrics[].containers[] に Kubernetes
quantity 文字列 ("320908Ki" / "239Mi") が載る。--dir でローカルの jsonl 置き場を
直接読める (テスト・オフライン用。CI は shallow clone の可能性があるため、
テストは必ず --dir 経由でネットワーク/リモート参照なしで通す)。

出力:
- 既定では人間可読のサマリを stdout へ
- --json で証跡ドキュメント (下記) を stdout へ。リダイレクトして
  ops/projects/logs/P-0181/memory-series.json として保存する
- --check で再計算がコミット済み証跡 JSON と一致することを確認する (--evidence で
  パス指定可、既定は上記パス)。P-0124 型の冪等検査

--check の意味論: 履歴は append-only で毎 30 分伸びるため、素朴な全期間再計算は
翌日には必ず不一致になる。そこで証跡 JSON に記録した観測窓 (window.first .. last)
**の中だけ**を再計算して比較する。窓内は過去データなので不変で、真の冪等になる。
未来の追記で窓が古くなったら、証跡を --json で作り直してコミットし直す (それも
運用)。窓内の書き換え (force push 等) は不一致として落ちる — 沈黙しない。

集計の定義:
- ピーク = 全サンプルの最大値。ただし metrics-server の値は約 30 分間隔の瞬間値で、
  OOMKill のような急峻な尖りは捉えられない (= 観測ピークは真のピークの**下限**)。
  実際 2026-08-23 には観測 398Mi 未満のまま limit 512Mi で 4 回死んでいる。
  limit 引き直しではこの「下限である」ことを必ず織り込むこと
- p95 = 線形補間 (numpy 流: idx = q*(n-1)、前後要素の加重平均)
- 成長率 = 全サンプルへの最小二乗直線の傾き (Ki/day)。leak_suspect は系列全体が
  単調非減少のとき True (再起動で使用量はリセットされるので、単調増加が貫くなら
  リセットを上回る漏出の疑い)。significant は slope > 0 かつ 30 日外挿が
  中央値の 10% 以上のとき True — 容量策を起こすに足る規模の目安で、
  雑音の日々変動 (数十 Mi) を弾くために中央値比で決めている

既知の死角 (伏せずに書き残す):
  - 上記の通り観測ピークは下限。limit ≈ 観測ピーク × マージンだけで決めると
    「OOMKill が起きた」事実 (真のピーク ≥ 旧 limit) を無視する
  - 単調非減少判定は再起動によるリセットに弱い。ゆっくりした鋸歯状 leak は
    slope 側でしか拾えない
  - pod 名を StatefulSet の固定名 argocd-application-controller-0 で決め打ち。
    StatefulSet 名を変えるとここも変える

判定ロジックの固定テストは ops/tests/test_argocd_memory_series.py
(`python3 -m unittest ops.tests.test_argocd_memory_series`)。
標準ライブラリのみ (repo 慣習)。
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import subprocess
import sys
from pathlib import Path

POD_NAME = "argocd-application-controller-0"
CONTAINER_NAME = "application-controller"
DEFAULT_BRANCH = "origin/ops-health-report"
HISTORY_DIR = "ops/health/history"
DEFAULT_EVIDENCE = "ops/projects/logs/P-0181/memory-series.json"

# significant の目安: 30 日外挿が中央値の何割以上なら容量策・原因追究の種にするか。
# 日々の変動は数十 Mi あるので、それより小さい傾きを「有意」としてはいけない
SIGNIFICANT_30D_MEDIAN_RATIO = 0.10

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_QUANTITY_RE = re.compile(r"^\d+(Ki|Mi|Gi)?$")
_SUFFIX_BYTES = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}


def parse_quantity_bytes(q):
    """Kubernetes quantity 文字列をバイト数 int へ。"320908Ki" -> 328609792。

    対応は無印 (バイト) / Ki / Mi / Gi の整数のみ。metrics-server の usage は
    この形で載る。小数・m 接尾・未知の揺れは ValueError (黙って読み飛ばさず、
    呼び出し側に数えさせる)
    """
    if not isinstance(q, str):
        raise TypeError("quantity は文字列: {!r}".format(q))
    s = q.strip()
    if not _QUANTITY_RE.fullmatch(s):
        raise ValueError("未対応の quantity: {!r}".format(q))
    m = re.fullmatch(r"(\d+)(Ki|Mi|Gi)?", s)
    return int(m.group(1)) * _SUFFIX_BYTES[m.group(2) or ""]


def parse_ts(ts):
    """レポートの時刻刻印 "2026-08-23T09:30:05Z" を aware UTC datetime へ。"""
    return datetime.datetime.strptime(ts, TS_FORMAT).replace(
        tzinfo=datetime.timezone.utc
    )


def extract_series(reports):
    """レポート dict の iterable から対象コンテナの (時刻, バイト) 系列を抜き出す。

    戻り値は (series, skipped)。series は時刻昇順。skipped は「対象 pod/コンテナは
    見つかったのに時刻や quantity が壊れていて数えられなかった行」の件数で、
    欠損を黙らせないためのカウンタ。対象 pod 自体がその回に居ないのは
    metrics-server の普通の欠落なので数えない (sample_count の減少で見える)。
    """
    series = []
    skipped = 0
    for rep in reports:
        if not isinstance(rep, dict):
            skipped += 1
            continue
        pm = rep.get("pod_metrics")
        if not isinstance(pm, list):
            # collect() が例外を {"error": ...} に畳んだ回など。系列としては欠損
            continue
        for pod in pm:
            if not isinstance(pod, dict) or pod.get("name") != POD_NAME:
                continue
            for c in pod.get("containers") or []:
                if not isinstance(c, dict) or c.get("name") != CONTAINER_NAME:
                    continue
                mem, ts_raw = c.get("memory"), rep.get("generated_at")
                try:
                    point = (parse_ts(ts_raw), parse_quantity_bytes(mem))
                except (ValueError, TypeError):
                    skipped += 1
                    continue
                series.append(point)
    series.sort(key=lambda p: p[0])
    return series, skipped


def percentile(sorted_values, fraction):
    """ソート済み数列の分位点 (線形補間)。[10,20] の 95% 点は 19.5。"""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("空の系列の分位点は定義できない")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction は [0,1]: {!r}".format(fraction))
    idx = fraction * (n - 1)
    lo = int(math.floor(idx))
    hi = min(lo + 1, n - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (idx - lo)


def summarize(series):
    """純粋な集計。peak/p95/min/median/日次ピーク/sample_count。

    peak_timestamp を伴うのは「どの日のどのピークか」を証跡に書くため
    (values.yaml の根拠コメントと memory-evidence.md の要求)。
    """
    if not series:
        raise ValueError("空の系列は集計できない")
    values = sorted(b for _, b in series)
    peak_ts, peak_bytes = max(series, key=lambda p: p[1])
    daily = {}
    for ts, b in series:
        day = ts.strftime("%Y-%m-%d")
        if b > daily.get(day, -1):
            daily[day] = b
    return {
        "sample_count": len(series),
        "peak_bytes": peak_bytes,
        "peak_timestamp": peak_ts.strftime(TS_FORMAT),
        "min_bytes": min(values),
        "median_bytes": int(round(percentile(values, 0.5))),
        "p95_bytes": int(round(percentile(values, 0.95))),
        "daily_peak_bytes": daily,
    }


def analyze_growth(series):
    """成長率 (最小二乗傾き Ki/day) と leak 疑い・有意性の判定。純関数。

    significant の定義は docstring 参照 (30 日外挿 >= 中央値の 10%)。
    サンプルが 1 点以下なら傾きは定義できず None (沈黙せず None と明示)。
    """
    out = {
        "slope_ki_per_day": None,
        "leak_suspect": False,
        "significant": False,
        "projected_30d_growth_ki": None,
        "rule": (
            "leak_suspect: 系列全体が単調非減少 / "
            "significant: slope>0 かつ 30日外挿 >= 中央値 x {:.2f}".format(
                SIGNIFICANT_30D_MEDIAN_RATIO
            )
        ),
    }
    if len(series) < 2:
        return out
    t0 = series[0][0]
    xs = [(ts - t0).total_seconds() / 86400.0 for ts, _ in series]
    ys = [b / 1024.0 for _, b in series]  # Ki
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = 0.0 if denom == 0 else sum(
        (x - mx) * (y - my) for x, y in zip(xs, ys)
    ) / denom
    median_ki = percentile(sorted(ys), 0.5)
    projected_30d = slope * 30.0
    out["slope_ki_per_day"] = round(slope, 3)
    out["projected_30d_growth_ki"] = round(projected_30d, 3)
    out["leak_suspect"] = all(b2 >= b1 for (_, b1), (_, b2) in zip(series, series[1:]))
    out["significant"] = bool(
        slope > 0 and projected_30d >= SIGNIFICANT_30D_MEDIAN_RATIO * median_ki
    )
    return out


def build_document(series, source, skipped):
    """証跡ドキュメント (--json の中身、--check の比較対象) を組む。

    notes は人間が読むためのもので --check の比較対象から外れている
    (check_document が見るのは window / sample_count / stats のみ)。
    """
    stats = summarize(series)
    growth = analyze_growth(series)
    first, last = series[0][0], series[-1][0]
    window = {
        "first": first.strftime(TS_FORMAT),
        "last": last.strftime(TS_FORMAT),
    }
    notes = [
        "観測ピーク ({:.1f}Mi @ {}) は 30 分間隔サンプリングの下限。".format(
            stats["peak_bytes"] / 1048576.0, stats["peak_timestamp"]
        ),
        "OOMKill の実績がある場合、真のピークは旧 limit 以上だったことが確定するので",
        "limit 引き直しは「観測ピーク」と「OOMKill 実績値」の両方を根拠にすること。",
    ]
    return {
        "tool": "ops/tools/argocd_memory_series.py",
        "project": "P-0181",
        "source": source,
        "pod": POD_NAME,
        "container": CONTAINER_NAME,
        "window": window,
        "sample_count": stats["sample_count"],
        "skipped_lines": skipped,
        "stats": stats,
        "growth": growth,
        "notes": notes,
    }


def iter_reports_git(branch):
    """git show で <branch> の history/*.jsonl を列挙して 1 レポート dict ずつ返す。

    ファイル一覧は git ls-tree で取る (作業ツリーには history は無い —
    履歴は health ブランチにしか積まれない)。
    """
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", branch, "--", HISTORY_DIR],
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0:
        raise RuntimeError(
            "git ls-tree {} が失敗 (shallow clone なら先例どおり "
            "'git fetch origin +refs/heads/*:refs/remotes/origin/*' が必要): {}".format(
                branch, listing.stderr.strip()
            )
        )
    paths = [
        line.strip()
        for line in listing.stdout.splitlines()
        if line.strip().endswith(".jsonl")
    ]
    if not paths:
        raise RuntimeError("{} に history/*.jsonl が無い".format(branch))
    for path in sorted(paths):
        shown = subprocess.run(
            ["git", "show", "{}:{}".format(branch, path)],
            capture_output=True,
            text=True,
        )
        if shown.returncode != 0:
            raise RuntimeError("git show {}:{} が失敗: {}".format(branch, path, shown.stderr.strip()))
        yield from _parse_lines(shown.stdout)


def iter_reports_dir(directory):
    """ローカル dir の *.jsonl を読む (テスト・オフライン用)。"""
    d = Path(directory)
    paths = sorted(d.glob("*.jsonl"))
    if not paths:
        raise RuntimeError("{} に *.jsonl が無い".format(d))
    for path in paths:
        yield from _parse_lines(path.read_text(encoding="utf-8"))


def _parse_lines(text):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # 壊れた行は extract_series 側で skipped として数えられる形にしたいが、
            # ここで dict 以外 (None) を流せば同じ経路で数えられる
            yield None


def check_document(recomputed, evidence):
    """再計結果と証跡 JSON の突合。不一致理由のリスト (空 = 一致) を返す。

    比較対象は pod/container/window/sample_count/stats/growth。notes や
    source は表記ゆれ・入力経路違い (--dir vs git) を吸うため比較しない。
    float (slope 等) は丸め済みの同一次計算なので通常厳密一致するが、
    isclose で緩める (プラットフォーム差で落ちる冪等検査は検査ではない)。
    """
    diffs = []
    for key in ("pod", "container", "window", "sample_count"):
        if recomputed.get(key) != evidence.get(key):
            diffs.append(
                "{}: 証跡={!r} 再計算={!r}".format(key, evidence.get(key), recomputed.get(key))
            )
    for section in ("stats", "growth"):
        diffs.extend(
            _diff_section(
                "{}.".format(section),
                evidence.get(section) or {},
                recomputed.get(section) or {},
            )
        )
    return diffs


def _diff_section(prefix, expected, actual):
    diffs = []
    for key in sorted(set(expected) | set(actual)):
        e, a = expected.get(key), actual.get(key)
        if isinstance(e, dict) or isinstance(a, dict):
            sub_e = e if isinstance(e, dict) else {}
            sub_a = a if isinstance(a, dict) else {}
            diffs.extend(_diff_section("{}{}.".format(prefix, key), sub_e, sub_a))
        elif e != a:
            if isinstance(e, float) or isinstance(a, float):
                if isinstance(e, (int, float)) and isinstance(a, (int, float)) and math.isclose(
                    e, a, rel_tol=1e-9, abs_tol=1e-9
                ):
                    continue
            diffs.append("{}{}: 証跡={!r} 再計算={!r}".format(prefix, key, e, a))
    return diffs


def _fmt_mib(b):
    return "{:.1f}Mi".format(b / 1048576.0)


def human_summary(doc):
    g = doc["growth"]
    lines = [
        "argocd {container} メモリ系列 ({n} サンプル, {first} .. {last})".format(
            container=doc["container"],
            n=doc["sample_count"],
            first=doc["window"]["first"],
            last=doc["window"]["last"],
        ),
        "  ピーク: {} ({})".format(
            _fmt_mib(doc["stats"]["peak_bytes"]), doc["stats"]["peak_timestamp"]
        ),
        "  p95: {}, 中央値: {}, 最小: {}".format(
            _fmt_mib(doc["stats"]["p95_bytes"]),
            _fmt_mib(doc["stats"]["median_bytes"]),
            _fmt_mib(doc["stats"]["min_bytes"]),
        ),
    ]
    if g["slope_ki_per_day"] is None:
        lines.append("  成長率: サンプル不足で不定")
    else:
        lines.append(
            "  成長率: {:+.1f}Ki/day (30 日外挿 {:+.1f}Ki) / leak_suspect={} / significant={}".format(
                g["slope_ki_per_day"],
                g["projected_30d_growth_ki"],
                g["leak_suspect"],
                g["significant"],
            )
        )
        if g["leak_suspect"]:
            lines.append("  ** 系列が単調増加: leak 疑い。恒久策 (processors チューニング等) の立案を推奨")
    days = doc["stats"]["daily_peak_bytes"]
    lines.append("  日次ピーク: " + ", ".join(
        "{}={}".format(d, _fmt_mib(b)) for d, b in sorted(days.items())
    ))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="証跡ドキュメント JSON を stdout へ")
    mode.add_argument("--check", action="store_true", help="コミット済み証跡 JSON との冪等検査")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="history を読む git ref")
    parser.add_argument("--dir", help="git でなくローカル dir の *.jsonl を読む (テスト用)")
    parser.add_argument("--evidence", default=DEFAULT_EVIDENCE, help="--check の比較先 JSON")
    args = parser.parse_args(argv)

    try:
        reports = iter_reports_dir(args.dir) if args.dir else iter_reports_git(args.branch)
        series, skipped = extract_series(reports)
        if not series:
            raise RuntimeError(
                "{} / {} の系列が 1 件も取れなかった (pod 名変更の可能性)".format(
                    args.branch or args.dir, HISTORY_DIR
                )
            )
        source = (
            {"kind": "dir", "path": str(args.dir)}
            if args.dir
            else {"kind": "git", "ref": args.branch, "path": HISTORY_DIR}
        )
        doc = build_document(series, source, skipped)

        if args.json:
            print(json.dumps(doc, ensure_ascii=False, indent=2))
            return 0
        if args.check:
            ev_path = Path(args.evidence)
            if not ev_path.exists():
                print("証跡 JSON が無い: {}".format(ev_path), file=sys.stderr)
                return 2
            evidence = json.loads(ev_path.read_text(encoding="utf-8"))
            # 窓の中だけで再計算する (docstring の「--check の意味論」)
            w_first = parse_ts(evidence["window"]["first"])
            w_last = parse_ts(evidence["window"]["last"])
            win = [(ts, b) for ts, b in series if w_first <= ts <= w_last]
            if not win:
                print("証跡の窓内にサンプルが無い (履歴が書き換わった?)", file=sys.stderr)
                return 2
            re_doc = build_document(win, source, skipped)
            diffs = check_document(re_doc, evidence)
            if diffs:
                print("--check 不一致 ({} 件):".format(len(diffs)))
                for d in diffs:
                    print("  - {}".format(d))
                return 1
            print(
                "--check OK: {} (窓 {} .. {}, {} サンプル)".format(
                    ev_path, evidence["window"]["first"], evidence["window"]["last"], len(win)
                )
            )
            return 0

        print(human_summary(doc))
        return 0
    except (RuntimeError, OSError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
