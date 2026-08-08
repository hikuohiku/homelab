#!/usr/bin/env python3
"""ops-health-reporter が「生きている方の autopilot」を観測しているか検査する。

apps/ops-health-reporter/report.py の collect_autopilot_health() は、Deployment 名と
Pod の label セレクタを文字列で持っている。心臓側 (apps/autopilot/) をリネームしても
report.py は静かに古い名前を見続け、**heart が死んでも死ぬ前と同じ文字列を出す** —
異常が定常状態に埋もれる。実際に 2026-08-08 まで、退役済みの Deployment `autopilot`
(replicas 0) と label `app=autopilot` を見たままだった (P-0011)。名前の存在確認だけでは
この形の drift は捕まらない (退役した方も名前としては実在する) ので、
**replicas >= 1 の Deployment を正とする**。

検査するのは 2 つの結合:

1. 観測対象 — report.py の AUTOPILOT_DEPLOYMENT / AUTOPILOT_APP_LABEL が、
   apps/autopilot/*.yaml にある **replicas >= 1 の** Deployment の
   metadata.name / spec.selector.matchLabels.app と一致すること
2. 心拍行の書式 — ops/heart/heart.py の log() が実際に出す行を、report.py の
   HEARTBEAT_RE が拾えること。ops/memory/substrate.md が「変えるときは同時に変える」と
   注意書きで縛っていた結合を機械検査に格上げしたもの。産出側のコードを実際に呼んで
   行を作るので、書式を片側だけ変えればここで落ちる

CI (ops job) から実行する。ops/check_pvc_usage_script_sync.py と同じ流儀 —
標準ライブラリのみ、正規表現、抽出に失敗したら成功扱いにせず落とす (fail-closed)。
report.py はモジュールトップで ServiceAccount トークンを開くのでクラスタ外では
import できない。ソースをテキストとして読むこと。
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REPORT_REL = "apps/ops-health-reporter/report.py"
AUTOPILOT_MANIFEST_DIR = "apps/autopilot"
HEART_REL = "ops/heart/heart.py"

# heart.py の f-string を実際の行にするための値。log() に渡される f-string の
# プレースホルダ名がここに無ければ落とす (勝手に埋めると検査が緩む)
HEART_PLACEHOLDER_SAMPLES = {"i": "14", "rc": "0", "elapsed": "3"}


def _fail(msg: str) -> None:
    print(f"::error::{msg}")


def extract_report_constant(source: str, name: str) -> str:
    m = re.search(rf'^{re.escape(name)}\s*=\s*"([^"]+)"\s*$', source, re.MULTILINE)
    if not m:
        raise ValueError(
            f"{REPORT_REL}: 定数 {name} が見つからない"
            " (観測対象は URL へ直書きせず、この名前の定数で持つこと)"
        )
    return m.group(1)


def extract_heartbeat_pattern(source: str) -> str:
    m = re.search(
        r"^HEARTBEAT_RE\s*=\s*re\.compile\(\s*(r?\"(?:[^\"\\]|\\.)*\")\s*,?\s*\)",
        source,
        re.MULTILINE,
    )
    if not m:
        raise ValueError(f"{REPORT_REL}: HEARTBEAT_RE のパターン文字列を抽出できない")
    return eval(m.group(1))  # noqa: S307 — 対象はリポジトリ内の文字列リテラルのみ


def parse_deployments(directory: Path) -> list[dict]:
    """kind: Deployment の doc から name / selector の app ラベル / replicas を抜く。

    PyYAML に依存しない (この repo の ops/check_*.py は誰も yaml を import していない)。
    apps/autopilot/*.yaml は 1 ファイル 1 doc・インデント 2 の素の manifest なので、
    キーのフルパスをインデントから組み立てる素朴なスキャンで足りる。
    """
    out = []
    for path in sorted(directory.glob("*.yaml")):
        for doc in re.split(r"^---\s*$", path.read_text(), flags=re.MULTILINE):
            fields: dict[str, str] = {}
            stack: list[tuple[int, str]] = []
            for raw in doc.splitlines():
                m = re.match(r"^( *)([A-Za-z0-9_.-]+):\s*(.*?)\s*$", raw)
                if not m:
                    continue
                indent, key, value = len(m.group(1)), m.group(2), m.group(3)
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                path_key = ".".join([k for _, k in stack] + [key])
                stack.append((indent, key))
                if value and not value.startswith("#"):
                    fields.setdefault(path_key, value)
            if fields.get("kind") != "Deployment":
                continue
            replicas = fields.get("spec.replicas")
            out.append(
                {
                    "file": path.relative_to(ROOT).as_posix(),
                    "name": fields.get("metadata.name"),
                    "app_label": fields.get("spec.selector.matchLabels.app"),
                    # replicas 未指定は k8s 既定の 1
                    "replicas": int(replicas) if replicas is not None else 1,
                }
            )
    return out


def extract_heart_log_lines() -> list[str]:
    """ops/heart/heart.py の log() を実際に呼んで心拍行を作る。

    書式を静的に読むのではなく産出側のコードそのものに出させる。log() の前置き
    (`[autopilot] <ts> `) もタイムスタンプの書式も実物になる。
    """
    sys.path.insert(0, str(ROOT))
    from ops.heart.heart import log  # noqa: PLC0415 — ROOT を sys.path に入れた後で読む

    source = (ROOT / HEART_REL).read_text()
    templates = [
        t
        for t in re.findall(r'\blog\(\s*f"([^"]*)"\s*\)', source)
        if "iteration #" in t
    ]
    if len(templates) < 2:
        raise ValueError(
            f"{HEART_REL}: log() に渡す 'iteration #' の f-string が"
            f" {len(templates)} 個しか見つからない (start と end の 2 つを期待)"
        )

    messages = []
    for template in templates:
        rendered = template
        for placeholder in re.findall(r"\{([^}]*)\}", template):
            if placeholder not in HEART_PLACEHOLDER_SAMPLES:
                raise ValueError(
                    f"{HEART_REL}: f-string の {{{placeholder}}} に対応する見本値が無い"
                    " (このスクリプトの HEART_PLACEHOLDER_SAMPLES に足すこと)"
                )
            rendered = rendered.replace(
                "{" + placeholder + "}", HEART_PLACEHOLDER_SAMPLES[placeholder]
            )
        messages.append(rendered)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for message in messages:
            log(message)
    return buf.getvalue().splitlines()


def main() -> int:
    try:
        report_src = (ROOT / REPORT_REL).read_text()
        deployment = extract_report_constant(report_src, "AUTOPILOT_DEPLOYMENT")
        app_label = extract_report_constant(report_src, "AUTOPILOT_APP_LABEL")
        pattern = extract_heartbeat_pattern(report_src)
        deployments = parse_deployments(ROOT / AUTOPILOT_MANIFEST_DIR)
        heart_lines = extract_heart_log_lines()
    except Exception as e:  # noqa: BLE001 — 抽出に失敗したら成功扱いにしない
        _fail(f"検査に必要な情報を取り出せませんでした: {type(e).__name__}: {e}")
        return 1

    if not deployments:
        _fail(f"{AUTOPILOT_MANIFEST_DIR}/*.yaml に Deployment が 1 つも見つかりません")
        return 1

    live = [d for d in deployments if d["replicas"] >= 1]
    known = ", ".join(f"{d['name']}(replicas={d['replicas']})" for d in deployments)

    if not live:
        _fail(
            f"{AUTOPILOT_MANIFEST_DIR}/*.yaml に replicas >= 1 の Deployment がありません"
            f" ({known})。観測できる心臓が無い状態です"
        )
        return 1

    target = next((d for d in live if d["name"] == deployment), None)
    if target is None:
        _fail(
            f"{REPORT_REL} の AUTOPILOT_DEPLOYMENT={deployment!r} は"
            f" {AUTOPILOT_MANIFEST_DIR} の稼働中 (replicas >= 1) の Deployment と"
            f" 一致しません。既存: {known}"
        )
        _fail(
            "  稼働中の Deployment 名に合わせてください"
            " (退役済み replicas 0 の方を指したままだと、心臓が死んでも"
            " health レポートは死ぬ前と同じ文字列を出し続けます)"
        )
        return 1

    if target["app_label"] != app_label:
        _fail(
            f"{REPORT_REL} の AUTOPILOT_APP_LABEL={app_label!r} が"
            f" {target['file']} の spec.selector.matchLabels.app="
            f"{target['app_label']!r} と一致しません"
        )
        return 1

    unmatched = [line for line in heart_lines if not re.match(pattern, line.strip())]
    if unmatched or not heart_lines:
        _fail(
            f"{REPORT_REL} の HEARTBEAT_RE が {HEART_REL} の log() が出す心拍行を"
            f" 拾えません: {unmatched}"
        )
        _fail(f"  HEARTBEAT_RE: {pattern}")
        _fail(
            "  産出側 (heart) と抽出側 (report.py) の書式は同時に変えること"
            " (ops/memory/substrate.md「観測経路」)"
        )
        return 1

    print(
        f"ok: {REPORT_REL} は稼働中の Deployment {target['name']}"
        f" (app={target['app_label']}, replicas={target['replicas']}, {target['file']})"
        " を観測しています"
    )
    print(f"ok: HEARTBEAT_RE は {HEART_REL} の心拍行 {len(heart_lines)} 種を拾えます")
    for line in heart_lines:
        print(f"    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
