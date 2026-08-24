#!/usr/bin/env python3
"""curriculum 生成役の出力 (proposals JSON) を機械検査する。

生成と判定を分離した curriculum は、生成役の出力が schema から外れても
判定役が黙って読んで採点してしまう。ここで構造を落としてから渡す (P-0210)。
ops/check_feedback.py と同じ流儀 — 標準ライブラリのみ、引数で対象ファイルを
受け取り、違反で exit code 1。

検査するもの:
- schema 必須項目 (ops/prompts/curriculum-generate.md のスキーマ)
- verify 非空 (空の受入検証は骨抜き。validate.py が採択案に課すのと同じ条件を入口で)
- cell 語彙 ([領域, 種類] — 語彙は generate プロンプトとこのファイルで二重管理。
  変えるときは同時に変えること)
- 探索枠比率 — repair 以外の案が rules.json curriculum.exploration_quota 以上
  (単一情報源は rules.json。ここに値を写さない)

exit code: 0=合格 / 1=不合格・入力読めず / 2=使い方誤り
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

OPS = pathlib.Path(__file__).parent

ID_RE = re.compile(r"^P-\d{4}$")
CELL_DOMAINS = {"k8s", "storage", "observability", "security", "life-prep", "self"}
CELL_KINDS = {"repair", "prevent", "feature", "investigate", "experiment"}
CONFIDENCES = {"confident", "unsure"}
REQUIRED_KEYS = {
    "id", "title", "why", "cell", "dod", "verify",
    "irreversible", "capabilities", "touches_apps", "confidence",
}
QUOTA_EPS = 1e-9


def load_quota():
    """探索枠の下限を rules.json (単一情報源) から読む。"""
    rules = json.loads((OPS / "rules.json").read_text())
    quota = rules.get("curriculum", {}).get("exploration_quota")
    if not isinstance(quota, (int, float)) or not 0 <= quota <= 1:
        raise ValueError(
            f"rules.json: curriculum.exploration_quota={quota!r} は 0..1 の数でない"
        )
    return float(quota)


def check_proposals(data, quota):
    """提案列を検査し、違反メッセージの一覧を返す (空リスト = 合格)。"""
    proposals = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(proposals, list) or not proposals:
        return ["proposals が空か配列でない。1 案も書かずに終わるとラウンド全体が消える"]

    errors: list[str] = []
    seen_ids: set[str] = set()
    total = 0
    non_repair = 0

    for i, p in enumerate(proposals):
        pid = p.get("id", "?") if isinstance(p, dict) else "?"
        where = f"proposals[{i}] ({pid})"
        if not isinstance(p, dict):
            errors.append(f"{where}: オブジェクトでない")
            continue
        total += 1

        missing = REQUIRED_KEYS - set(p)
        if missing:
            errors.append(f"{where}: 必須キー不足 {sorted(missing)}")

        if not isinstance(pid, str) or not ID_RE.match(pid):
            errors.append(f"{where}: id は P-NNNN 形式であること (過去案の最大 id + 連番)")
        elif pid in seen_ids:
            errors.append(f"{where}: id が重複している")
        else:
            seen_ids.add(pid)

        for key in ("title", "why", "dod"):
            if key in p and not str(p.get(key) or "").strip():
                errors.append(f"{where}: {key} が空")

        cell = p.get("cell")
        kind = None
        if not (
            isinstance(cell, list) and len(cell) == 2
            and all(isinstance(x, str) for x in cell)
        ):
            errors.append(f"{where}: cell は [領域, 種類] の 2 要素")
        else:
            domain, kind = cell
            if domain not in CELL_DOMAINS:
                errors.append(
                    f"{where}: cell 領域 {domain!r} は語彙外 ({sorted(CELL_DOMAINS)})"
                )
            if kind not in CELL_KINDS:
                errors.append(
                    f"{where}: cell 種類 {kind!r} は語彙外 ({sorted(CELL_KINDS)})"
                )
            elif kind != "repair":
                non_repair += 1

        verify = p.get("verify")
        if not (
            isinstance(verify, list) and verify
            and all(isinstance(v, str) and v.strip() for v in verify)
        ):
            errors.append(
                f"{where}: verify は非空の bash 実行可能コマンド列であること"
                " (verify が書けない案は未成熟)"
            )

        if not isinstance(p.get("irreversible"), bool):
            errors.append(f"{where}: irreversible は真偽値")
        if not isinstance(p.get("capabilities"), list):
            errors.append(f"{where}: capabilities は配列")
        if not isinstance(p.get("touches_apps"), bool):
            errors.append(f"{where}: touches_apps は真偽値")

        # budget.soft_cap_tokens は 2026-08-24 に廃止 (定額移行で、消費量を理由に
        # 仕事を止めない)。古い案が付けてきても無視する

        if p.get("confidence") not in CONFIDENCES:
            errors.append(
                f"{where}: confidence={p.get('confidence')!r} は"
                f" {sorted(CONFIDENCES)} のいずれか"
            )

        # P-0091: 依頼と案の対応づけが破れると同じ依頼が毎回立案され続ける
        if p.get("proposed_by") == "human-request" and not str(p.get("request_id") or "").strip():
            errors.append(
                f"{where}: proposed_by=human-request の案には request_id (元依頼の id) が必須"
            )
        if p.get("request_id") and p.get("proposed_by") != "human-request":
            errors.append(
                f"{where}: request_id は proposed_by=human-request の案専用"
            )

    ratio = non_repair / total if total else 0.0
    if ratio < quota - QUOTA_EPS:
        errors.append(
            f"探索枠不足: repair 以外が {non_repair}/{total}"
            f" (下限 {quota:.2f})。全体の 1/4 以上は調査・実験・予防でなければならない"
            " (rules.json curriculum.exploration_quota)"
        )

    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <proposals.json>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        print(f"error: {path} を読めない: {e}")
        return 1
    except json.JSONDecodeError as e:
        print(f"error: {path} が JSON として壊れている: {e}")
        return 1

    try:
        quota = load_quota()
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"error: 探索枠の下限を読めない: {e}")
        return 1

    errors = check_proposals(data, quota)
    for e in errors:
        print(f"error: {e}")
    if errors:
        print(f"\n{len(errors)} error")
        return 1
    print(f"ok: {len(data['proposals'])} 案とも検査を通過 (探索枠込み)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
