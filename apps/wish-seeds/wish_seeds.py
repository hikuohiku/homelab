#!/usr/bin/env python3
"""P-0192「人間本人からの要望募集」の部品一式。

構成は 2 つ:

1. **問いかけの送信** (spec DoD (1))。morning brief P-0174 と同一型の送信専用経路
   (`sendMessage` 1 通。受信系 API は一切触らない)。P-0174 は main 未 merge のため
   同じ形をここに最小限写した (PROJECT.md 設計方針の指示)。**1 通きり**の担保は
   証跡ファイルによる — 送信前に ask-evidence.json を見て、存在したら送らない
   (再実行・再走で二重送信しない。verify が直接見ない項目への機械的な歯止め)。

2. **返答 → seed への昇格** (DoD (2)(3))。telegram-adapter が ops-feedback ブランチに
   置く note ({id, source: "telegram", received, body}) を受け取り、seeds.md の新節
   「人間の要望 (2026-08 募集より)」の本文を起こす。返信ゼロでも
   「聞いたこと・返ってこなかったこと」を記録する (沈黙も観測)。

   昇格の前に triage.classify を通す。お願いへの自由文返事に「やめて」「止めて」等が
   入ると heart は全停止/veto として拾う (決定論パススルー)。それは triage の仕様として
   正しい挙動だが、seed 化までしてしまうと被害が二重になる (停止までした返信が
   「要望」としても採用される)。そこで本モジュールは review_needed に分類された本文だけを
   要望行に昇格し、停止/veto 系に落ちたものは昇格を見送って「要確認」として別掲する。

固定テストは ops/tests/test_wish_seeds.py
(`python3 -m unittest ops.tests.test_wish_seeds`)。HTTP 層は注入可能で、
テストはネットワークなしで通る (version_watch と同じ流儀)。

使い方 (リポジトリルートから):

    # 本文確認 (送らない。デフォルトは常に dry-run)
    python3 -m ops.tools.wish_seeds

    # 実送信 (TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID 必須。
    # 証跡ファイルが既にあるときは送らず終了する)
    python3 -m ops.tools.wish_seeds --send
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path

# triage は render_seeds_section() の中で遅延 import する。このモジュールは
# apps/wish-seeds/ の ConfigMap にも同内容のコピーが焼かれ (apps/wish-seeds/
# kustomization.yaml)、repo checkout の無いクラスタ内 Job から run_ask.py 経由で
# import される。module level の ops.* import があるとそこで壊れるため。
# 送信側 (compose_ask / send_telegram / build_evidence) は依存ゼロで動く
REPO = Path(__file__).resolve().parents[2]
RULES_PATH = REPO / "ops" / "rules.json"
EVIDENCE_PATH = REPO / "ops" / "projects" / "logs" / "P-0192" / "ask-evidence.json"

# spec DoD (1) の固定文言。挨拶や装飾を足さない (「1 通のみ」の中身そのもの)
ASK_TEXT = (
    "生活で面倒に感じていることを上位 3 つ教えてください "
    "(homelab で自動化できそうなもの)"
)

# seeds.md 新節の見出し。verify 3 が grep する文字列の由来
SECTION_TITLE = "## 人間の要望 (2026-08 募集より)"


def compose_ask():
    return ASK_TEXT


def load_rules():
    with open(RULES_PATH) as f:
        return json.load(f)


def send_telegram(token, chat_id, text, urlopen=None):
    """sendMessage 1 通。受信系 API は一切使わない (spec「送信専用」)。

    morning brief P-0174 の send_telegram と同一形だが、証跡に必要なため
    Telegram 応答 payload をそのまま返す (message_id は呼び出し側が取る)。
    """
    if urlopen is None:
        urlopen = urllib.request.urlopen
    request = urllib.request.Request(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "wish-seeds-p0192",
        },
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read() or b"{}")
    if not payload.get("ok"):
        raise RuntimeError("Telegram sendMessage が ok を返さなかった: {}".format(payload))
    return payload


def build_evidence(message_id, chat_id, sent_at=None):
    """送信証跡 1 件分の dict。verify 2 が message_id / sent_at を要求する。"""
    if sent_at is None:
        sent_at = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    return {
        "project": "P-0192",
        "ask_text": ASK_TEXT,
        "chat_id": chat_id,
        "message_id": message_id,
        "sent_at": sent_at,
    }


def already_sent(evidence_path=EVIDENCE_PATH):
    """証跡ファイルの存在で「送信済み」を判定する (二重送信の歯止め)。"""
    return Path(evidence_path).exists()


def _wish_lines(body):
    """返答本文を 1 行 1 要望に割る。空行は落とし、長すぎる行は要約しないで切る
    (seed は立案の原料であり、ここで編集しない)。"""
    lines = []
    for raw in str(body).splitlines():
        line = raw.strip()
        if line:
            lines.append(line[:200])
    return lines


def render_seeds_section(notes, rules=None):
    """返答 note の list から seeds.md 新節の本文を起こす。

    notes は telegram-adapter 形 ({id, source, received, body}) を想定するが、
    body だけあれば動く (欠けている欄は素通し)。戻り値は seeds.md にそのまま
    貼れるテキスト (末尾改行付き)。

    - review_needed に分類された本文だけを要望行に昇格する (クラス docstring 参照)
    - 要望 0 件かつ返信 0 件なら沈黙の記録になる
    - 返信はあるが全部昇格見送りの場合も正直にそう書く (沈黙とは言わない)
    """
    from ops.heart import triage

    if rules is None:
        rules = load_rules()
    wishes = []
    flagged = []
    for note in notes:
        body = note.get("body", "")
        verdict = triage.classify(body, rules)
        entry = {
            "id": note.get("id", "(id 不明)"),
            "received": note.get("received", "(時刻不明)"),
            "body": body,
            "kind": verdict["kind"],
        }
        if verdict["kind"] == "review_needed":
            wishes.extend(_wish_lines(body))
        else:
            flagged.append(entry)

    out = [SECTION_TITLE, ""]
    out.append(
        "P-0192 (2026-08 募集) の Telegram 問いかけ「{}」への返信。".format(ASK_TEXT)
    )
    out.append("curriculum の立案原料 (H6「人間が欲しいものを聞くのが最良の原料」の実施)。")
    out.append("")
    if wishes:
        for wish in wishes:
            out.append("- {}".format(wish))
    elif notes:
        out.append(
            "返信はあったが、すべて triage が停止/veto 系に分類したため要望としては昇格しなかった。"
        )
    else:
        out.append(
            "{} に募集を送ったが、締め時点で返信 0 件だった。".format(
                "2026-08-23"
            )
        )
        out.append("聞いたこと・返ってこなかったことの記録。沈黙も観測である。")

    if flagged:
        out.append("")
        out.append("### 昇格を見送った返信 (triage が停止/veto 系に分類)")
        out.append("")
        for entry in flagged:
            out.append(
                "- 「{}」 ({}, note {})".format(
                    entry["body"], entry["kind"], entry["id"]
                )
            )
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--send",
        action="store_true",
        help="実際に送る。無ければ dry-run (本文表示のみ)",
    )
    parser.add_argument(
        "--evidence",
        default=str(EVIDENCE_PATH),
        help="証跡ファイルのパス (既定: %(default)s)",
    )
    args = parser.parse_args(argv)

    evidence_path = Path(args.evidence)
    if already_sent(evidence_path):
        print(
            "証跡 {} が既にある。1 通きりの募集なので送らない".format(evidence_path)
        )
        return 1

    text = compose_ask()
    if not args.send:
        print(text)
        print("(dry-run: 送信していない。--send で実送信)", file=sys.stderr)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not token or not chat_id:
        print(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID 未設定。"
            "autopilot ns の Secret telegram-adapter-credentials 由来で設定すること",
            file=sys.stderr,
        )
        return 1

    payload = send_telegram(token, chat_id, text)
    message_id = (payload.get("result") or {}).get("message_id")
    evidence = build_evidence(message_id=message_id, chat_id=chat_id)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with open(evidence_path, "w") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("送信した: message_id={} -> {}".format(message_id, evidence_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
