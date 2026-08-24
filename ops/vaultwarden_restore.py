#!/usr/bin/env python3
"""vaultwarden の restic snapshot を scratch PVC へ復元し、金庫が生きて返ることを実測する (P-9025)。

なぜ在るか (P-9025):
  人間のパスワード金庫 vaultwarden (sqlite) は日次 restic backup がある一方、「バックアップから
  実際に戻してログイン画面が立ち上がる」実績が一度も無かった (archive に復元系 0 件。T-0117 /
  P-0208 は coder home・syncthing で実証済み)。しかも backup CronJob は B2 の download cap 超過で
  実際に失敗した実績がある (P-0111)。「戻せるはず」を最重要データから証明し、docs/backup.md の
  復元手順を実測で裏付けるためのスクリプト。

  spec の verify はこのスクリプトの**実在**と **restore-summary.json の中身**を検査する
  (integrity=='ok' かつ rows>0 かつ duration_seconds>0 かつ target が scratch)。

設計:
  - **本番 PVC に触れない**。復元先は使い捨ての scratch PVC (env SCRATCH_DIR) のみ。
  - **credential は backup CronJob と同じ `vaultwarden-restic-backup-credentials`
    (append-only 鍵) を参照する** (restic-external-secret.yaml)。restore は読み取り
    (readFiles) で完結し、削除権限つき鍵 `vaultwarden-restic-credentials` は持ち出さない
    (P-0341 の結論。retention の forget --prune だけが削除権限を要る)。
  - restic の実行は環境変数 (RESTIC_REPOSITORY / RESTIC_PASSWORD / B2_ACCOUNT_ID /
    B2_ACCOUNT_KEY) を前提にする。Pod からは secret の secretKeyRef で注入される。
  - restic restore は所有権・タイムスタンプの復元を伴うため CHOWN / FOWNER / DAC_OVERRIDE の
    3 capability が必要 (docs/backup.md の T-0071 / P-0047 の教訓)。backup 側の
    DAC_READ_SEARCH では足りない。再実行時は前回の残留を先に掃除する。
  - 標準ライブラリのみ。sqlite3 (integrity_check / 行数) と hashlib (sha256 照合) は
    python の標準ライブラリで完結する。
  - **fail-closed**: snapshot が無い / 24h より古い / integrity が ok でない / 行数 0 /
    チェックサム不一致 / restic が非 0 → 非 0 で終了し restore-summary.json に失敗理由を書く。

実行の形:
  このスクリプトは vaultwarden namespace の使い捨て Job (python:3.14-alpine + restic バイナリを
  initContainer から emptyDir へコピー) の中で動くことを想定する。restic バイナリの場所は env
  RESTIC_BINARY で渡す (既定は PATH 上の restic)。restore-summary.json は SUMMARY_PATH
  (既定 SCRATCH_DIR/.restore-summary.json) に書き、同時に stdout にも JSON 1 行を出す。

終了コード:
  | rc | 意味                                    |
  |----|------------------------------------------|
  | 0  | 復元・検証・サマリ書き出しが全て成功      |
  | 1  | snapshot 不在 / 古い / 復元・検証失敗     |
  | 2  | 引数の誤り (argparse 既定)                |
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

# vaultwarden コンテナは runAsUser/runAsGroup 1000 で動く (apps/vaultwarden の deployment)。
# scratch PVC のルートをこの UID/GID に chown しないと、復元した /data を読んで書く
# vaultwarden プロセスが権限エラーで落ちる。
VW_UID = 1000
VW_GID = 1000

# B2 の download cap は毎日 00:00 UTC にリセットされる (P-0111)。backup は毎日 03:40 JST
# (= 前日 18:40 UTC) に走る。このしきい値は DoD (1)「直近 24h 以内に snapshot が無ければ
# backup CronJob を 1 回起動して実在を先に確定する」の「24h」。
DEFAULT_MAX_SNAPSHOT_AGE_HOURS = 24.0

# restore-summary.json に書く必須キー。verify が integrity/rows/duration_seconds/target(scratch) を
# 実測で検査する。
SUMMARY_KEYS = [
    "integrity",
    "rows",
    "users",
    "duration_seconds",
    "transferred_bytes",
    "files_restored",
    "target",
    "snapshot_id",
    "snapshot_time",
    "checksum_match",
    "restored_at",
]


def run_restic(binary, args, *, check=True):
    """restic を実行して (rc, stdout, stderr) を返す。check=True なら非 0 で CalledProcessError。"""
    env = os.environ.copy()
    proc = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, [binary, *args], proc.stdout, proc.stderr
        )
    return proc.returncode, proc.stdout, proc.stderr


def load_snapshots(binary):
    """restic snapshots --json を読んで snapshot のリストを返す。読めなければ例外。"""
    _, out, err = run_restic(binary, ["snapshots", "--json"])
    try:
        snaps = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"restic snapshots --json を JSON として読めない: {exc}\nstderr={err}") from exc
    if not isinstance(snaps, list):
        raise RuntimeError(f"restic snapshots --json のトップレベルが配列でない: {out[:200]!r}")
    return snaps


def pick_latest(snapshots):
    """time が最新の snapshot を選ぶ。空なら例外。"""
    if not snapshots:
        raise RuntimeError("restic snapshots が空 — リポジトリに snapshot が無い (backup CronJob を先に 1 回回すこと)")
    return max(snapshots, key=lambda s: s.get("time", ""))


def snapshot_age_seconds(snapshot, now):
    """snapshot の time と現在時刻の差 (秒)。time を読めなければ失敗扱いにする (fail-closed)。"""
    raw = snapshot.get("time")
    if not raw:
        raise RuntimeError(f"snapshot {snapshot.get('id')} に time が無い")
    dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int((now - dt).total_seconds())


def clean_scratch(scratch):
    """scratch ルートの中身を空にする (前回実行の残留が権限エラーの原因になる、docs/backup.md の教訓)。"""
    os.makedirs(scratch, exist_ok=True)
    for name in os.listdir(scratch):
        p = os.path.join(scratch, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


def restore_snapshot(binary, snapshot_id, scratch):
    """最新 snapshot を scratch へ復元し、所要時間と転送量を返す。"""
    start = time.monotonic()
    rc, out, err = run_restic(binary, ["restore", snapshot_id, "--target", scratch], check=False)
    elapsed = int(time.monotonic() - start)
    if rc != 0:
        raise RuntimeError(f"restic restore が rc={rc} で失敗:\n{out}\n{err}")

    # "Summary: Restored 12 files/dirs (1.748 MiB) in 0:05" の (サイズ) をパースする
    size_match = re.search(r"\(([\d.]+)\s*(B|KiB|MiB|GiB)\)", out)
    transferred_bytes = None
    if size_match:
        value = float(size_match.group(1))
        unit = size_match.group(2)
        scale = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[unit]
        transferred_bytes = int(value * scale)
    files_match = re.search(r"Restored (\d+) files/dirs", out)
    files_restored = int(files_match.group(1)) if files_match else None
    return elapsed, transferred_bytes, files_restored


def assemble_data(scratch):
    """restic の 2 パス復元結果を vaultwarden の /data レイアウトに組み立てる。

    backup は「/mnt/vaultwarden-data (db.sqlite3系・icon_cache を除く本体) + /staging/db.sqlite3
    (Online Backup API の一貫コピー)」の 2 パスを 1 snapshot にしている
    (apps/vaultwarden/restic-backup-cronjob.yaml)。restic は絶対パスのまま復元するため、
    restore 後は SCRATCH_DIR/mnt/vaultwarden-data/... と SCRATCH_DIR/staging/db.sqlite3 に分かれて
    現れる。これを /data 相当 (SCRATCH_DIR 直下) へ移動し、空になった mnt/ staging/ を消す。
    stale な WAL/-shm は backup 側で除かれているので持ち込まない (docs/backup.md の復元時注意)。
    """
    data_src = os.path.join(scratch, "mnt", "vaultwarden-data")
    db_src = os.path.join(scratch, "staging", "db.sqlite3")
    if not os.path.isdir(data_src):
        raise RuntimeError(f"復元結果に {data_src} が無い (snapshot の構成が想定と異なる)")
    if not os.path.isfile(db_src):
        raise RuntimeError(f"復元結果に {db_src} が無い (snapshot の構成が想定と異なる)")

    for name in os.listdir(data_src):
        src = os.path.join(data_src, name)
        dst = os.path.join(scratch, name)
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.move(src, dst)
        else:
            shutil.move(src, dst)
    shutil.move(db_src, os.path.join(scratch, "db.sqlite3"))
    shutil.rmtree(os.path.join(scratch, "mnt"), ignore_errors=True)
    shutil.rmtree(os.path.join(scratch, "staging"), ignore_errors=True)

    # scratch PVC のルートを vaultwarden の UID/GID に揃える (prod /data と同じ所有関係)。
    # chown は root (CHOWN capability) でしかできない。Pod 内は root で走るので適用され、
    # 非 root の実行 (単体テスト等) ではスキップする。
    if os.geteuid() == 0:
        os.chown(scratch, VW_UID, VW_GID)
        os.chmod(scratch, 0o700)
    return os.path.join(scratch, "db.sqlite3")


def verify_sqlite(db_path):
    """PRAGMA integrity_check と主要テーブルの行数を返す。

    integrity は 'ok' でなければ失敗。rows は sqlite_* を除く全テーブルの行数の合計。
    users は users テーブルが在ればその行数 (人間のアカウントが生きていることの確認)。
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        rows = 0
        table_rows = {}
        for t in tables:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            table_rows[t] = n
            rows += n
        users = table_rows.get("users", 0)
        return integrity, rows, users, table_rows
    finally:
        con.close()


