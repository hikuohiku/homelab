#!/usr/bin/env python3
"""immich の restic snapshot を scratch に丸ごと復元し、写真と DB が生きて返ることを実測する (P-9047)。

なぜ在るか (P-9047):
  最大のデータ保持者 immich は P-0005 の初期棄却以来一度も復元されていない。
  backup は 5 本の restic CronJob が取っているが「戻せるか」は誰も知らないままで、
  08-22 の B2 download cap 超過で backup 子 Job が落ち全アプリ Degraded になった実測
  (P-0111) が「取れているだけの backup」の信用を揺るがした。この drill は、最新 snapshot
  (DB ダンプ + uploads の両方) を scratch namespace に復元し、(a) postgres が起動して
  vchord が生き (b) 復元 DB の写真数 (asset 行数) が本番の実測と一致し (c) 復元された
  immich-server の API が 200 を返す、までを実測する。P-0291 (immich postgres 16.14) の
  安全網でもある。

  spec (P-9047) の verify はこのスクリプトの**実在と --check の rc=0**、ConfigMap
  `autopilot/immich-restore-drill-report` の `photo_count` が数字であること、
  `--verify-freshness --max-age 3d` が成功記録の古さを検査することを見る。

設計:
  - **本番 PVC には触れない**。復元先は使い捨ての scratch PVC (env SCRATCH_DIR、既定
    /scratch) のみ。snapshot の直近性を先に確認し、古ければ fail-closed で止まる。
  - **credential は backup CronJob と同じ `immich-restic-backup-credentials`
    (append-only 鍵) を参照する** (apps/immich/restic-external-secret.yaml)。restore は
    読み取り (readFiles) で完結し、削除権限つき鍵 `immich-restic-credentials` は持ち出さない
    (P-0341 の結論。P-9025/P-0047 で実証済み)。
  - **実行の形**: immich namespace の使い捨て Job。1 Pod に initContainer 2 本
    (restic バイナリの空き emptyDir へのコピー / postgres の initdb+ext ブートストラップ) と
    コンテナ 4 個 (driver / postgres / valkey / immich-server) を同居させる。
    driver は vectorchord イメージ (python3 + psql + pg_isready を持つ) を root で動かし、
    restic restore の CHOWN/FOWNER/DAC_OVERRIDE 3 capability を付ける
    (docs/backup.md の T-0071 の教訓)。再実行時は scratch を先に掃除する。
  - **順序の保証**: immich-server は起動コマンドをラップし、driver が dump を psql で
    流し終えてから (/work/load-done マーカー) 起動する。空 DB に対して immich-server 自身の
    migration が走ってから dump を流す順序の衝突を避ける。
  - **dump のバージョン整合**: 内蔵ダンプのファイル名 `immich-db-backup-{ts}-v{server}-pg{pg}`
    の `pg` 部分と scratch postgres の `SHOW server_version` を突き合わせ、不一致なら
    fail-closed (docs/backup.md の注意)。
  - **記録**: 成功/失敗に関わらず report JSON を WORK_DIR/report.json と stdout に出す
    (stdout の最終行は `REPORT: {json}`)。ConfigMap `immich-restore-drill-report`
    (autopilot ns) への書き込みは `--publish` モードが行う (Job の SA には autopilot ns への
    書き込み権限が無いため、wrapper/人間が kubectl を持つ場所から publish する)。
  - 標準ライブラリのみ。gzip (dump 展開) と urllib (API probe) で完結する。

終了コード:
  | rc | 意味                                                |
  |----|-----------------------------------------------------|
  | 0  | drill 成功 / --check 成功 / --verify-freshness 最新  |
  | 1  | snapshot 不在・古い / 復元・検証失敗 / 記録が古い    |
  | 2  | 引数の誤り (argparse 既定)                           |
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# backup CronJob (apps/immich/restic-backup-cronjob.yaml) は `b2:<bucket>:immich` を
# リポジトリにし、`/mnt/immich-library` (immich-library PVC のライブラリルート) を丸ごと
# backup する。immich 内蔵の日次 DB ダンプが `UPLOAD_LOCATION/backups/*.sql.gz` に落ちる
# ため、snapshot 1 本に DB ダンプと uploads が両方入る (docs/backup.md)。
RESTIC_REPO_PATH = "immich"
LIBRARY_SUBPATH = ("mnt", "immich-library")
BACKUPS_DIR = "backups"

# 日次 backup は 02:45 JST (前日 17:45 UTC) に走る。このしきい値は P-9025 と同じ「直近 24h」。
# 超えていたら「backup CronJob を回して snapshot の実在を先に確定せよ」と fail-closed で止まる。
DEFAULT_MAX_SNAPSHOT_AGE_HOURS = 24.0

# 成功記録 ConfigMap。verify が kubectl でこの namespace のこの ConfigMap を読む。
CONFIGMAP_NAME = "immich-restore-drill-report"
CONFIGMAP_NS = "autopilot"

# 復元先 postgres (scratch) と API probe。単一 Pod 同居時は localhost、別 Deployment 構成では
# env DRILL_DB_HOST で Service 名を渡す (--skip-probe 構成)。
DB_HOST = os.environ.get("DRILL_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DRILL_DB_PORT", "5432"))
DB_USER = os.environ.get("DRILL_DB_USER", "immich")
DB_NAME = os.environ.get("DRILL_DB_NAME", "immich")
API_URL = os.environ.get("DRILL_API_URL", "http://127.0.0.1:2283/api/server/ping")
# 復元する immich server バージョン (dump ファイル名の -v{server} と突き合わせる。docs/backup.md)。
DEFAULT_EXPECTED_SERVER_VERSION = "3.1.0"

# report に必ず含めるキー。--publish が ConfigMap へ写すものと、verify が読むもの。
REPORT_KEYS = [
    "restored_at",
    "snapshot_id",
    "snapshot_time",
    "photo_count",
    "asset_table",
    "postgres_ok",
    "postgres_version",
    "vchord_ok",
    "vchord_version",
    "api_status",
    "api_url",
    "duration_seconds",
    "transferred_bytes",
    "files_restored",
    "dump_file",
    "expected_photo_count",
    "photo_count_matches",
    "target",
    "namespace",
]


def run_cmd(argv, *, check=True, stdin_data=None, timeout=900, env=None):
    """コマンドを実行して (rc, stdout, stderr) を返す。check=True なら非 0 で例外。"""
    proc = subprocess.run(
        argv,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env or os.environ.copy(),
    )
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, argv, proc.stdout, proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def load_snapshots(restic_binary):
    """restic snapshots --json を読んで snapshot のリストを返す。読めなければ例外。"""
    _, out, err = run_cmd([restic_binary, "snapshots", "--json"])
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
        raise RuntimeError(
            "restic snapshots が空 — リポジトリに snapshot が無い (backup CronJob を先に 1 回回すこと)"
        )
    return max(snapshots, key=lambda s: s.get("time", ""))


def snapshot_age_seconds(snapshot, now):
    """snapshot の time と現在時刻の差 (秒)。time を読めなければ失敗扱い (fail-closed)。"""
    raw = snapshot.get("time")
    if not raw:
        raise RuntimeError(f"snapshot {snapshot.get('id')} に time が無い")
    dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int((now - dt).total_seconds())


def clean_scratch(scratch):
    """scratch ルートの中身を空にする (前回実行の残留が権限エラーの原因になる、T-0071 の教訓)。"""
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


def restore_snapshot(restic_binary, snapshot, scratch):
    """最新 snapshot を scratch へ復元し、所要時間と転送量・ファイル数を返す。"""
    start = time.monotonic()
    rc, out, err = run_cmd(
        [restic_binary, "restore", snapshot["id"], "--target", scratch], check=False
    )
    elapsed = int(time.monotonic() - start)
    if rc != 0:
        raise RuntimeError(f"restic restore が rc={rc} で失敗:\n{out}\n{err}")

    # "Summary: Restored 82 files/dirs (340.715 MiB) in 0:16" の (サイズ) とファイル数をパース
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


def library_root(scratch):
    """restore 後のライブラリルートを返す。restic は絶対パスのまま復元するため。"""
    return os.path.join(scratch, *LIBRARY_SUBPATH)


def find_latest_dump(scratch):
    """復元したライブラリの backups/ から最新の .sql.gz を選ぶ。

    ファイル名は `immich-db-backup-{YYYYMMDD}T{HHMMSS}-v{server}-pg{pg}.sql.gz`。時系列順の
    ファイル名の辞書順がそのまま時刻順になる (UTC 02:00 実行、T-0125 実測)。
    """
    backups = os.path.join(library_root(scratch), BACKUPS_DIR)
    if not os.path.isdir(backups):
        raise RuntimeError(f"復元結果に backups/ が無い: {backups} (snapshot の構成が想定と異なる)")
    dumps = sorted(p for p in os.listdir(backups) if p.endswith(".sql.gz") and not p.startswith("."))
    if not dumps:
        raise RuntimeError(f"backups/ に .sql.gz が無い: {backups} (内蔵 DB ダンプが落ちていない)")
    return os.path.join(backups, dumps[-1])


DUMP_VERSION_RE = re.compile(r"-v(?P<server>\d+\.\d+\.\d+)-pg(?P<pg>\d+(?:\.\d+)?)\.sql\.gz$")


def parse_dump_version(filename):
    """ダンプファイル名から (server, pg) を抜く。形式に合わなければ (None, None)。"""
    m = DUMP_VERSION_RE.search(os.path.basename(filename))
    if not m:
        return None, None
    return m.group("server"), m.group("pg")


def wait_for_postgres(timeout=180, sleep=2):
    """pg_isready で scratch postgres の起動を待つ。タイムアウトで例外。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, _, _ = run_cmd(["pg_isready", "-h", DB_HOST, "-p", str(DB_PORT), "-U", DB_USER], check=False)
        if rc == 0:
            return
        time.sleep(sleep)
    raise RuntimeError(f"scratch postgres が {timeout}s 以内に ready にならない (pg_isready)")


