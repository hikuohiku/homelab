#!/usr/bin/env python3
"""ops/stage3/readiness.json の schema と verdict 規則 (P-0185)。

なぜ在るか:
  段階 3 の開放判断を「その日の気分」にしないために、閾値と証拠を事前に台帳として
  固定する。台帳そのもの (readiness.json) はデータでしかなく、形が崩れても気づけない。
  このモジュールは台帳の不変条件 — 必須キー / verdict の 2 値性 / 必須観点の網羅 /
  evidence_path の存在 — を純粋関数として持ち、ops/tests/test_stage3_readiness.py が
  合成入力で両方向 (落ちること / 通ること) を固定する。check スクリプト群
  (P-0071/P-0105) と同じ流儀。
fail-closed の方針:
  - criteria が空 / 必須キー欠損 / pass が真偽値でない / 観点が抜けている → すべて schema 違反
  - verdict は「全 criteria の pass が true のときだけ ready_for_announce_draft」。
    1 つでも false や不明があれば blocked。開けない側に倒すのは heartbeat 判定と同じ
"""

from __future__ import annotations

import json
import pathlib

# spec DoD 固定の必須キー。これ以上のキー (備考等) を各基準が持つことは許す
REQUIRED_KEYS = ("id", "criterion", "threshold", "current_value", "evidence_path", "pass")
STRING_KEYS = ("id", "criterion", "threshold", "current_value", "evidence_path")

VERDICTS = ("blocked", "ready_for_announce_draft")

# 採点表が最低限カバーすべき観点 (spec DoD (1))。id で固定する — criterion 文言の
# 変更は自由だが、この 5 つの id が揃わない台帳は段階 3 の審査材料にならない
MANDATORY_PERSPECTIVES = (
    "trifecta-separation-drill",  # lethal trifecta 分離の実証
    "veto-channel-live",          # 最新チャネルでの veto 到達性
    "secrets-audit-wired",        # 秘密分離の監査済み
    "restore-proven",             # バックアップ復元の実証
    "loop-continuity-guarded",    # ループ連続性 (watchdog・livenessProbe)
)

READY_VERDICT = "ready_for_announce_draft"
BLOCKED_VERDICT = "blocked"


def load(path) -> dict:
    """readiness.json を読む。読めない・壊れているときは例外を上げる (呼び出し側で fail-closed)。"""
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def validate(doc) -> list[str]:
    """schema 検査 (純粋関数)。違反の説明文リストを返す。空リスト = 合格。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["トップレベルが object ではない"]
    if doc.get("verdict") not in VERDICTS:
        errors.append(
            f"verdict は {VERDICTS} のいずれかであるべき (実際: {doc.get('verdict')!r})"
        )

    criteria = doc.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("criteria は 1 件以上の配列であるべき")
        return errors

    seen_ids: set[str] = set()
    for i, c in enumerate(criteria):
        label = f"criteria[{i}]" if not isinstance(c, dict) else f"criteria[{i}] ({c.get('id')!r})"
        if not isinstance(c, dict):
            errors.append(f"{label}: object ではない")
            continue
        missing = [k for k in REQUIRED_KEYS if k not in c]
        if missing:
            errors.append(f"{label}: 必須キーが無い {missing}")
        for k in STRING_KEYS:
            if k in missing:
                continue
            if not isinstance(c[k], str) or not c[k].strip():
                errors.append(f"{label}: {k} は空でない文字列であるべき (実際: {c[k]!r})")
        if "pass" in c and not isinstance(c["pass"], bool):
            errors.append(f"{label}: pass は真偽値であるべき (実際: {c['pass']!r})")
        cid = c.get("id")
        if isinstance(cid, str) and cid:
            if cid in seen_ids:
                errors.append(f"{label}: id が重複している ({cid})")
            seen_ids.add(cid)
    return errors


def missing_perspectives(doc) -> list[str]:
    """必須観点 (spec DoD (1)) のうち採点表に無い id を返す (純粋関数)。

    schema 検査からは分離してある — 項目数の下限や観点の網羅は台帳の品質要件、
    必須キーの形は構造要件で、落ちる理由を混ぜない方が「どこが悪いか」が直接読める。
    """
    criteria = doc.get("criteria") if isinstance(doc, dict) else None
    if not isinstance(criteria, list):
        return list(MANDATORY_PERSPECTIVES)
    seen = {c.get("id") for c in criteria if isinstance(c, dict)}
    return [p for p in MANDATORY_PERSPECTIVES if p not in seen]


def compute_verdict(criteria) -> str:
    """verdict の判定規則 (純粋関数)。全 pass のときだけ ready 側、それ以外は blocked。

    criteria が空でも blocked (何も採点していないのに開ける判断はしない)。
    """
    if not criteria:
        return BLOCKED_VERDICT
    if all(c.get("pass") is True for c in criteria):
        return READY_VERDICT
    return BLOCKED_VERDICT


def missing_evidence(doc, root=pathlib.Path(".")) -> list[str]:
    """evidence_path が実在しない基準のパス一覧 (純粋関数 + ファイル存在だけの I/O)。

    存在検査を通すためのダミーファイルを作るのは証拠の捏造なので、不在なら
    台帳側を正直に直す (pass=false にして不在の根拠を指す) のが正しい直し方。
    """
    rel = doc.get("criteria") if isinstance(doc, dict) else None
    if not isinstance(rel, list):
        return ["(criteria が読めないため evidence の存在確認ができない)"]
    out: list[str] = []
    for c in rel:
        path = c.get("evidence_path") if isinstance(c, dict) else None
        if not path:
            continue
        if not (pathlib.Path(root) / path).exists():
            out.append(path)
    return out
