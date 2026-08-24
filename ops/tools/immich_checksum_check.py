#!/usr/bin/env python3
"""immich アセット整合性検証の解釈ロジック (P-0361)。

immich は「写真の実データを持つ唯一のサービス」で、ホスト側のファイルが黙って壊れる
(bit rot・誤書き込み) と人間の唯一の原本が静かに死ぬ。immich v3.0.0 導入の整合性検証
(`integrity-checksum-mismatch` ジョブ) はこの腐りを checksum 再計算で検出し、
`integrity_report` テーブルに ChecksumFail を書き込む。このモジュールはその結果の
解釈・不一致判定だけを担う純関数群で、クラスタやネットワークに触れない。

- 産出側 (apps/immich/checksum-cronjob.yaml): immich API でジョブをトリガーして
  完了を待ち、`GET /api/admin/integrity/summary` の checksum_mismatch 件数を
  このモジュールの解釈関数で判定し、専用 ConfigMap `immich-checksum-report` の
  report.json へ書く (稼働 CronJob は別途。本ファイルは判断ロジックの正)。
- 集約側 (apps/ops-health-reporter/report.py): その ConfigMap を読んで latest.json の
  `checksum` 節へ畳む (rbac.yaml の resourceNames に追記)。

API 形は immich 上流のソース (v3.1.0 相当。server/src/controllers/
integrity-admin.controller.ts と server/src/enum.ts の ManualJobName / IntegrityReport)
に基づく。**クラスタ内の実機での確定 (実際のレスポンス・所要時間・checkpoint 再開の
挙動) は docs/immich-checksum.md と PROGRESS.md に残す** — このファイルの定数と実機が
食い違ったら、実測値を根拠に fixture ごと修正すること。

使い方:

    python3 ops/tools/immich_checksum_check.py --selftest   # 同梱 fixture で自己検証
    python3 ops/tools/immich_checksum_check.py --summary summary.json --threshold 1
        # 実機の GET /api/admin/integrity/summary のレスポンスを解釈して report を表示
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys

# immich の integrity 関連 API の識別子 (v3.1.0 相当、上流ソースから)。
# ChecksumFail の report type は enum IntegrityReport.ChecksumFail = 'checksum_mismatch'。
# 手動ジョブ名は enum ManualJobName.IntegrityChecksumFiles = 'integrity-checksum-mismatch'。
# `POST /api/jobs` body {"name": <JOB_NAME>} でトリガー (admin 要、204)。結果は
# `GET /api/admin/integrity/summary` が {checksum_mismatch, missing_file, untracked_file}
# の件数で返す。実機確定は docs/immich-checksum.md に残す
REPORT_TYPE = "checksum_mismatch"
JOB_NAME = "integrity-checksum-mismatch"
SUMMARY_KEYS = ("checksum_mismatch", "missing_file", "untracked_file")

# 産出側が書く ConfigMap とキー (report.py / rbac.yaml と合わせる)
CONFIGMAP_NAME = "immich-checksum-report"
REPORT_KEY = "report.json"

# rules.json の checksum.mismatch_threshold (DoD (3)) が唯一の宣言元。ここに既定値を
# 持たない — 黙って 0 を決め打ちすると閾値が 2 か所になる (download_budget の
# DEFAULT_DAILY_CAP_BYTES と同じ思想)。CronJob / CLI は明示的に渡す
DEFAULT_MISMATCH_THRESHOLD = None


def parse_summary(raw):
    """`GET /api/admin/integrity/summary` のレスポンスを正規化する。

    immich v3 の形: {"checksum_mismatch": N, "missing_file": N, "untracked_file": N}
    の非負 int 3 つ。形が違う・int でない・負値は ValueError — 黙って 0 扱いにすると
    「検出ゼロ」と「装置が壊れた」が区別できなくなる (dashboard_smoke の ok 判定と
    同じ思想。壊れた記録を読み手に error として見せる)。
    """
    if not isinstance(raw, dict):
        raise ValueError("integrity summary が dict でない")
    for key in SUMMARY_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                "integrity summary の {} が非負 int でない: {!r}".format(key, value)
            )
        if value < 0:
            raise ValueError("integrity summary の {} が負値: {!r}".format(key, value))
    return {key: raw[key] for key in SUMMARY_KEYS}


def normalize_threshold(value):
    """閾値の検査。非負 int のみ受け付け、None (未設定) はそのまま通す。

    bool は int の派生なので明示的に弾く。負値・文字列・float は不正として None
    (download_budget.coerce_bytes と同じ思想。黙って 0 扱いにしない)。
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def judge_mismatch(mismatch_count, threshold=DEFAULT_MISMATCH_THRESHOLD):
    """ChecksumFail 件数と閾値から status / reason を返す (DoD (3) の判定)。

    threshold は rules.json の checksum.mismatch_threshold が唯一の宣言元で、
    CronJob が env で渡す。None (未設定) は unconfigured — 0 を決め打ちしない
    (バックアップ側の P-0187 とは違い、この装置は「0 でなければ嘘」ではなく
    「閾値の宣言が無ければ検出数を正直に見せる」側に倒す)。
    """
    if threshold is None:
        return {
            "status": "unconfigured",
            "reason": "不一致の閾値が未設定 (rules.json の checksum.mismatch_threshold を設定すること)",
        }
    if mismatch_count >= threshold:
        return {
            "status": "fail",
            "reason": "checksum_mismatch が {} 件 (閾値 {} 以上) — 原本の腐りを検出".format(
                mismatch_count, threshold
            ),
        }
    return {
        "status": "ok",
        "reason": "checksum_mismatch が {} 件 (閾値 {} 未満)".format(
            mismatch_count, threshold
        ),
    }