def psql_query(sql):
    """psql で 1 つの値を取る。行はトリムして返す。失敗なら例外。"""
    _, out, err = run_cmd(
        ["psql", "-X", "-h", DB_HOST, "-p", str(DB_PORT), "-U", DB_USER, "-d", DB_NAME,
         "-tA", "-c", sql],
        check=False,
    )
    if not out.strip() and err.strip():
        raise RuntimeError(f"psql が失敗: {err.strip()}")
    return out.strip()


def load_dump(dump_path, postgres_version, expected_server_version):
    """gunzip しながら psql に流し込む。psql の失敗は例外で止める (fail-closed)。

    psql に stdin で流すため -tA は使わない。ON_ERROR_STOP で最初のエラーで止める。
    ダンプファイル名の -v{server}-pg{pg} と、scratch postgres の実測バージョン・
    expected_server_version を突き合わせ、不一致なら中止する (docs/backup.md の注意)。
    """
    server, pg = parse_dump_version(dump_path)
    pg_major = postgres_version.split()[0] if postgres_version else ""
    if pg is None or pg != pg_major:
        raise RuntimeError(
            f"ダンプの pg バージョン ({pg!r}) が scratch postgres の {pg_major!r} と不一致 "
            f"— ダンプ: {os.path.basename(dump_path)}。復元を中止する (fail-closed)"
        )
    if server != expected_server_version:
        raise RuntimeError(
            f"ダンプの immich バージョン ({server!r}) が期待値 {expected_server_version!r} と不一致 "
            f"— ダンプ: {os.path.basename(dump_path)}。復元を中止する (fail-closed)"
        )
    proc = subprocess.Popen(
        ["psql", "-X", "-h", DB_HOST, "-p", str(DB_PORT), "-U", DB_USER, "-d", DB_NAME,
         "-v", "ON_ERROR_STOP=1"],
        stdin=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    assert proc.stdin is not None
    with gzip.open(dump_path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            proc.stdin.write(chunk.decode("utf-8", errors="replace"))
    proc.stdin.close()
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"psql によるダンプ投入が rc={rc} で失敗 ({os.path.basename(dump_path)})")


def probe_api(url=None, timeout=240, sleep=3):
    """immich-server の API が 200 を返すまで待つ。返ったら HTTP ステータスを返す。"""
    target = url or API_URL
    deadline = time.monotonic() + timeout
    last_status = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(target, timeout=10) as resp:
                last_status = resp.status
                if resp.status == 200:
                    return 200
        except Exception as exc:  # noqa: BLE001 — probe はどんな失敗も待ち続ける
            last_error = str(exc)
        time.sleep(sleep)
    detail = f"last_status={last_status}" if last_status is not None else f"last_error={last_error}"
    raise RuntimeError(f"immich-server API が {timeout}s 以内に 200 を返さない ({detail})")


def compose_report(snapshot, photo_count, expected_photo_count, postgres_version, vchord_version,
                   api_status, duration_seconds, transferred_bytes, files_restored, dump_file, now):
    """成功 report を作る。target は必ず 'scratch' を含める (verify と spec の検査)。"""
    return {
        "restored_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot_id": snapshot.get("short_id") or snapshot.get("id"),
        "snapshot_time": snapshot.get("time"),
        "photo_count": photo_count,
        "asset_table": "asset",
        "postgres_ok": True,
        "postgres_version": postgres_version,
        "vchord_ok": bool(vchord_version),
        "vchord_version": vchord_version,
        "api_status": api_status,
        "api_url": API_URL,
        "duration_seconds": duration_seconds,
        "transferred_bytes": transferred_bytes,
        "files_restored": files_restored,
        "dump_file": os.path.basename(dump_file) if dump_file else None,
        "expected_photo_count": expected_photo_count,
        "photo_count_matches": photo_count == expected_photo_count,
        "target": "scratch",
        "namespace": os.environ.get("DRILL_NAMESPACE", "immich"),
    }


def write_report(report, work_dir):
    """report を WORK_DIR/report.json と stdout (最終行 REPORT: {...}) に出す。"""
    os.makedirs(work_dir, exist_ok=True)
    report_path = os.path.join(work_dir, "report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("REPORT: " + json.dumps(report, ensure_ascii=False))


def run_drill(args, now) -> int:
    """drill 本体。成功で 0、失敗で 1 (report には error を書く)。"""
    scratch = args.scratch_dir
    work_dir = args.work_dir
    restic_binary = args.restic_binary
    expected_photo_count = args.expected_photo_count

    report = {
        "restored_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot_id": None,
        "snapshot_time": None,
        "photo_count": None,
        "asset_table": "asset",
        "postgres_ok": None,
        "postgres_version": None,
        "vchord_ok": None,
        "vchord_version": None,
        "api_status": None,
        "api_url": API_URL,
        "duration_seconds": None,
        "transferred_bytes": None,
        "files_restored": None,
        "dump_file": None,
        "expected_photo_count": expected_photo_count,
        "photo_count_matches": None,
        "target": "scratch",
        "namespace": os.environ.get("DRILL_NAMESPACE", "immich"),
    }

    try:
        if not os.environ.get("RESTIC_REPOSITORY"):
            raise RuntimeError("RESTIC_REPOSITORY が未設定 (secret の注入を確認)")

        start = time.monotonic()

        snaps = load_snapshots(restic_binary)
        snap = pick_latest(snaps)
        report["snapshot_id"] = snap.get("short_id") or snap.get("id")
        report["snapshot_time"] = snap.get("time")

        age = snapshot_age_seconds(snap, now)
        if age > args.max_snapshot_age_hours * 3600:
            raise RuntimeError(
                f"最新 snapshot ({report['snapshot_id']}, {report['snapshot_time']}) が {age} 秒前で "
                f"直近 {args.max_snapshot_age_hours:.0f}h を超えている — backup CronJob を 1 回起動して "
                f"snapshot の実在を先に確定すること"
            )

        clean_scratch(scratch)
        elapsed, transferred, files = restore_snapshot(restic_binary, snap, scratch)
        report["duration_seconds"] = elapsed
        report["transferred_bytes"] = transferred
        report["files_restored"] = files

        dump_path = find_latest_dump(scratch)
        report["dump_file"] = os.path.basename(dump_path)

        # scratch postgres (単一 Pod 同居なら localhost、別 Deployment なら DRILL_DB_HOST) を待ち、
        # バージョン突き合わせ → 投入 → 検証
        wait_for_postgres()
        postgres_version = psql_query("SHOW server_version;")
        report["postgres_version"] = postgres_version
        if not postgres_version.split()[0].startswith("16."):
            raise RuntimeError(f"scratch postgres のバージョンが想定外: {postgres_version!r} (16 系を期待)")

        load_dump(dump_path, postgres_version, args.expected_server_version)

        vchord_version = psql_query("SELECT extversion FROM pg_extension WHERE extname='vchord';")
        report["vchord_version"] = vchord_version
        report["vchord_ok"] = bool(vchord_version)
        if not vchord_version:
            raise RuntimeError("復元 DB に vchord 拡張が無い (extension が生きていない)")
        # vchord が実際に使えること: ベクトル検索テーブルの行数を数えられれば type/演算子が生きている
        psql_query("SELECT COUNT(*) FROM smart_search;")

        photo_count_raw = psql_query("SELECT COUNT(*) FROM asset;")
        try:
            photo_count = int(photo_count_raw)
        except ValueError as exc:
            raise RuntimeError(f"asset 行数が数字でない: {photo_count_raw!r}") from exc
        report["photo_count"] = photo_count
        report["postgres_ok"] = True

        if expected_photo_count is not None and photo_count != expected_photo_count:
            raise RuntimeError(
                f"復元 DB の asset 行数 {photo_count} が本番の実測 {expected_photo_count} と不一致"
            )
        report["photo_count_matches"] = photo_count == expected_photo_count

        # immich-server は単一 Pod 同居時は load-done を待って起動し、--skip-probe でない
        # ときだけ API が 200 を返すまで待つ。別 Deployment 構成 (--skip-probe) では
        # サーバは後段の Job/Deployment が立て、probe は --probe モードか wrapper が行う。
        if not args.skip_probe:
            os.makedirs(work_dir, exist_ok=True)
            open(os.path.join(work_dir, "load-done"), "w").close()
            api_status = probe_api()
            report["api_status"] = api_status

        report["duration_seconds"] = int(time.monotonic() - start)
        write_report(report, work_dir)
        return 0

    except Exception as exc:
        report["error"] = str(exc)
        try:
            write_report(report, work_dir)
        except Exception as write_exc:
            print(f"warning: report を書けなかった: {write_exc}", file=sys.stderr)
        return 1


def parse_max_age(text):
    """'3d' / '12h' / '90m' / '3600s' を秒に。素の数字は日として扱う。"""
    text = text.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)([dhms]?)$", text)
    if not m:
        raise ValueError(f"max-age を解釈できない: {text!r} ('3d' / '12h' / '90m' / '3600s')")
    value = float(m.group(1))
    unit = m.group(2) or "d"
    return int(value * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit])


