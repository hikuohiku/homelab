"""P-0102 — restic リポジトリの週次健康診断 runner の判定ロジック。

なぜ要るか: 5 本の restic リポジトリ (vaultwarden / immich / coder-postgres /
coder-workspace-homes / syncthing) は毎晩 append-only 鍵で B2 に書かれているが、
`restic check` は一度も走っておらず、破損は復元時に初めて判る。さらに backup CronJob が
黙って失敗し続けても気づく仕組みが無い (journal では各回が手動で mtime を見ていただけ)。
このモジュールは「壊れていないこと・新鮮であること」の判定を単一の情報源として持ち、
クラスタ内 CronJob (apps/restic-check/) から ConfigMap 経由で実行される。

構成 (test_backup_coverage.py の流儀):
  - 判定は純関数 (parse / evaluate / render / evidence)。テストは合成入力で両方向を固定し、
    「今たまたま通っている」と「正しい」を区別できるようにする
  - クラスタ側のシェルループ (次セッションで apps/restic-check/cronjob.yaml の initContainer
    に置く) は restic を実行してリポジトリごとに 1 ファイルの JSON レコードを書くだけ。
    レコードの契約:
      {"repo": "vaultwarden",          # REPOS の名前と一致させること
       "check_rc": 0,                  # restic check --read-data-subset の終了コード
       "snapshots_rc": 0,              # restic snapshots --latest 1 --json の終了コード
       "snapshots_json": "[...]"}      # 同コマンドの標準出力そのもの
  - main() がレコード群を読み evaluate() し、人間可読レポートと EVIDENCE_JSON 行を
    標準出力へ出す (Pod ログが正。新しい保存基盤を作らない — PROJECT.md 方針 5)。
    失敗時のみ Discord webhook へ incident を送り、成功時は黙る

終了コードの意味:
  0 — 全リポジトリで check 成功かつ全 snapshot が新鮮 (24h 以内)
  1 — check 失敗・レコード欠落・未知のリポジトリが混入、のいずれか
  2 — check は全部成功したが、いずれかの最新 snapshot が 24h 超または取得不能
      (夜間 backup が静かに死んでいる状態。これを「黙って失敗」の主要検知経路にする)

鮮度の扱い: snapshots が取れない / 配列が空 の場合も stale 扱いにする。全 5 リポジトリは
既に毎晩動いているので「snapshot が 1 件も無い」は異常であり、沈黙を健全と解釈しない
(P-0047 の教訓「置いただけでは取れていないのと同じ」の監視版)。

coder-workspace-homes は host 単位世代管理だが `--latest 1` は全 host 中の最新 1 件を
返すので、どれか 1 host でも新しければ nightly は生きている、としてよい (PROJECT.md 方針 2)。
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 検査対象。apps/{vaultwarden,immich,coder,syncthing}/restic-backup-cronjob.yaml が
# 書いている b2:$RESTIC_B2_BUCKET:<名前> の末尾と一致させる。新しい backup 対象が増えたら
# ここに足すこと — 足し忘れると、そのリポジトリのレコードは下の evaluate() で
# 「予期しないリポジトリ」として失敗扱いになり、CI ではなく実行時に気づける。
REPOS = (
    "vaultwarden",
    "immich",
    "coder-postgres",
    "coder-workspace-homes",
    "syncthing",
)

# DoD: 最新 snapshot の古さ 24h 超で warn。backup は毎晩走るので 24h 超は
# 「直近 1 回以上の夜間 backup が失敗している」ことを意味する
WARN_AFTER_HOURS = 24.0

# レコード自体が無かったリポジトリの check_rc 代値。127 = 「コマンドが一度も走らなかった」
# の慣習的な意味。シェル側がクラッシュしても、欠落を見逃さないための保険
MISSING_RC = 127

# restic は snapshot の time を RFC3339 (小数 9 桁 = ナノ秒、Z サフィックス) で出す。
# Python の fromisoformat は版によって小数小数桁数の扱いが違うので、正規表現で自前で切る
_RFC3339 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ]"
    r"(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?"
    r"([Zz]|[+-]\d{2}:?\d{2})?$"
)


def _parse_rfc3339(text):
    """RFC3339 文字列を timezone-aware な datetime へ。解釈できない場合は None。

    ナノ秒はマイクロ秒に丸める (7 桁目以降を捨てる。鮮度判定に影響する精度ではない)。
    """
    m = _RFC3339.match(str(text).strip())
    if not m:
        return None
    year, month, day, hh, mm, ss, frac, zone = m.groups()
    micro = int((frac + "000000")[:6]) if frac else 0
    if zone is None or zone in ("Z", "z"):
        tz = timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])))
    try:
        return datetime(
            int(year), int(month), int(day),
            int(hh), int(mm), int(ss), micro, tzinfo=tz,
        )
    except ValueError:
        return None


def parse_latest_snapshot_time(snapshot_json):
    """`restic snapshots --latest 1 --json` の標準出力から最新 snapshot の時刻を返す。

    壊れた入力 (空文字・JSON でない・空配列・time キー欠け) はすべて None に潰す。
    呼び出し側 (evaluate) は None を「snapshot 取得不能 = stale」に寄せる。
    --latest 1 でも将来の restic の挙動変更に備え、複数要素なら最大を取る。
    """
    try:
        snaps = json.loads(snapshot_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(snaps, list):
        return None
    times = []
    for s in snaps:
        if isinstance(s, dict) and isinstance(s.get("time"), str):
            t = _parse_rfc3339(s["time"])
            if t is not None:
                times.append(t)
    return max(times) if times else None


def _one_result(name, rec, now):
    """1 リポジトリ分の生レコードを判定結果 dict へ。rec が None はレコード欠落。"""
    if rec is None:
        return {
            "repo": name, "check_rc": MISSING_RC,
            "snapshot_age_hours": None, "freshness": "unknown", "stale": True,
            "exit_code": MISSING_RC,
        }
    raw_check_rc = rec.get("check_rc")
    check_rc = raw_check_rc if isinstance(raw_check_rc, int) else MISSING_RC
    snapshots_raw = rec.get("snapshots_rc")
    snapshots_rc = snapshots_raw if isinstance(snapshots_raw, int) else MISSING_RC
    snap_t = parse_latest_snapshot_time(rec.get("snapshots_json") or "")
    if snap_t is None:
        age = None
        # snapshots コマンド自体が失敗したのか、成功したが 1 件も無いのかは区別して残す。
        # どちらも stale 扱いだが、調査の入り口が違う (権限/ネットワーク vs backup 全滅)
        freshness = "unknown" if snapshots_rc != 0 else "no-snapshot"
        stale = True
    else:
        raw_age = (now - snap_t).total_seconds() / 3600.0
        # 比較は丸める前の生値で。表示用の丸めを比較に使うと境界付近で最大数十秒の
        # 判定誤差が生まれる
        stale = raw_age > WARN_AFTER_HOURS
        age = round(raw_age, 2)
        freshness = "warn" if stale else "ok"
    return {
        "repo": name, "check_rc": check_rc,
        "snapshot_age_hours": age, "freshness": freshness, "stale": stale,
        "exit_code": check_rc,
    }


def evaluate(records, now=None):
    """純関数。生レコード群 → 集約結果。

    - 打ち切りない: 1 リポジトリの失敗で止めず全 5 件を判定する (PROJECT.md 方針 1)。
      復旧の初手は「どれがどう壊れたか」を 1 回の実行で知ること
    - REPOS に無いリポジトリのレコードが混ざっていたら unexpected として失敗扱い。
      backup 対象が増えて REPOS 表が追従していない漏れを実行時に見つけるための検知線
      (棚卸しの後に増えたものは誰も拾わない — docs/backup.md §T-0065 の再発防止)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    by_repo = {}
    for rec in records:
        if isinstance(rec, dict) and isinstance(rec.get("repo"), str):
            by_repo[rec["repo"]] = rec
    results = []
    for name in REPOS:
        results.append(_one_result(name, by_repo.pop(name, None), now))
    for extra in sorted(by_repo):
        rec = by_repo[extra]
        entry = _one_result(extra, rec, now)
        entry["freshness"] = "unexpected"
        entry["stale"] = True
        entry["exit_code"] = 1
        results.append(entry)
    failed = [r for r in results if r["exit_code"] != 0]
    staled = [r for r in results if r["exit_code"] == 0 and r["stale"]]
    if failed:
        exit_code = 1
    elif staled:
        exit_code = 2
    else:
        exit_code = 0
    return {
        "run_at": now.isoformat(),
        "results": results,
        "exit_code": exit_code,
        "notify": bool(failed or staled),
        "failed_repos": [r["repo"] for r in failed],
        "stale_repos": [r["repo"] for r in staled],
    }


