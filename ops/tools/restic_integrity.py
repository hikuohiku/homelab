"""全 restic リポジトリの実データ回転読み (--read-data-subset) の選択ロジック (P-0187)。

バックアップの「守り」は鮮度 (P-0157)・復元速度 (P-0080/114/115)・改変不能性 (P-0168) と
順に厚くなったが、保存済みデータを読み返して壊れていないか確認した者はいなかった。
B2 は 11 ナインを保証しない一般オブジェクトストアであり、bit rot は取り出す当日まで黙る。
このモジュールは「今月どのスライスを読むか」と「これまでいくら読んだか」を決める純関数群
だけを持ち、クラスタにも B2 にも触れない。実行側 (各 ns の integrity CronJob) と集計側
(ops-health-reporter) の両方がここに依存する正本であり、クラスタへの埋め込みコピーとの
同期は別途検査する (check_download_ledger_script_sync.py 流儀)。

契約 (ops/tests/test_restic_integrity.py で固定):

- 決定論的 & 再実行可能: スライス番号は (repo id, UTC 日付) だけの関数。同じ日の再実行は
  同じスライスを読むため、失敗リトライが二重カウントや読み漏らしの原因にならない
- 約 3 ヶ月で一周: 既定サイクル T=3・月次実行。restic の N/T 形式
  (`check --read-data-subset=N/T`) は pack 集合を ID ハッシュで T 分割した第 N グループを
  決定論的に読む。パーセント形式は実行ごとのランダム抽出であり「回転」にならないので使わない
- カバー率は嘘をつかない: 実行記録 ({date, slot}) の集計で算出し、記録の日付から
  slot_for_date() が導く期待値と一致しない記録は採用しない。CronJob の Job 履歴は
  successfulJobsHistoryLimit で消えるため、長期記憶は産出側が成功時に書き込む ConfigMap 側が
  唯一の拠りになる (download-budget と同じ発想)

対象リポジトリは initializer の実測 (2026-08-23、apps/ 配下全 CronJob の RESTIC_REPOSITORY)
による 5 本。spec 列挙の「autopilot-core 相当」に対応する restic リポジトリは実在しない
(apps/autopilot-core/ には PVC があるが restic backup が無い)。

B2 download cap はアカウント単位・毎日 00:00 UTC リセット (P-0111 root_cause.md 確定分)。
1 回の実行が読むのは総データ量の約 1/T。retention 群が日曜朝に集中している実測があるため、
integrity CronJob のスケジュール (実行日・時刻) はこれを避けて分散すること — このモジュールは
「いつ動くか」を持たず、「その日に何を読むか」だけを答える。
"""

import argparse
import datetime
import hashlib

# 「全データを約 3 ヶ月で一周」の T。月次実行とセット。repo ごとのデータ総量は repo 内 docs
# には無いため読み量 (= 総量 ÷ T) の見積もりは manifest コメント側に根拠ごと書く。
# 1 スライスが大きすぎると判明した場合は T を増やして実行頻度を上げる (合計カバー期間
# ≈ 3 ヶ月を維持するのが spec の精神に沿う調整方向)。
DEFAULT_CYCLE_MONTHS = 3

# restic リポジトリの実在一覧 (RESTIC_REPOSITORY = b2:<bucket>:<名前> の <名前>)。
# initializer が 2026-08-23 に apps/ 配下の全 CronJob から実測した値。
REPOSITORIES = (
    "coder-postgres",
    "coder-workspace-homes",
    "immich",
    "syncthing",
    "vaultwarden",
)