def build_report(summary, generated_at, threshold=DEFAULT_MISMATCH_THRESHOLD, job=None):
    """解釈結果を report.json の形へ。産出側 (CronJob) と集約側 (report.py) の契約。

    summary は `GET /api/admin/integrity/summary` のレスポンス dict。
    generated_at は report 生成時刻の "YYYY-MM-DDTHH:MM:SSZ" 文字列。
    job は任意の実行メタ ({name, triggered_at, ...}) で、あるときだけ載せる。
    """
    parsed = parse_summary(summary)
    judged = judge_mismatch(parsed["checksum_mismatch"], threshold)
    report = {
        "generated_at": generated_at,
        "namespace": "immich",
        "status": judged["status"],
        "reason": judged["reason"],
        "ok": judged["status"] == "ok",
        "checksum_mismatch": parsed["checksum_mismatch"],
        "missing_file": parsed["missing_file"],
        "untracked_file": parsed["untracked_file"],
    }
    if job:
        report["job"] = job
    return report


# --selftest 用の同梱 fixture。実機のレスポンス形が確定したら、実測値を根拠に更新する
# (PROJECT.md「実機のレスポンス形が確定したら fixture を実測値で更新する」)。
FIXTURES = [
    {
        "name": "ok_zero_mismatch",
        "summary": {"checksum_mismatch": 0, "missing_file": 1, "untracked_file": 2},
        "threshold": 1,
        "expect_status": "ok",
        "expect_mismatch": 0,
    },
    {
        "name": "fail_mismatch",
        "summary": {"checksum_mismatch": 3, "missing_file": 0, "untracked_file": 0},
        "threshold": 1,
        "expect_status": "fail",
        "expect_mismatch": 3,
    },
    {
        "name": "threshold_zero_means_any_mismatch",
        "summary": {"checksum_mismatch": 1, "missing_file": 0, "untracked_file": 0},
        "threshold": 0,
        "expect_status": "fail",
        "expect_mismatch": 1,
    },
    {
        "name": "unconfigured_without_threshold",
        "summary": {"checksum_mismatch": 0, "missing_file": 0, "untracked_file": 0},
        "threshold": None,
        "expect_status": "unconfigured",
        "expect_mismatch": 0,
    },
    {
        "name": "malformed_missing_key",
        "summary": {"checksum_mismatch": 0, "missing_file": 0},
        "threshold": 1,
        "expect_error": True,
    },
    {
        "name": "malformed_negative_count",
        "summary": {"checksum_mismatch": -1, "missing_file": 0, "untracked_file": 0},
        "threshold": 1,
        "expect_error": True,
    },
    {
        "name": "malformed_bool_count",
        "summary": {"checksum_mismatch": True, "missing_file": 0, "untracked_file": 0},
        "threshold": 1,
        "expect_error": True,
    },
    {
        "name": "malformed_non_dict",
        "summary": ["checksum_mismatch", 0],
        "threshold": 1,
        "expect_error": True,
    },
]


def run_selftest():
    """同梱 fixture を純関数に通し、期待値を断言する。失敗は列挙して 0/1 を返す。"""
    failures = []
    for fixture in FIXTURES:
        name = fixture["name"]
        try:
            parsed = parse_summary(fixture["summary"])
            judged = judge_mismatch(parsed["checksum_mismatch"], fixture["threshold"])
            mismatch = parsed["checksum_mismatch"]
            if fixture.get("expect_error"):
                failures.append("{}: 例外が出るはずが成功した".format(name))
                continue
            if judged["status"] != fixture["expect_status"]:
                failures.append(
                    "{}: status {} != 期待 {}".format(
                        name, judged["status"], fixture["expect_status"]
                    )
                )
            if mismatch != fixture["expect_mismatch"]:
                failures.append(
                    "{}: checksum_mismatch {} != 期待 {}".format(
                        name, mismatch, fixture["expect_mismatch"]
                    )
                )
        except ValueError as e:
            if not fixture.get("expect_error"):
                failures.append("{}: 予期しない例外: {}".format(name, e))

    if failures:
        for message in failures:
            print("FAIL {}".format(message), file=sys.stderr)
        print("selftest: {} 件失敗".format(len(failures)), file=sys.stderr)
        return 1
    print("selftest: 全 {} fixture 合格".format(len(FIXTURES)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selftest", action="store_true", help="同梱 fixture で自己検証する"
    )
    parser.add_argument(
        "--summary", metavar="PATH", help="integrity summary の JSON ファイルを解釈する"
    )
    parser.add_argument(
        "--threshold", type=int, default=DEFAULT_MISMATCH_THRESHOLD,
        help="不一致とみなす checksum_mismatch 件数 (既定: rules.json 宣言待ち = 未設定)",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    if args.summary:
        with open(args.summary, encoding="utf-8") as f:
            raw = json.load(f)
        report = build_report(
            raw,
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            normalize_threshold(args.threshold),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())