def evidence_records(evaluation):
    """受入 verify #3 用の evidence 形状。

    ops/projects/logs/P-0102/check_evidence.json は「この関数の出力をそのまま
    json.dump したもの」でなければならない:
      d = json.load(open(...)); assert len(d) >= 5 and all(x['exit_code'] == 0 for x in d)
    実鍵での 1 回きりの証明 (#3 セッション) は、一時 Job のログから EVIDENCE_JSON 行を
    取ってこの形状で保存する。
    """
    return [
        {
            "repo": r["repo"],
            "check_rc": r["check_rc"],
            "exit_code": r["exit_code"],
            "freshness": r["freshness"],
            "snapshot_age_hours": r["snapshot_age_hours"],
            "run_at": evaluation["run_at"],
        }
        for r in evaluation["results"]
    ]


def render_report(evaluation):
    """人間可読レポート。Pod ログ (stdout) が正なので、機械可読な EVIDENCE_JSON 行を
    最後に出し、ログからの切り出しを 1 grep で済ませる。"""
    label = {0: "OK", 1: "CHECK-FAILURE", 2: "STALE"}[evaluation["exit_code"]]
    lines = [f"restic-check {evaluation['run_at']} overall={label}({evaluation['exit_code']})"]
    for r in evaluation["results"]:
        age = "-" if r["snapshot_age_hours"] is None else f"{r['snapshot_age_hours']}h"
        mark = "WARN" if r["stale"] else "ok"
        lines.append(
            f"  {r['repo']:<24} check=rc{r['check_rc']} "
            f"freshness={r['freshness']} age={age} [{mark}]"
        )
    lines.append(
        "EVIDENCE_JSON "
        + json.dumps(evidence_records(evaluation), ensure_ascii=False, separators=(",", ":"))
    )
    return "\n".join(lines)


