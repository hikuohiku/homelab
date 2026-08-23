#!/usr/bin/env python3
"""ops-health-report ブランチの latest.json の鮮度を、**器の外側から**検査する。

なぜ在るか (P-0211):
  heart の沈黙は外部 watchdog (P-0027) の heartbeat 検査が見張るが、
  ops-health-reporter 自身が死んでも誰も鳴らさない。reporter が止まると
  ops-health-report ブランチの更新が途絶え、autopilot と人間が共有する健全性情報が
  古いまま凍りつつダッシュボードは平然と見えるため、「既知事象だから」と誤読される
  土壌になる (substrate 2026-08-23 訂正の遠因)。このスクリプトは heartbeat 検査と同じ
  GitHub Actions (クラスタの外、別の障害ドメイン) から呼ばれ、reporter の産出物が
  古くなったら issue #56 で人間を叩くための判定だけを担う。

設計:
  - 標準ライブラリのみ。ネットワークに出ない (ブランチからの取得は呼び出し側
    workflow の git の仕事。ここは JSON を受け取って判定するだけ)。
    import 時に副作用を持たない。
  - 判定は純粋関数 (read_latest / judge / build_body) に、I/O は main() に寄せる。
    雛形は ops/check_heartbeat_fresh.py + 同テスト (P-0027) と同じ流儀。
  - **fail-closed**: ファイルが無い / JSON が壊れている / generated_at が無い・
    読めない → stale 扱い。沈黙を検知する道具が「読めなかった」を「元気」に倒したら
    存在意義が無い。
  - 閾値は rules.json ではなくモジュール定数 (health reporter 用の鍵は rules.json に
    無く、追加は CODEOWNERS 保護パス触りの別論点)。書き手の CronJob は 30 分毎
    (apps/ops-health-reporter/cronjob.yaml) なので、一時的な失敗数回では鳴らないよう
    余裕を持った値にして誤検知を避ける。

終了コード (.github/workflows/watchdog.yml がこれで分岐する):

  | rc | 意味                       | workflow の振る舞い                            |
  |----|----------------------------|------------------------------------------------|
  | 0  | fresh                      | 何もしない (job success)                       |
  | 3  | stale (fail-closed 含む)   | issue #56 コメント + Discord incident → fail  |
  | 2  | 引数の誤り (argparse 既定) | job fail                                       |
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

# 投稿本文の末尾に置く自己識別マーカー。重複抑止には使わない (本スクリプトは
# cooldown を持たない) が、人間とスクリプトがこの検知を識別できるように残す。
# 器のフィードバック取り込み用の `<!-- autopilot:self-posted -->` を使ってはいけない —
# あれを使うと器がこのコメントを読み飛ばし、人間に届けたい警告が消える。
MARKER = "<!-- watchdog:health-report-stale -->"

# 閾値の既定値 (時間)。書き手は 30 分毎なので 6h は 12 サイクル分の余裕。
# 一時的な API 失敗や node01 の再起動程度では鳴らず、半日更新が途れたら確実に鳴る。
DEFAULT_MAX_AGE_HOURS = 6.0

RC_FRESH = 0
RC_STALE = 3


def parse_iso(v):
    """ISO8601 (末尾 Z を含む) を aware datetime に。読めなければ None。

    ops/check_heartbeat_fresh.py と同じ流儀。reporter が書く generated_at は
    report.py の now_iso 相当の "%Y-%m-%dT%H:%M:%SZ"。
    """
    try:
        dt = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def read_latest(path):
    """latest.json を読む -> (doc, load_error)。例外は投げない (fail-closed は judge 側)。"""
    p = pathlib.Path(path)
    try:
        raw = p.read_text()
    except OSError as e:
        return None, f"latest.json を読めない ({p}): {e}"
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"latest.json が JSON として壊れている ({p}): {e}"
    if not isinstance(doc, dict):
        return None, f"latest.json のトップレベルが object でない ({p})"
    return doc, None


def judge(doc, now, max_age_seconds, load_error=None):
    """鮮度の判定 (純粋関数) -> {"stale", "age_seconds", "generated_at", "reason"}。

    fail-closed: doc が無い / generated_at が無い・読めない → stale=True。
    未来の generated_at (clock skew) は stale 扱いにしないが、reason に残す。
    """
    if load_error or doc is None:
        return {
            "stale": True,
            "age_seconds": None,
            "generated_at": None,
            "reason": (load_error or "latest.json が取得できなかった")
            + " — 読めないことは元気の証拠ではないので stale と判定する (fail-closed)",
        }

    raw_at = doc.get("generated_at")
    if raw_at is None:
        return {
            "stale": True,
            "age_seconds": None,
            "generated_at": None,
            "reason": "latest.json に generated_at が無い — 書き手 (ops-health-reporter の report.py) が壊れている可能性 (fail-closed)",
        }

    at = parse_iso(raw_at)
    if at is None:
        return {
            "stale": True,
            "age_seconds": None,
            "generated_at": str(raw_at),
            "reason": f"generated_at={raw_at!r} を ISO8601 として解釈できない (fail-closed)",
        }

    age = int((now - at).total_seconds())
    if age < 0:
        return {
            "stale": False,
            "age_seconds": age,
            "generated_at": str(raw_at),
            "reason": (
                f"generated_at が {-age} 秒 未来にある (clock skew)。"
                "沈黙ではないので fresh と判定するが、時刻がずれている"
            ),
        }
    if age >= max_age_seconds:
        return {
            "stale": True,
            "age_seconds": age,
            "generated_at": str(raw_at),
            "reason": f"health report が {age} 秒 更新されていない (閾値 {max_age_seconds} 秒)",
        }
    return {
        "stale": False,
        "age_seconds": age,
        "generated_at": str(raw_at),
        "reason": f"health report は {age} 秒前に生成されている (閾値 {max_age_seconds} 秒)",
    }


def build_body(judgement, marker, now, max_age_hours, run_url=None):
    """issue #56 に投稿する本文 (Markdown)。人間宛。

    **器の triage に食わせないこと**が最大の制約 (ops/heart/triage.py)。
    投稿された本文は次のビートで triage.classify に読まれるので、本文は 50 文字を
    大きく超え、どの行頭にも停止・再開キーワード (止めて / 止まって / やめて /
    中止 / stop / abort / veto / 再開 / resume) を置かず、`veto P-\\d{4}` /
    `ack P-\\d{4}` の形を書かない。この不変条件は ops/tests/test_health_freshness.py
    が triage.classify で実際に確かめている。
    """
    age = judgement.get("age_seconds")
    age_line = (
        f"{age} 秒 (約 {age / 3600:.1f} 時間)" if isinstance(age, int) else "不明 (読めなかった)"
    )
    lines = [
        "**外部 watchdog: ops-health-report ブランチの更新が古くなっています。**",
        "",
        "GitHub Actions (クラスタの外) から `ops/health/latest.json` を見たところ、",
        "ops-health-reporter が健全性情報を書き進めた形跡が閾値を超えて途絶えていました。",
        "reporter の死亡は autopilot と人間が共有する健全性情報を静かに凍らせるので、",
        "heartbeat 検査とは別口でお知らせします。",
        "",
        f"- 判定時刻 (UTC): `{now.strftime('%Y-%m-%dT%H:%M:%SZ')}`",
        f"- `latest.json` の `generated_at`: `{judgement.get('generated_at')}`",
        f"- 経過: {age_line}",
        f"- 閾値: {max_age_hours} 時間 (`ops/check_health_freshness.py --max-age-hours`)",
        f"- 判定理由: {judgement.get('reason')}",
    ]
    if run_url:
        lines.append(f"- この検知を出した run: {run_url}")
    lines += [
        "",
        "確認してほしいこと:",
        "",
        "1. `autopilot` namespace の ops-health-reporter CronJob が成功しているか、ログに例外が出ていないか",
        "2. ops-health-report ブランチへの push が失敗していないか (トークン失効・リポジトリ側の拒否)",
        "3. クラスタごと死んでいる場合は同じ watchdog の heartbeat 検査が鳴らすはず。両方静かなら watchdog 側も疑う",
        "",
        "誤検知だと判断した場合はこのコメントを無視して構いません。閾値の調整は "
        "`ops/check_health_freshness.py` の DEFAULT_MAX_AGE_HOURS で行えます。",
        "",
        marker,
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="ops-health-report ブランチの latest.json が stale かどうかを器の外側から判定する"
    )
    ap.add_argument("latest_json", help="latest.json のパス (無くてよい: fail-closed で stale)")
    ap.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"この時間 (時) より古ければ stale (既定: {DEFAULT_MAX_AGE_HOURS})",
    )
    ap.add_argument("--now", default=None, help="現在時刻 (ISO8601)。試験用")
    ap.add_argument("--body-out", default=None, help="stale のとき投稿本文をこのパスに書く")
    ap.add_argument("--run-url", default=None, help="本文に載せる Actions run の URL")
    ap.add_argument("--json", action="store_true", help="判定結果を JSON でも stdout に出す")
    args = ap.parse_args(argv)

    if args.max_age_hours < 0:
        print(f"error: --max-age-hours に負の値 ({args.max_age_hours}) は指定できない")
        return 2

    now = parse_iso(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    if now is None:
        print(f"error: --now={args.now!r} を ISO8601 として解釈できない")
        return 2

    doc, load_error = read_latest(args.latest_json)
    judgement = judge(doc, now, args.max_age_hours * 3600, load_error)

    print(f"health report: {'STALE' if judgement['stale'] else 'fresh'} — {judgement['reason']}")

    if not judgement["stale"]:
        if args.json:
            print(json.dumps({"stale": False, "judgement": judgement}, ensure_ascii=False))
        return RC_FRESH

    body = build_body(judgement, MARKER, now, args.max_age_hours, run_url=args.run_url)
    if args.body_out:
        pathlib.Path(args.body_out).write_text(body)

    if args.json:
        print(json.dumps({"stale": True, "judgement": judgement}, ensure_ascii=False))

    return RC_STALE


if __name__ == "__main__":
    sys.exit(main())