def parse_date(value):
    """YYYY-MM-DD 文字列 / datetime.date / datetime.datetime を datetime.date へ。

    文字列は strptime で厳格に検査する (2026-02-30 など暦に無い日は ValueError)。
    datetime は .date() へ潰す — タイムゾーン付きオブジェクトを渡された場合の
    「どこの日付か」は呼び出し側の責務で、ここでは UTC 日付を渡されることを期待する
    (スロット選択のキーは UTC 日付。cap のリセットも 00:00 UTC なので UTC が唯一の
    自然な区切り)。それ以外の型は TypeError。
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    raise TypeError(
        "date は YYYY-MM-DD 文字列か date を受け付ける: {!r}".format(value))


def month_sequence(date):
    """暦月を 1 ずつ増える整数へ (year*12 + month - 1)。年跨ぎの回転計算の基礎。

    date(2026, 12, x) と date(2027, 1, x) の差がちょうど 1 になることが大事。
    """
    d = parse_date(date)
    return d.year * 12 + d.month - 1


def _validate_cycle(cycle):
    # bool は int の派生なので明示的に弾く (download_budget.coerce_bytes と同じ倒し方)
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise ValueError("cycle は 1 以上の整数: {!r}".format(cycle))
    return cycle


def _validate_repo_id(repo_id):
    if not isinstance(repo_id, str) or not repo_id:
        raise ValueError("repo_id は空でない文字列: {!r}".format(repo_id))
    return repo_id


def repo_offset(repo_id, cycle=DEFAULT_CYCLE_MONTHS):
    """repo 固有の回転開始位相 (0..cycle-1)。

    組込の hash() は str randomization で起動ごとに変わるため sha256 を使う。
    先頭 8 バイト (big endian) で十分 — 衝突しても「複数 repo の位相が揃う」だけで、
    各 repo が周期ごとに全スライスを巡る性質は壊れない。
    """
    _validate_repo_id(repo_id)
    _validate_cycle(cycle)
    digest = hashlib.sha256(repo_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % cycle


def slot_for_date(repo_id, date, cycle=DEFAULT_CYCLE_MONTHS):
    """(repo, UTC 日付) → 今月読むスライス番号 N (1..cycle)。

    同じ日なら何度呼んでも同じ値 (再実行可能)。同じ月の中であれば日付によらず
    同じ値なので、スケジュールのずれや当月内のリトライでスライスが変わらない。
    """
    _validate_repo_id(repo_id)
    _validate_cycle(cycle)
    sequence = month_sequence(date)
    return (sequence + repo_offset(repo_id, cycle)) % cycle + 1


def subset_arg(slot, cycle=DEFAULT_CYCLE_MONTHS):
    """restic へ渡す N/T 形式の引数値 ("2/3" など)。

    restic は `check --read-data-subset=N/T` の N/T を pack ID ハッシュによる決定論的な
    T 分割の第 N グループとして解釈する。範囲外の slot は静かに通さない。
    """
    _validate_cycle(cycle)
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= cycle:
        raise ValueError("slot は 1..cycle の整数: {!r} (cycle={})".format(slot, cycle))
    return "{}/{}".format(slot, cycle)


def plan(repo_id, date=None, cycle=DEFAULT_CYCLE_MONTHS):
    """実行側 (integrity CronJob) へ渡す今月の計画。

    返り値は KEY=VALUE 出力と結果 ConfigMap 書き込みの両方で使う形。
    date を省略した場合は今日 (UTC)。実行側はこれを保存せず都度呼ぶ —
    計画は入力だけの関数なので状態を持たず、再実行で同じ答えになる。
    """
    _validate_repo_id(repo_id)
    _validate_cycle(cycle)
    if date is None:
        date = datetime.datetime.now(datetime.timezone.utc).date()
    d = parse_date(date)
    slot = slot_for_date(repo_id, d, cycle)
    return {
        "repo": repo_id,
        "date": d.isoformat(),
        "cycle": cycle,
        "offset": repo_offset(repo_id, cycle),
        "slot": slot,
        "subset": subset_arg(slot, cycle),
    }


def coverage_from_records(repo_id, records, today=None, cycle=DEFAULT_CYCLE_MONTHS):
    """実行記録 ({date: "YYYY-MM-DD", slot: N}) から直近 cycle か月の累積カバー率を算出する。

    records は「成功した実行が書いた記録」のリスト (新しい順でも古い順でもよい)。
    長期記憶は ConfigMap 側なので、ここでは与えられた記録だけを信じて集計する。
    ただし盲信はしない:

    - 未来日付は clock skew の疑いがあるため採用しない (heartbeat judge() と同じ倒し方)
    - 直近 cycle か月 (today の属する月を含む) の外の記録は窓の外として数えない
      (窓内で一周した分だけが「現在のカバー率」の意味)
    - 記録の日付から slot_for_date() が導く期待値と slot が一致しない記録は
      inconsistent として採用しない — 記録の書き間違い・改ざんでカバー率が盛れない
    - 壊れた記録 (dict 以外・壊れた日付・bool/int 以外の slot) は例外を出さず skipped に
      数える (report.py collect() と同じ思想: 1 レコードの壊れで全体を止めない)

    同一月の再実行は同じスライスになるため重複記録は自然に潰れる (set 集計)。
    """
    _validate_repo_id(repo_id)
    _validate_cycle(cycle)
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()
    else:
        today = parse_date(today)
    last_sequence = month_sequence(today)
    first_sequence = last_sequence - (cycle - 1)

    seen = set()
    skipped = {"malformed": 0, "future": 0, "out_of_window": 0, "inconsistent": 0}
    total = 0
    for record in records or []:
        total += 1
        if not isinstance(record, dict):
            skipped["malformed"] += 1
            continue
        try:
            d = parse_date(record.get("date"))
        except (TypeError, ValueError):
            skipped["malformed"] += 1
            continue
        slot = record.get("slot")
        if d is None or isinstance(slot, bool) or not isinstance(slot, int):
            skipped["malformed"] += 1
            continue
        if d > today:
            skipped["future"] += 1
            continue
        sequence = month_sequence(d)
        if sequence < first_sequence or sequence > last_sequence:
            skipped["out_of_window"] += 1
            continue
        if slot != slot_for_date(repo_id, d, cycle):
            skipped["inconsistent"] += 1
            continue
        seen.add(slot)

    missing = [n for n in range(1, cycle + 1) if n not in seen]
    fraction = len(seen) / cycle
    return {
        "repo": repo_id,
        "cycle": cycle,
        "window_first_month": _format_month(first_sequence),
        "window_last_month": _format_month(last_sequence),
        "slices_seen": sorted(seen),
        "missing_slices": missing,
        "coverage_fraction": fraction,
        "coverage_percent": round(fraction * 100, 1),
        "records_total": total,
        "skipped": skipped,
    }


def _format_month(sequence):
    year, month0 = divmod(sequence, 12)
    return "{:04d}-{:02d}".format(year, month0 + 1)


def main(argv=None):
    """CLI。integrity CronJob の initContainer から SUBSET=... 行を機械消費する想定。

    restic/restic イメージには python が無いため、実行側は python イメージの
    initContainer でこの CLI を叩いて共有 emptyDir に書かせる (vaultwarden の
    sqlite-snapshot initContainer と同じ構図)。
    """
    parser = argparse.ArgumentParser(
        prog="restic_integrity",
        description="restic --read-data-subset の回転スライスを選ぶ (P-0187)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan", help="指定 repo の今月のスライスを選んで表示する")
    p_plan.add_argument("--repo", required=True, help="リポジトリ名 (例: vaultwarden)")
    p_plan.add_argument("--date", default=None, help="YYYY-MM-DD (UTC)。既定は今日")
    p_plan.add_argument("--cycle", type=int, default=DEFAULT_CYCLE_MONTHS)
    args = parser.parse_args(argv)

    result = plan(args.repo, date=args.date, cycle=args.cycle)
    for key in ("repo", "date", "cycle", "offset", "slot", "subset"):
        print("{}={}".format(key.upper(), result[key]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