def incident_message(evaluation):
    """Discord への incident 本文。失敗時のみ送る (成功時は黙る — 方針 4)。"""
    label = {1: "check 失敗", 2: "snapshot 鮮度超過"}[evaluation["exit_code"]]
    heads = evaluation["failed_repos"] + evaluation["stale_repos"]
    body = "\n".join(
        f"- {r['repo']}: check=rc{r['check_rc']} freshness={r['freshness']} "
        f"age={r['snapshot_age_hours']}h"
        for r in evaluation["results"]
        if r["stale"] or r["exit_code"] != 0
    )
    return (
        f"[restic-check] {label} ({len(heads)} 件) run_at={evaluation['run_at']}\n"
        f"{body}\n"
        "確認は cluster 内 CronJob restic-check の Job ログへ。"
        "夜間 backup が止まっている可能性 — docs/backup.md"
    )[:1900]


def post_discord(url, text, timeout=10):
    """webhook 一方向 POST。heart/notify.py と同じ payload 形 ({"content": ...})。

    User-Agent を必ず上書きする: python-urllib 既定の UA (`Python-urllib/3.x`) だと
    Discord 前面の Cloudflare に error 1010 でブロックされ HTTP 403 になる
    (2026-08-23 実機で発覚。webhook 自体は正常で、UA を明示すると 204 が返る)。
    403 になっても呼び出し側は握り潰さず stderr へ出して非ゼロ終了する。
    """
    req = urllib.request.Request(
        url,
        data=json.dumps({"content": text[:1900]}).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "restic-check/0.1 (homelab)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def load_records(results_dir):
    """initContainer が書いたレコードファイル (*.json) を読む。壊れたファイルは捨てず、
    check_rc を MISSING_RC に潰した上で残す — 判定を「緑で通す」方向に使わないため。"""
    records = []
    for path in sorted(Path(results_dir).glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (ValueError, OSError):
            rec = {"repo": path.stem, "check_rc": MISSING_RC}
        records.append(rec)
    return records


def main(argv=None, now=None):
    """now は evaluate() への時刻注入 (単体テスト用。None なら実時刻)。"""
    results_dir = os.environ.get("RESTIC_CHECK_RESULTS_DIR", "/work/results")
    evaluation = evaluate(load_records(results_dir), now=now)
    report = render_report(evaluation)
    print(report)
    webhook = os.environ.get("RESTIC_CHECK_WEBHOOK_URL", "")
    if evaluation["notify"]:
        if webhook:
            try:
                post_discord(webhook, incident_message(evaluation))
            except (OSError, ValueError) as exc:
                # 通知失敗でも報告と終了コードは握り潰さない。Discord が死んでいても
                # Job 自体は確実に赤くなる (ops-health-reporter が pod_issues で拾う)
                print(f"discord notification failed: {exc}", file=sys.stderr)
        else:
            print("notify required but RESTIC_CHECK_WEBHOOK_URL unset", file=sys.stderr)
    return evaluation["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