def cmd_probe(args) -> int:
    """--api-url (既定 DRILL_API_URL か 127.0.0.1:2283) が 200 を返すか実測し、JSON を stdout に出す。

    別 Deployment 構成 (--skip-probe で driver を回した後) の API 確認に使う。
    kubectl もクラスタ内ネットワークも、スクリプト自身が持つ必要はない。
    """
    url = args.api_url or API_URL
    try:
        status = probe_api(url=url, timeout=args.probe_timeout)
    except RuntimeError as exc:
        print(json.dumps({"api_status": None, "api_url": url, "error": str(exc)}))
        return 1
    print(json.dumps({"api_status": status, "api_url": url}))
    return 0


def cmd_verify_freshness(args) -> int:
    """ConfigMap の restored_at が max-age 以内かを kubectl で実測する。"""
    max_age_seconds = parse_max_age(args.max_age)
    _, out, err = run_cmd(
        ["kubectl", "get", "configmap", CONFIGMAP_NAME, "-n", CONFIGMAP_NS, "-o", "json"],
        check=False,
    )
    if not out.strip():
        print(f"ConfigMap {CONFIGMAP_NS}/{CONFIGMAP_NAME} が無い: {err.strip()}", file=sys.stderr)
        return 1
    try:
        doc = json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"ConfigMap を JSON として読めない: {exc}", file=sys.stderr)
        return 1
    restored_at = (doc.get("data") or {}).get("restored_at")
    if not restored_at:
        print(f"ConfigMap に restored_at が無い (data={doc.get('data')})", file=sys.stderr)
        return 1
    try:
        dt = datetime.datetime.fromisoformat(restored_at.replace("Z", "+00:00"))
    except ValueError as exc:
        print(f"restored_at を日付として解釈できない: {restored_at!r} ({exc})", file=sys.stderr)
        return 1
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    age = datetime.datetime.now(datetime.timezone.utc) - dt
    if age.total_seconds() > max_age_seconds:
        print(
            f"最後の drill は {age.days}d {age.seconds // 3600}h 前 (restored_at={restored_at})。"
            f"max-age {args.max_age} を超えている",
            file=sys.stderr,
        )
        return 1
    print(f"ok: 最後の drill は {age.days}d {age.seconds // 3600}h 前 (restored_at={restored_at})")
    return 0


