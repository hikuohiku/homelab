#!/usr/bin/env python3
"""Telegram 受信文の停止/再開キーワード判定ドリル (P-0118 / dry-run)。

heart 本体に一切触れずに「telegram 由来のメッセージがどのように分類されるか」を
実測するための道具。判定は ops/heart/triage.py の純関数 classify() の呼び出しのみで、
heart への書き込み・全停止 (stop_engaged) の実地発火などの副作用はゼロ。
キーワードの単一情報源は ops/rules.json (veto.stop_keywords / resume_keywords)。

使い方 (リポジトリルートから):
  python3 ops/drills/telegram_veto_drill.py --check          # 同梱ケースで自己検証
  python3 ops/drills/telegram_veto_drill.py --input msg.txt  # 1 行 1 メッセージを分類

--check は受入コマンドが引数無しで通る形 (同梱ケースとの突合)。期待分類からの逸脱が
あれば rc=1。--input は各行を triage.classify() に通して JSONL ({body, kind}) で出力する。
人間が Telegram に実際に送る前に「この文言でどう判定されるか」を確かめるのが主用途。
例: 「止めて」と送る前に --input に通せば、それが stop_all になることが dry-run で確認できる。

テキスト単体では分からないこと (ドリルの限界):
  - kind: task-request への分流は note JSON のトップレベル kind を読む
    collect_feedback() 側の契約であり、本文テキストからは決まらない。そのため
    「実装依頼らしき文」はテキストレベルでは review_needed に出るのが正しい
    (ops/tests/test_telegram_veto.py が note レベルの分流を固定している)
  - 長文 (50 文字超) 中の叙述的な出現は意図的に拾わない (_matches_stop の設計、指摘 [26])。
    「〜で止めてしまう」と書いても stop_all にならないのは仕様
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ops.heart import triage  # noqa: E402

RULES_PATH = REPO / "ops" / "rules.json"


def load_rules():
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _first(rules, key, ascii_only=False):
    kws = rules["veto"][key]
    for kw in kws:
        if not ascii_only or kw.isascii():
            return kw
    raise SystemExit(f"rules.json の veto.{key} に条件を満たすキーワードが無い")


def default_cases(rules):
    """同梱ケース (本文, 期待 kind)。キーワードは rules.json から読み、
    ハードコードしない (登録の変更に追従する)。"""
    stop = rules["veto"]["stop_keywords"][0]
    stop_ascii = _first(rules, "stop_keywords", ascii_only=True)
    resume = rules["veto"].get("resume_keywords", [])[0]
    narrative = (
        "この間の障害のあと自動で" + stop + "しまう問題がないか見直している"
        "うちに、経緯を長めに記録しておいたほうがよさそうだと感じて書き残しています"
    )
    return [
        (stop, "stop_all"),                                # 単独の命令形
        ("お疲れさま。\n一旦" + stop + "ください", "stop_all"),  # 複数行・短文
        (stop_ascii + " everything", "stop_all"),          # ASCII キーワード
        (resume, "resume_all"),                            # 再開
        (resume + "してください", "resume_all"),
        (narrative, "review_needed"),                      # 長文中の叙述は拾わない
        ("今日はいい天気ですね", "review_needed"),           # 雑談は noise へ
    ]


def run_check(rules, out):
    cases = default_cases(rules)
    failures = 0
    for body, expected in cases:
        got = triage.classify(body, rules)["kind"]
        ok = got == expected
        failures += 0 if ok else 1
        label = repr(body if len(body) <= 40 else body[:37] + "...")
        print(f"[{'ok' if ok else 'NG'}] expect={expected:<13} got={got:<13} body={label}", file=out)
    total = len(cases)
    print(f"{total - failures}/{total} cases ok (rules: {RULES_PATH})", file=out)
    return failures == 0


def run_input(path, rules, out):
    lines = sys.stdin.read().splitlines() if path == "-" else Path(path).read_text(
        encoding="utf-8"
    ).splitlines()
    n = 0
    for lineno, line in enumerate(lines, 1):
        body = line.rstrip("\r")
        if not body.strip():
            continue  # 空行は無視 (1 行 1 メッセージの慣習)
        kind = triage.classify(body, rules)["kind"]
        print(json.dumps({"line": lineno, "body": body, "kind": kind}, ensure_ascii=False), file=out)
        n += 1
    print(f"classified {n} message(s)", file=sys.stderr)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="同梱ケースとの突合で自己検証する")
    group.add_argument("--input", metavar="FILE", help="本文 1 行 1 メッセージのファイルを分類 ('-' で標準入力)")
    args = parser.parse_args()

    rules = load_rules()
    if args.check:
        return 0 if run_check(rules, sys.stdout) else 1
    if not Path(args.input).exists() and args.input != "-":
        print(f"error: 入力ファイルが無い: {args.input}", file=sys.stderr)
        return 2
    run_input(args.input, rules, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
