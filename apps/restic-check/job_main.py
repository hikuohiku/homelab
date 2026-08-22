"""P-0102 — restic-check Pod の取りまとめ役 (本体コンテナの entrypoint)。

initContainer (restic-probe) は busybox sh しか無いため、リポジトリごとに生フィールド
3 ファイルを staging ディレクトリへ書く:

    {repo}.check_rc       — `restic check --read-data-subset` の終了コード (10 進整数)
    {repo}.snapshots_rc   — `restic snapshots --latest 1 --json` の終了コード
    {repo}.snapshots.out  — 同コマンドの標準出力そのもの

このスクリプトはそれを読んで、ops/restic_check_runner.py のレコード契約
  {"repo", "check_rc", "snapshots_rc", "snapshots_json"}
の JSON ファイルへ組み立ててから判定ロジックに委ねる。判定・通知・終了コードの
ロジックはここに 1 行も置かない — 単一情報源は ops/restic_check_runner.py で、
CI の ops/check_restic_check_script_sync.py がデプロイされるコピーとの一致を担保する。
取りこぼし (ファイル欠け・非整数・staging 全滅) はすべて「レコード欠落」に寄せられ、
runner 側で MISSING_RC として赤くなる。緑へ解釈する方向の潰し方はしない。
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import restic_check_runner  # noqa: E402 (ConfigMap 内で同階層に配置される)

STAGING_DIR = os.environ.get("RESTIC_CHECK_STAGING_DIR", "/work/results/staging")
RESULTS_DIR = os.environ.get("RESTIC_CHECK_RESULTS_DIR", "/work/results")


def _read_int(path):
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return restic_check_runner.MISSING_RC


def assemble(staging_dir=STAGING_DIR):
    """staging の生フィールド群 → runner のレコード契約の dict 群。

    glob の起点を *.check_rc (各リポジトリが必ず書く最初の 1 枚) にするので、
    check より前に initContainer が死んだリポジトリはレコードごと欠落し、
    runner の「全レコードが揃わなければ赤」契約で拾われる。
    """
    staging = Path(staging_dir)
    records = []
    for rc_path in sorted(staging.glob("*.check_rc")):
        repo = rc_path.name[: -len(".check_rc")]
        try:
            snapshots_json = (staging / f"{repo}.snapshots.out").read_text(
                encoding="utf-8"
            )
        except OSError:
            snapshots_json = ""
        records.append(
            {
                "repo": repo,
                "check_rc": _read_int(rc_path),
                "snapshots_rc": _read_int(staging / f"{repo}.snapshots_rc"),
                "snapshots_json": snapshots_json,
            }
        )
    return records


def write_records(records, results_dir=RESULTS_DIR):
    out = Path(results_dir)
    out.mkdir(parents=True, exist_ok=True)
    for rec in records:
        (out / f"{rec['repo']}.json").write_text(
            json.dumps(rec), encoding="utf-8"
        )
    return len(records)


def main():
    records = assemble()
    print(f"assembled {len(records)} record(s) from {STAGING_DIR}")
    write_records(records)
    return restic_check_runner.main()


if __name__ == "__main__":
    sys.exit(main())
