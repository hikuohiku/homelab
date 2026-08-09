#!/usr/bin/env python3
"""ops-state ブランチの heartbeat.json の鮮度を、**器の外側から**検査する。

なぜ在るか (P-0027):
  今の生存確認は全部クラスタの内側にある — heart 自身が heartbeat.json を書き、
  ops-health-reporter CronJob が同じクラスタで読み、ダッシュボードも同じクラスタが
  配信する。heart / node01 / k3s のどれが死んでも、死んだと言う口ごと死ぬ。
  旧・細切れループは「止まったまま死んだ」のに誰も気づかなかった。
  このスクリプトは GitHub Actions (クラスタの外、別の障害ドメイン) から呼ばれ、
  沈黙していたら issue #56 で人間を叩くための判定だけを担う。

設計:
  - 標準ライブラリのみ。ネットワークに出ない (コメント取得は呼び出し側の gh の仕事で、
    ここは JSON を受け取って判定するだけ)。import 時に副作用を持たない。
  - 判定は純粋関数 (judge / should_post / build_body) に、I/O は main() に寄せる。
  - **fail-closed**: ファイルが無い / JSON が壊れている / at が読めない → stale 扱い。
    沈黙を検知する道具が「読めなかった」を「元気」に倒したら存在意義が無い。
  - 閾値 (stale_seconds) は ops/rules.json が単一情報源。cooldown は rules.json が
    CODEOWNERS 保護パス (触ると auto-merge が止まる) なのでモジュール定数にする
    (ops/heart/adoptgate.py と同じ流儀)。

終了コード (.github/workflows/watchdog.yml がこれで分岐する):

  | rc | 意味                       | workflow の振る舞い          |
  |----|----------------------------|------------------------------|
  | 0  | fresh                      | 何もしない (job success)     |
  | 1  | stale、通知すべき          | コメント投稿 → job fail      |
  | 3  | stale だが cooldown 内     | 投稿しない → job fail        |
  | 2  | 引数の誤り (argparse 既定) | job fail                     |

  --comments-file を渡さなければ抑止判定を一切しない (= stale なら常に rc=1)。
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

OPS = pathlib.Path(__file__).parent

# 投稿本文の末尾に置く自己識別マーカー。次回起動の重複抑止はこれを目印に探す。
# ops/post_issue_comment.py の `<!-- autopilot:self-posted -->` を使ってはいけない —
# あれは器のフィードバック取り込み (CHARTER §6) にスキップさせる印で、
# 人間に届けたいこのコメントが器に食われる。
MARKER = "<!-- watchdog:heartbeat-stale -->"
# 訓練 (workflow_dispatch + simulate_at) 用。本番と分けるのは、訓練のコメントが
# 本番の抑止窓を食うと、直後に本当の沈黙が来ても黙るため。
# MARKER は DRILL_MARKER の部分文字列ではない (末尾が " -->" と "-drill -->" で違う)
DRILL_MARKER = "<!-- watchdog:heartbeat-stale-drill -->"

# 同じ検知で再投稿するまでの間隔。stale_seconds=7200 (2h) に対し schedule が 30 分毎なので、
# 抑止が無いと 1 障害で 1 日 48 件投稿される。6h なら最大 4 件/日。
# stale が続く事実は job の fail として Actions 上に残り続けるので、コメントは
# 「気づかせる」だけでよい
REPOST_COOLDOWN_SECONDS = 21600  # 6h

RC_FRESH = 0
RC_STALE_NOTIFY = 1
RC_STALE_SUPPRESSED = 3


def parse_iso(v):
    """ISO8601 (末尾 Z を含む) を aware datetime に。読めなければ None。

    ops/heart/statefiles.py の parse_iso と同じ流儀。heart が書く at は
    now_iso() = "%Y-%m-%dT%H:%M:%SZ"。
    """
    try:
        dt = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def load_stale_seconds(rules_path=None):
    """ops/rules.json の heartbeat.stale_seconds を読む (閾値の単一情報源)。

    import 時ではなく呼ばれたときに読む (このモジュールは exec_module されるので
    import が I/O を持ってはいけない)。
    """
    path = pathlib.Path(rules_path) if rules_path else OPS / "rules.json"
    rules = json.loads(path.read_text())
    value = rules["heartbeat"]["stale_seconds"]
    return int(value)


def read_heartbeat(path):
    """heartbeat.json を読む -> (doc, load_error)。例外は投げない (fail-closed は judge 側)。"""
    p = pathlib.Path(path)
    try:
        raw = p.read_text()
    except OSError as e:
        return None, f"heartbeat.json を読めない ({p}): {e}"
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"heartbeat.json が JSON として壊れている ({p}): {e}"
    if not isinstance(doc, dict):
        return None, f"heartbeat.json のトップレベルが object でない ({p})"
    return doc, None


def judge(doc, now, stale_seconds, load_error=None):
    """鮮度の判定 (純粋関数) -> {"stale", "age_seconds", "at", "reason"}。

    fail-closed: doc が無い / at が無い・読めない → stale=True。
    未来の at (clock skew) は stale ではない扱いにし、reason に残す。
    """
    if load_error or doc is None:
        return {
            "stale": True,
            "age_seconds": None,
            "at": None,
            "reason": (load_error or "heartbeat.json が取得できなかった")
            + " — 読めないことは元気の証拠ではないので stale と判定する (fail-closed)",
        }

    raw_at = doc.get("at")
    if raw_at is None:
        return {
            "stale": True,
            "age_seconds": None,
            "at": None,
            "reason": "heartbeat.json に at が無い — 書き手 (heart の write_heartbeat) が壊れている可能性 (fail-closed)",
        }

    at = parse_iso(raw_at)
    if at is None:
        return {
            "stale": True,
            "age_seconds": None,
            "at": str(raw_at),
            "reason": f"at={raw_at!r} を ISO8601 として解釈できない (fail-closed)",
        }

    age = int((now - at).total_seconds())
    if age < 0:
        return {
            "stale": False,
            "age_seconds": age,
            "at": str(raw_at),
            "reason": (
                f"at が {-age} 秒 未来にある (clock skew)。"
                "沈黙ではないので fresh と判定するが、時刻がずれている"
            ),
        }
    if age >= stale_seconds:
        return {
            "stale": True,
            "age_seconds": age,
            "at": str(raw_at),
            "reason": f"heartbeat が {age} 秒 更新されていない (閾値 {stale_seconds} 秒)",
        }
    return {
        "stale": False,
        "age_seconds": age,
        "at": str(raw_at),
        "reason": f"heartbeat は {age} 秒前に更新されている (閾値 {stale_seconds} 秒)",
    }


def should_post(comments, now, cooldown_seconds, marker, comments_since=None):
    """重複抑止の判定 (純粋関数) -> {"post", "reason", "last_at"}。

    comments は GitHub API の issue comments JSON をそのまま (created_at / body を見る)。
    comments_since は取得に使った lookback (ISO)。それが now - cooldown より新しいと、
    「窓の外に自分のコメントがあったか」が原理的に分からない。その場合は post=True に
    倒す (重複より沈黙の方が悪い)。ただし窓内に自分のコメントが**見つかっている**なら、
    lookback が短くても抑止して構わない (見つかった事実は lookback に依存しない)。
    """
    cutoff = now - datetime.timedelta(seconds=cooldown_seconds)

    last = None
    for c in comments or []:
        if marker not in (c.get("body") or ""):
            continue
        created = parse_iso(c.get("created_at"))
        if created is None:
            continue
        if last is None or created > last:
            last = created

    if last is not None and last > cutoff:
        age = int((now - last).total_seconds())
        return {
            "post": False,
            "reason": f"{age} 秒前に同じ検知のコメントを投稿済み (cooldown {cooldown_seconds} 秒)",
            "last_at": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    since = parse_iso(comments_since) if comments_since else None
    if since is not None and since > cutoff:
        return {
            "post": True,
            "reason": (
                f"コメントの取得範囲 (since={comments_since}) が cooldown "
                f"{cooldown_seconds} 秒より短く、抑止すべきか判断できない。投稿に倒す"
            ),
            "last_at": last.strftime("%Y-%m-%dT%H:%M:%SZ") if last else None,
        }

    if last is None:
        return {"post": True, "reason": "cooldown 内に同じ検知のコメントは無い", "last_at": None}
    return {
        "post": True,
        "reason": f"直近の同じ検知のコメントは cooldown ({cooldown_seconds} 秒) より前",
        "last_at": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_body(judgement, marker, now, stale_seconds, drill=False, run_url=None):
    """issue #56 に投稿する本文 (Markdown)。人間宛。

    **器の triage に食わせないこと**が最大の制約 (ops/heart/triage.py)。
    _matches_stop() は「いずれかの行が停止キーワードで始まる」か
    「全体が 50 文字以下でキーワードを含む」で全停止に倒れ、`veto P-\\d{4}` は veto になる。
    したがって本文は 50 文字を大きく超え、どの行頭にも停止キーワード
    (止めて / 止まって / やめて / 中止 / stop / abort / veto) を置かず、
    `veto P-NNNN` の形を書かない。この不変条件は
    ops/tests/test_check_heartbeat_fresh.py が triage.classify で実際に確かめている。
    """
    age = judgement.get("age_seconds")
    age_line = f"{age} 秒 (約 {age / 3600:.1f} 時間)" if isinstance(age, int) else "不明 (読めなかった)"
    head = (
        "**[訓練] 外部 watchdog の疎通確認です。実際の障害ではありません。**"
        if drill
        else "**外部 watchdog: heart の heartbeat が古くなっています。**"
    )
    lines = [
        head,
        "",
        "GitHub Actions (クラスタの外) から ops-state ブランチの `heartbeat.json` を見たところ、",
        "heart が状態を書き進めた形跡が閾値を超えて途絶えていました。人間に向けたお知らせです。",
        "",
        f"- 判定時刻 (UTC): `{now.strftime('%Y-%m-%dT%H:%M:%SZ')}`",
        f"- `heartbeat.json` の `at`: `{judgement.get('at')}`",
        f"- 経過: {age_line}",
        f"- 閾値: `ops/rules.json` の `heartbeat.stale_seconds` = {stale_seconds} 秒",
        f"- 判定理由: {judgement.get('reason')}",
    ]
    if run_url:
        lines.append(f"- この検知を出した run: {run_url}")
    lines += [
        "",
        "確認してほしいこと:",
        "",
        "1. node01 (Proxmox VM) と k3s が生きているか",
        "2. `autopilot` namespace の heart Deployment の Pod が Running か、ログに例外が出ていないか",
        "3. ops-state ブランチへの push が失敗していないか (トークン失効・リポジトリ側の拒否)",
        "",
        "誤検知だと判断した場合はこのコメントを無視して構いません。次の検知は最短でも "
        f"{REPOST_COOLDOWN_SECONDS // 3600} 時間後まで抑止されます。",
        "検知そのものが不要になったら `.github/workflows/watchdog.yml` の schedule を外してください。",
        "",
        marker,
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="ops-state の heartbeat.json が stale かどうかを器の外側から判定する"
    )
    ap.add_argument("--file", required=True, help="heartbeat.json のパス (無くてよい: fail-closed で stale)")
    ap.add_argument("--rules", default=None, help="rules.json のパス (既定: ops/rules.json)")
    ap.add_argument("--now", default=None, help="現在時刻 (ISO8601)。試験用")
    ap.add_argument("--comments-file", default=None, help="issue comments の JSON。省略すると重複抑止をしない")
    ap.add_argument("--comments-since", default=None, help="comments の取得に使った lookback (ISO8601)")
    ap.add_argument("--body-out", default=None, help="stale のとき投稿本文をこのパスに書く")
    ap.add_argument("--run-url", default=None, help="本文に載せる Actions run の URL")
    ap.add_argument("--drill", action="store_true", help="訓練モード (本番と別のマーカーを使う)")
    ap.add_argument("--json", action="store_true", help="判定結果を JSON でも stdout に出す")
    args = ap.parse_args(argv)

    now = parse_iso(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    if now is None:
        print(f"error: --now={args.now!r} を ISO8601 として解釈できない")
        return 2

    try:
        stale_seconds = load_stale_seconds(args.rules)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        # 閾値が読めないのは watchdog 自身の故障。stale と混ぜず引数エラー側に倒す
        print(f"error: stale_seconds を読めない ({args.rules or OPS / 'rules.json'}): {e}")
        return 2

    doc, load_error = read_heartbeat(args.file)
    judgement = judge(doc, now, stale_seconds, load_error)
    marker = DRILL_MARKER if args.drill else MARKER

    print(f"heartbeat: {'STALE' if judgement['stale'] else 'fresh'} — {judgement['reason']}")

    if not judgement["stale"]:
        if args.json:
            print(json.dumps({"stale": False, "judgement": judgement}, ensure_ascii=False))
        return RC_FRESH

    body = build_body(judgement, marker, now, stale_seconds, drill=args.drill, run_url=args.run_url)
    if args.body_out:
        pathlib.Path(args.body_out).write_text(body)

    decision = {"post": True, "reason": "重複抑止の判定をしていない (--comments-file 未指定)", "last_at": None}
    if args.comments_file:
        try:
            comments = json.loads(pathlib.Path(args.comments_file).read_text())
        except (OSError, json.JSONDecodeError) as e:
            # 抑止の材料が無い = 抑止できない。沈黙より重複を選ぶ
            comments = []
            print(f"warning: comments を読めない ({args.comments_file}): {e} — 抑止せず投稿に倒す")
        if not isinstance(comments, list):
            comments = []
        decision = should_post(
            comments, now, REPOST_COOLDOWN_SECONDS, marker, comments_since=args.comments_since
        )

    print(f"post: {decision['post']} — {decision['reason']}")
    if args.json:
        print(json.dumps({"stale": True, "judgement": judgement, "decision": decision}, ensure_ascii=False))

    return RC_STALE_NOTIFY if decision["post"] else RC_STALE_SUPPRESSED


if __name__ == "__main__":
    sys.exit(main())