def cmd_publish(args) -> int:
    """report JSON から ConfigMap immich-restore-drill-report を kubectl で書く (upsert)。"""
    if args.report:
        with open(args.report, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    else:
        report = json.load(sys.stdin)
    if report.get("api_status") != 200 or not report.get("photo_count_matches"):
        print("report が成功を表していない (api_status=200 かつ photo_count_matches=true が必要)", file=sys.stderr)
        return 1

    data = {
        "restored_at": str(report.get("restored_at", "")),
        "snapshot_id": str(report.get("snapshot_id", "")),
        "photo_count": str(report.get("photo_count", "")),
        "duration_seconds": str(report.get("duration_seconds", "")),
        "postgres_ok": str(bool(report.get("postgres_ok"))).lower(),
        "postgres_version": str(report.get("postgres_version", "")),
        "vchord_ok": str(bool(report.get("vchord_ok"))).lower(),
        "vchord_version": str(report.get("vchord_version", "")),
        "api_status": str(report.get("api_status", "")),
        "api_url": str(report.get("api_url", "")),
        "photo_count_matches": str(bool(report.get("photo_count_matches"))).lower(),
        "target": str(report.get("target", "")),
    }
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": CONFIGMAP_NAME, "namespace": CONFIGMAP_NS},
        "data": data,
    }
    rc, out, err = run_cmd(
        ["kubectl", "apply", "-f", "-"], stdin_data=json.dumps(manifest), check=False
    )
    if rc != 0:
        print(f"kubectl apply が rc={rc} で失敗:\n{out}\n{err}", file=sys.stderr)
        return 1
    print(out.strip())
    return 0