def checksum_compare(binary, snapshot_id, db_path, db_in_repo):
    """復元した db.sqlite3 の sha256 と restic dump (snapshot 内の /staging/db.sqlite3) を照合する。

    db_in_repo は snapshot 内のパス (vaultwarden は /staging/db.sqlite3)。一致すれば True。
    """
    def sha256_of_file(path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def sha256_of_dump():
        env = os.environ.copy()
        proc = subprocess.run(
            [binary, "dump", snapshot_id, db_in_repo],
            capture_output=True,
            env=env,
            timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"restic dump {snapshot_id} {db_in_repo} が rc={proc.returncode} で失敗:\n{proc.stderr[-2000:]}"
            )
        return hashlib.sha256(proc.stdout).hexdigest()

    local = sha256_of_file(db_path)
    remote = sha256_of_dump()
    return local == remote, local, remote


def write_summary(summary, path):
    """restore-summary.json を書く。target は必ず 'scratch' を含める (verify の grep が検査)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="vaultwarden の restic snapshot を scratch PVC へ復元して検証し、restore-summary.json を書く (P-9025)"
    )
    ap.add_argument("--scratch-dir", default=os.environ.get("SCRATCH_DIR", "/scratch"),
                    help="復元先の scratch ディレクトリ (既定 $SCRATCH_DIR か /scratch)")
    ap.add_argument("--summary-path", default=os.environ.get("SUMMARY_PATH", None),
                    help="restore-summary.json の出力先 (既定 $SUMMARY_PATH か SCRATCH_DIR/.restore-summary.json)")
    ap.add_argument("--restic-binary", default=os.environ.get("RESTIC_BINARY", "restic"),
                    help="restic バイナリのパス (既定 $RESTIC_BINARY か PATH の restic)")
    ap.add_argument("--max-snapshot-age-hours", type=float,
                    default=os.environ.get("SNAPSHOT_MAX_AGE_HOURS", DEFAULT_MAX_SNAPSHOT_AGE_HOURS),
                    help=f"snapshot の許容最大経過時間 (時)。既定 {DEFAULT_MAX_SNAPSHOT_AGE_HOURS}")
    ap.add_argument("--db-in-repo", default="/staging/db.sqlite3",
                    help="snapshot 内の db.sqlite3 のパス (restic dump の引数)。既定 /staging/db.sqlite3")
    ap.add_argument("--now", default=None, help="現在時刻 (ISO8601)。試験用")
    args = ap.parse_args(argv)

    scratch = args.scratch_dir
    summary_path = args.summary_path or os.path.join(scratch, ".restore-summary.json")
    binary = args.restic_binary
    now = (
        datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.datetime.now(datetime.timezone.utc)
    )

    summary = {
        "integrity": None,
        "rows": None,
        "users": None,
        "duration_seconds": None,
        "transferred_bytes": None,
        "files_restored": None,
        "target": "scratch",
        "snapshot_id": None,
        "snapshot_time": None,
        "checksum_match": None,
        "restored_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    try:
        if not os.environ.get("RESTIC_REPOSITORY"):
            raise RuntimeError("RESTIC_REPOSITORY が未設定 (secret の注入を確認)")

        snaps = load_snapshots(binary)
        snap = pick_latest(snaps)
        summary["snapshot_id"] = snap.get("short_id") or snap.get("id")
        summary["snapshot_time"] = snap.get("time")

        age = snapshot_age_seconds(snap, now)
        if age > args.max_snapshot_age_hours * 3600:
            raise RuntimeError(
                f"最新 snapshot ({summary['snapshot_id']}, {summary['snapshot_time']}) が {age} 秒前で "
                f"直近 {args.max_snapshot_age_hours:.0f}h を超えている — backup CronJob を 1 回起動して "
                f"snapshot の実在を先に確定すること (DoD 1)"
            )

        clean_scratch(scratch)
        elapsed, transferred, files = restore_snapshot(binary, snap["id"], scratch)
        summary["duration_seconds"] = elapsed
        summary["transferred_bytes"] = transferred
        summary["files_restored"] = files

        db_path = assemble_data(scratch)

        integrity, rows, users, table_rows = verify_sqlite(db_path)
        summary["integrity"] = integrity
        summary["rows"] = rows
        summary["users"] = users
        if integrity != "ok":
            raise RuntimeError(f"PRAGMA integrity_check が 'ok' でない: {integrity!r} (テーブル行数 {rows})")
        if rows <= 0:
            raise RuntimeError("復元した db.sqlite3 の行数が 0 (空の金庫)")

        match, local, remote = checksum_compare(binary, snap["id"], db_path, args.db_in_repo)
        summary["checksum_match"] = match
        if not match:
            raise RuntimeError(f"sha256 照合が不一致 — 復元 {local} / snapshot 内 {remote}")

        write_summary(summary, summary_path)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    except Exception as exc:
        summary["error"] = str(exc)
        try:
            write_summary(summary, summary_path)
        except Exception as write_exc:
            print(f"warning: restore-summary.json を書けなかった: {write_exc}", file=sys.stderr)
        print(json.dumps(summary, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())