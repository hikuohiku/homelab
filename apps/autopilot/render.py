#!/usr/bin/env python3
"""claude -p --output-format stream-json の出力を 1 行ログに畳む。

-p は既定だと完了までなにも出さないので、最大 1 時間 Pod のログが無音になる。
何をしているのかを外から追えるようにするためだけの部品で、判断は一切しない。
壊れても本体を巻き込まないよう、解釈できない行はそのまま捨てる。
"""
import json
import sys


def emit(text):
    print(f"[claude] {text}", flush=True)


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    kind = ev.get("type")
    if kind == "assistant":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                emit(f"{name} {str(hint)[:160]}")
            elif block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    emit(text[:400])
    elif kind == "result":
        emit(
            "result subtype={} turns={} cost_usd={}".format(
                ev.get("subtype"), ev.get("num_turns"), ev.get("total_cost_usd")
            )
        )