def cmd_check() -> int:
    """自己検査 (verify の --check)。ネットワークもクラスタも要らない。rc=0 で green。"""
    checks = []

    # 必須モジュール・関数が import されていること (この時点で到達できていれば成立)
    for name in ["run_cmd", "load_snapshots", "pick_latest", "snapshot_age_seconds",
                 "restore_snapshot", "find_latest_dump", "parse_dump_version",
                 "load_dump", "probe_api", "compose_report", "cmd_verify_freshness", "cmd_publish", "cmd_probe"]:
        checks.append((name, name in globals()))

    # 純粋関数の契約: 最新 snapshot 選択
    now = datetime.datetime(2026, 8, 25, 0, 0, 0, tzinfo=datetime.timezone.utc)
    snaps = [
        {"id": "a" + "0" * 63, "time": "2026-08-24T17:45:00Z"},
        {"id": "b" + "0" * 63, "time": "2026-08-23T00:00:00Z"},
    ]
    checks.append(("pick_latest", pick_latest(snaps)["id"].startswith("a")))
    age = snapshot_age_seconds(snaps[0], now)
    checks.append(("snapshot_age_seconds", 0 < age < 24 * 3600))
    try:
        snapshot_age_seconds({"id": "x"}, now)
        checks.append(("snapshot_age_seconds_missing_time_fails", False))
    except RuntimeError:
        checks.append(("snapshot_age_seconds_missing_time_fails", True))

    # ダンプファイル名のバージョン抽出
    server, pg = parse_dump_version("immich-db-backup-20260824T020000-v3.1.0-pg16.14.sql.gz")
    checks.append(("parse_dump_version_ok", (server, pg) == ("3.1.0", "16.14")))
    checks.append(("parse_dump_version_bad", parse_dump_version("not-a-dump.sql.gz") == (None, None)))

    # report schema: 必須キーが揃っていること
    report = compose_report(
        snaps[0], 19, 19, "16.14", "1.1.1", 200, 120, 373334885, 82,
        "immich-db-backup-20260824T020000-v3.1.0-pg16.14.sql.gz", now,
    )
    checks.append(("report_keys", all(k in report for k in REPORT_KEYS)))
    checks.append(("report_target_scratch", "scratch" in report["target"]))
    checks.append(("report_photo_count_numeric", isinstance(report["photo_count"], int)))
    checks.append(("report_matches", report["photo_count_matches"] is True))

    failed = [name for name, ok in checks if not ok]
    if failed:
        print("self-check failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"immich_restore_drill.py: self-check ok ({len(checks)} checks)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="immich の restic snapshot を scratch に復元して DB/uploads/API が生きて返ることを実測する drill (P-9047)"
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="自己検査 (verify 用)。クラスタ不要")
    mode.add_argument("--verify-freshness", action="store_true",
                      help=f"ConfigMap {CONFIGMAP_NS}/{CONFIGMAP_NAME} の restored_at が max-age 以内かを実測する")
    mode.add_argument("--publish", action="store_true",
                      help="report JSON (--report か stdin) を ConfigMap に書き込む (upsert)")
    mode.add_argument("--probe", action="store_true",
                      help="--api-url が 200 を返すか実測し JSON を stdout に出す (別 Deployment 構成の API 確認用)")

    ap.add_argument("--scratch-dir", default=os.environ.get("SCRATCH_DIR", "/scratch"),
                    help="復元先の scratch ディレクトリ (既定 $SCRATCH_DIR か /scratch)")
    ap.add_argument("--work-dir", default=os.environ.get("WORK_DIR", "/work"),
                    help="report / マーカーの置き場所 (既定 $WORK_DIR か /work)")
    ap.add_argument("--restic-binary", default=os.environ.get("RESTIC_BINARY", "/tools/restic"),
                    help="restic バイナリのパス (既定 $RESTIC_BINARY か /tools/restic)")
    ap.add_argument("--max-snapshot-age-hours", type=float,
                    default=os.environ.get("SNAPSHOT_MAX_AGE_HOURS", DEFAULT_MAX_SNAPSHOT_AGE_HOURS),
                    help=f"snapshot の許容最大経過時間 (時)。既定 {DEFAULT_MAX_SNAPSHOT_AGE_HOURS}")
    ap.add_argument("--expected-photo-count", type=int,
                    default=os.environ.get("EXPECTED_PHOTO_COUNT", None),
                    help="本番 DB の asset 行数 (実測値)。drill の照合基準")
    ap.add_argument("--expected-server-version",
                    default=os.environ.get("EXPECTED_SERVER_VERSION", DEFAULT_EXPECTED_SERVER_VERSION),
                    help=f"復元する immich server バージョン (dump の -v{{server}} と突き合わせ)。既定 {DEFAULT_EXPECTED_SERVER_VERSION}")
    ap.add_argument("--skip-probe", action="store_true",
                    help="immich-server の起動待ち/probe をスキップする (別 Deployment 構成。probe は --probe で行う)")
    ap.add_argument("--api-url", default=None,
                    help="--probe が実測する URL (既定 $DRILL_API_URL か http://127.0.0.1:2283/api/server/ping)")
    ap.add_argument("--probe-timeout", type=int, default=240,
                    help="--probe のタイムアウト秒 (既定 240)")
    ap.add_argument("--max-age", default=os.environ.get("DRILL_MAX_AGE", "3d"),
                    help="--verify-freshness の許容最大経過 (既定 3d。'3d'/'12h'/'90m'/'3600s')")
    ap.add_argument("--report", default=None, help="--publish が読む report JSON のパス (既定 stdin)")
    ap.add_argument("--now", default=None, help="現在時刻 (ISO8601)。試験用")
    args = ap.parse_args(argv)

    now = (
        datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.datetime.now(datetime.timezone.utc)
    )

    if args.check:
        return cmd_check()
    if args.verify_freshness:
        return cmd_verify_freshness(args)
    if args.publish:
        return cmd_publish(args)
    if args.probe:
        return cmd_probe(args)
    return run_drill(args, now)


if __name__ == "__main__":
    sys.exit(main())