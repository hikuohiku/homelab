#!/usr/bin/env python3
"""ops/rules.json の argocd_controller 節と、その reporter アプリ dir への同期コピー
apps/ops-health-reporter/argocd-alerts.json が一致しているか検証する。CI (ops job) から実行。

P-0181 の近接警報では、閾値 N (application-controller のメモリ実使用が limit の何% 以上で
warn 判定するか) の正を ops/rules.json (人間レビュー必須パス) に置いている。reporter は
in-cluster で repo の rules.json を直接読めず、configMapGenerator も kustomization.yaml 外の
ファイルを読めない (ops/tools/version_watch.py の二重管理先例) ため、値はアプリ dir への
手動同期コピー経由で /scripts に載る。この構造では「片側だけ変える」drift が静かに通り、
reporter は古い閾値を掴んだまま沈黙する。ここで機械的に落とす
(check_download_ledger_script_sync.py と同じ考え方。標準ライブラリのみで動くこと)。

加えて kustomization.yaml の configMapGenerator files に同期コピーが列挙されていることも
見る。外れていると in-cluster の /scripts に載らず、reporter は毎回例外を出すか最悪の場合
古い設定を使い続けるため。
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RULES_REL = "ops/rules.json"
COPY_REL = "apps/ops-health-reporter/argocd-alerts.json"
KUSTOMIZATION_REL = "apps/ops-health-reporter/kustomization.yaml"

SECTION = "argocd_controller"
KEY = "memory_limit_warn_percent"
# 同期コピーが自分の出所を主張するポインタの期待値 (JSON Pointer 表記)。
# ズレていたら出所不明の値なので落とす
EXPECTED_SOURCE = "{}#/{}".format(RULES_REL, SECTION)


def check_value(value, where):
    """閾値そのものの検査。問題文のリスト (空=正常) を返す。bool は int の派生なので弾く。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return ["{}: {} は int であること (現状 {!r})".format(where, KEY, value)]
    if not 0 < value <= 100:
        return [
            "{}: {} は 0 < N <= 100 の範囲であること (現状 {!r})".format(
                where, KEY, value
            )
        ]
    return []


def collect_problems(rules_doc, copy_doc, kustomization_text):
    """3 入力から drift・破損の問題リストを返す (空 = 一致)。純関数。"""
    problems = []

    if not isinstance(rules_doc, dict):
        problems.append("{}: JSON がオブジェクトでない".format(RULES_REL))
    elif not isinstance(rules_doc.get(SECTION), dict):
        problems.append("{}: 節 {} が無いかオブジェクトでない".format(RULES_REL, SECTION))

    rules_value = None
    if isinstance(rules_doc, dict) and isinstance(rules_doc.get(SECTION), dict):
        rules_value = rules_doc[SECTION].get(KEY)
        problems.extend(check_value(rules_value, RULES_REL))

    if not isinstance(copy_doc, dict):
        problems.append("{}: JSON がオブジェクトでない".format(COPY_REL))
    else:
        source = copy_doc.get("source")
        if source != EXPECTED_SOURCE:
            problems.append(
                "{}: source が {} を指していない (現状 {!r}) — 出所不明のコピーは置かない".format(
                    COPY_REL, EXPECTED_SOURCE, source
                )
            )
        copy_value = copy_doc.get(KEY)
        problems.extend(check_value(copy_value, COPY_REL))
        if (
            isinstance(rules_value, int)
            and not isinstance(rules_value, bool)
            and isinstance(copy_value, int)
            and not isinstance(copy_value, bool)
            and rules_value != copy_value
        ):
            problems.append(
                "閾値が一致しません (rules={!r} copy={!r})。rules.json 側を正として両方変えること".format(
                    rules_value, copy_value
                )
            )

    if not isinstance(kustomization_text, str):
        problems.append("{}: テキストとして読めない".format(KUSTOMIZATION_REL))
    else:
        listed = re.search(
            r"^(\s*)-\s+{}\s*$".format(re.escape(Path(COPY_REL).name)),
            kustomization_text,
            re.MULTILINE,
        )
        if not listed:
            problems.append(
                "{}: configMapGenerator files に {} が無い — in-cluster の /scripts に載らず reporter が読めない".format(
                    KUSTOMIZATION_REL, Path(COPY_REL).name
                )
            )

    return problems


def main() -> int:
    try:
        rules_doc = json.loads((ROOT / RULES_REL).read_text(encoding="utf-8"))
        copy_doc = json.loads((ROOT / COPY_REL).read_text(encoding="utf-8"))
        kustomization_text = (ROOT / KUSTOMIZATION_REL).read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — 読めないなら検査成立せず成功扱いにしない
        print(f"::error::同期コピー検査の入力を読めませんでした: {type(e).__name__}: {e}")
        return 1

    problems = collect_problems(rules_doc, copy_doc, kustomization_text)
    if problems:
        for p in problems:
            print(f"::error::{p}")
        return 1

    print(
        f"ok: {KEY} は {EXPECTED_SOURCE} (rules.json 側が正) と {COPY_REL} で一致、"
        f"kustomization.yaml 経由で /scripts に載る"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
