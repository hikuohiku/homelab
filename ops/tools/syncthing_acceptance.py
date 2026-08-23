#!/usr/bin/env python3
"""syncthing 移行の受け入れ検査 (P-0163)。

クラスタで動く実サービスの中で唯一、実データが旧 LXC 101 に置き去りのままなのが
syncthing (seeds H5, backlog T-0140)。移行は人間にしか到達できない cert/config の
取り出しを待って止まっており、このツールは「取り出した後の受け入れ」を機械判定して
人の残作業を 1 コマンドまで削る。

サブコマンド:

  check     検査リストを実行して合格/不合格を exit code で返す (本体)。
              python3 ops/tools/syncthing_acceptance.py check \
                --data-dir /var/syncthing --strict
  exercise  合成データでの空回し演習 (DoD 2)。ダミー同期フォルダを 1 個登録し、
            書き込み → rescan → 読み戻し → restic 対象確認までを自動化する。
            本番データには触れない (触るのは専用フォルダ acceptance-dummy のみ)。

check の検査項目と必須/任意:

  必須 (1 つでも fail なら rc=1):
    identity-files         cert.pem / key.pem / config.xml の所在と読み出し
    device-id-format       cert.pem から device ID を導出 (sha256→base32→luhn32
                           検査数字→7 文字区切り) し正規形に一致すること
    config-parse           config.xml が解析できること
    self-device-declared   導出した device ID が config.xml の device 一覧に
                           含まれること (cert と config の取り違え検知)
    folder-paths           folder 定義が存在し、全 path が新 root 配下に
                           収まること (旧 LXC のパス残りを検知)
    pvc-rw                 データディレクトリへの書き込み→読み戻し
                           (所有権 1000:1000 問題の早期発見)
    restic-coverage        restic-backup-cronjob.yaml が /mnt/syncthing-data を
                          対象にし、除外が既知の安全な 2 つだけであること (静的検査)
  任意 (到達できないときは「不明」として表示し、単独では落とさない。
  沈黙して見せかけの緑を作らないのが原則 — version_watch.py 流儀):
    gui-health             /rest/noauth/health への疎通 (GUI 認証設定に依存しない)
    tailnet-sync           syncthing-sync Service の TCP 22000 疎通

  --strict を付けると「不明」も不合格になる。最終ゲートはクラスタ内
  (Service DNS が解け restic マニフェストも参照できる位置) での --strict 実行を
  想定しており、docs/syncthing-migration.md の手順ではそちらを使う。

exit code: 0=合格 / 1=不合格 (--strict では不明も含む) / 2=使い方エラー。

既知の死角 (伏せずに書き残る):
  - cert.pem/config.xml の所在は「PVC 直下」または「config/ 配下」の 2 通りを
    自動判別する (repo 内でも cronjob の exclude は config/index-v2 表記、
    backup コメントは裸の config.xml 表記で、実測済みの片側しか固定できない
    ため)。どちらでもない構成は identity-files の fail になる
  - device ID 導出は syncthing 本家 lib/protocol/deviceid.go (luhn.go) の
    アルゴリズムの移植で、本家テストのゴールデンベクトルで固定している
    (ops/tests/test_syncthing_acceptance.py)
  - tailnet-sync は TCP 接続が張れることしか見ない (同期プロトコルの応答は
    見ない。それ以上は syncthing 自身にやらせる方が早い)
  - exercise は稼働中 syncthing の設定を一時的に変える (専用フォルダの追加と
    削除のみ。終了時に常に削除を試みる)
  - restic-coverage はリポジトリ内のマニフェストを読む静的検査なので、
    リポジトリ無しで動かす (--restic-manifest 未指定の in-cluster Job 等) と
    「不明」になる。--strict の最終ゲートは --restic-manifest を渡すこと

検査ロジックの固定テスト: ops/tests/test_syncthing_acceptance.py
  python3 -m unittest ops.tests.test_syncthing_acceptance
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import posixpath
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESTIC_MANIFEST = ROOT / "apps" / "syncthing" / "restic-backup-cronjob.yaml"

DEFAULT_GUI_URL = "http://syncthing.syncthing.svc:8384"
DEFAULT_SYNC_ADDR = "syncthing-sync.syncthing.svc:22000"
DEFAULT_ROOT = "/var/syncthing"
DEFAULT_EXERCISE_FOLDER = "acceptance-dummy"
EXERCISE_MARKER = ".acceptance-marker"

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

# restic backup が除外してよいもの。これ以外の --exclude が増えていたら
# 「同期フォルダがバックアップから漏れている」可能性があるので落とす。
# 実値は apps/syncthing/restic-backup-cronjob.yaml の実測 (P-0047)。
SAFE_RESTIC_EXCLUDES = {
    "/mnt/syncthing-data/config/index-v2",
    "/mnt/syncthing-data/config/syncthing.lock",
}
RESTIC_BACKUP_TARGET = "/mnt/syncthing-data"


def make_result(name, required, status, detail):
    return {"name": name, "required": bool(required), "status": status,
            "detail": detail}


# ---------------------------------------------------------------------------
# device ID 導出 — syncthing 本家 lib/protocol/deviceid.go + luhn.go の移植。
# 本家のテスト (deviceid_test.go) のゴールデンベクトルで固定する。
# ---------------------------------------------------------------------------

LUHN_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _codepoint32(ch):
    b = ord(ch)
    if ord("A") <= b <= ord("Z"):
        return b - ord("A")
    if ord("2") <= b <= ord("7"):
        return b + 26 - ord("2")
    return -1


def luhn32(s):
    """13 文字の base32 断片に検査数字 1 文字を付ける (本家 luhn32 の移植)。

    「実際の Luhn アルゴリズムには従わない」のは本家コメントの通り。
    """
    factor = 1
    total = 0
    for ch in s:
        cp = _codepoint32(ch)
        if cp < 0:
            raise ValueError(f"digit {ch!r} not valid in alphabet {LUHN_ALPHABET}")
        addend = factor * cp
        factor = 1 if factor == 2 else 2
        addend = addend // 32 + addend % 32
        total += addend
    return LUHN_ALPHABET[(32 - total % 32) % 32]


def luhnify(core52):
    """52 文字の base32 に 4 個の検査数字を挟んで 56 文字にする。"""
    if len(core52) != 52:
        raise ValueError(f"unsupported string length {len(core52)} (expected 52)")
    out = []
    for i in range(4):
        chunk = core52[i * 13:(i + 1) * 13]
        out.append(chunk + luhn32(chunk))
    return "".join(out)


def unluhnify(styled56):
    """検査数字つき 56 文字を検証して 52 文字に戻す。壊れていれば ValueError。"""
    if len(styled56) != 56:
        raise ValueError(f"unsupported string length {len(styled56)} (expected 56)")
    core = []
    for i in range(4):
        chunk = styled56[i * 14:(i + 1) * 14 - 1]
        if styled56[i * 14 + 13] != luhn32(chunk):
            raise ValueError(f"check digit incorrect at chunk {i}")
        core.append(chunk)
    return "".join(core)


def chunkify(s56):
    """56 文字を 7 文字ずつ '-' 区切りにする (8 グループ)。"""
    return "-".join(s56[i:i + 7] for i in range(0, len(s56), 7))


def unchunkify(s):
    return s.replace("-", "").replace(" ", "")


def untypeoify(s):
    """base32 に出てこない紛らわしい文字を本家の規則で修正する (0→O, 1→I, 8→B)。"""
    return s.replace("0", "O").replace("1", "I").replace("8", "B")


def canonical_device_id(raw):
    """任意の受け入れ可能表記 (大小文字混在・区切り揺れ・タイプミス・旧 52 文字形式)
    を正規形 (8 グループ・検査数字つき) に整える。不正なら ValueError。"""
    s = raw.strip().strip("=")
    s = untypeoify(s.upper())
    s = unchunkify(s)
    if not re.fullmatch(r"[A-Z2-7]*", s):
        raise ValueError(f"{raw!r}: alphabet 外の文字を含む")
    if len(s) == 56:
        s = unluhnify(s)
    elif len(s) != 52:
        raise ValueError(f"{raw!r}: 正規化後の長さが {len(s)} (52/56 以外)")
    return chunkify(luhnify(s))


def validate_device_id(raw):
    """形式検査のみ。(ok, 正規形 or エラーメッセージ) を返す。"""
    try:
        return True, canonical_device_id(raw)
    except ValueError as e:
        return False, str(e)


def pem_to_der(cert_pem):
    if isinstance(cert_pem, bytes):
        cert_pem = cert_pem.decode("ascii", "strict")
    return ssl.PEM_cert_to_DER_cert(cert_pem)


def derive_device_id(cert_pem):
    """cert.pem (PEM) から device ID の正規形を導出する。

    本家 NewDeviceID: sha256(DER) → base32 (padding除去で 52 文字) →
    検査数字挿入 → 区切り付け。DER 抽出に失敗した場合は ssl が ValueError を
    出すのでそのまま上げる。
    """
    der = pem_to_der(cert_pem)
    b52 = base64.b32encode(hashlib.sha256(der).digest()).decode("ascii").rstrip("=")
    return chunkify(luhnify(b52))


# ---------------------------------------------------------------------------
# config.xml 解析
# ---------------------------------------------------------------------------

def parse_config(xml_text):
    """config.xml から folder 定義・device ID 一覧・GUI apikey を抽出する。

    戻り値: {"folders": [{"id","path"}...], "device_ids": [...], "api_key": str|None}
    解析できない場合は ValueError。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"XML として解析できない: {e}") from e
    folders = [
        {"id": f.get("id"), "path": f.get("path")}
        for f in root.iter("folder")
    ]
    device_ids = sorted({d.get("id") for d in root.iter("device") if d.get("id")})
    api_key = None
    for g in root.iter("gui"):
        k = g.findtext("apikey")
        if k:
            api_key = k.strip()
    return {"folders": folders, "device_ids": device_ids, "api_key": api_key}


def evaluate_folder_paths(folders, expected_root=DEFAULT_ROOT):
    """folder 定義が移行先の配置規則に合うかを判定する純関数。

    合格条件: 1 件以上ある・id/path が空でない・id 重複がない・
    全 path が絶対パスで expected_root 配下に収まる。
    戻り値: (problems: list[str], n_folders: int)
    """
    problems = []
    seen_ids = set()
    root = posixpath.normpath(expected_root)
    for f in folders:
        fid, fpath = f.get("id"), f.get("path")
        label = repr(fid) if fid else "(id 無し)"
        if not fid:
            problems.append(f"folder {label}: id が空")
        elif fid in seen_ids:
            problems.append(f"folder {label}: id が重複")
        seen_ids.add(fid)
        if not fpath:
            problems.append(f"folder {label}: path が未定義")
            continue
        norm = posixpath.normpath(fpath)
        if not norm.startswith("/"):
            problems.append(f"folder {label}: path {fpath!r} が相対パス")
        elif norm != root and not norm.startswith(root + "/"):
            problems.append(
                f"folder {label}: path {fpath!r} が {root} 配下にない"
                f" (旧 LXC 101 のパスが残っている疑い)")
    if not folders:
        problems.append("folder 定義が 1 件も無い (取り出したファイルが違う疑い)")
    return problems, len(folders)


# ---------------------------------------------------------------------------
# restic backup カバレッジの静的検査
# ---------------------------------------------------------------------------

def restic_backup_targets(manifest_text):
    """CronJob マニフェストから `restic backup` コマンドの引数を抜き出す純関数。

    行継続 (末尾 \\) をたどって 1 コマンド分の引数列に展開する。
    戻り値: {"found": bool, "targets": [...], "excludes": [...]}
    """
    idx = manifest_text.find("restic backup")
    if idx < 0:
        return {"found": False, "targets": [], "excludes": []}
    lines = []
    for line in manifest_text[idx:].splitlines():
        stripped = line.rstrip()
        lines.append(stripped.rstrip("\\"))
        if not stripped.endswith("\\"):
            break
    tokens = [t for t in " ".join(lines).split() if t]
    excludes, targets = [], []
    for tok in tokens:
        if tok == "restic":
            continue
        if tok == "backup" and not targets and not excludes:
            continue
        if tok.startswith("--exclude="):
            excludes.append(tok.split("=", 1)[1])
        elif tok.startswith("-"):
            continue
        else:
            targets.append(tok)
    return {"found": True, "targets": targets, "excludes": excludes}


def evaluate_restic_coverage(manifest_text):
    """backup 対象が PVC 全体で、除外が既知の安全なものだけかを判定する純関数。

    戻り値: (problems: list[str], info: dict)
    """
    parsed = restic_backup_targets(manifest_text)
    problems = []
    if not parsed["found"]:
        return ["manifest 内に `restic backup` コマンドが見つからない"], parsed
    if RESTIC_BACKUP_TARGET not in parsed["targets"]:
        problems.append(
            f"backup 対象に {RESTIC_BACKUP_TARGET} が無い (targets={parsed['targets']})")
    unexpected = [e for e in parsed["excludes"] if e not in SAFE_RESTIC_EXCLUDES]
    if unexpected:
        problems.append(
            f"既知の安全な除外以外の --exclude がある: {unexpected}"
            f" (同期フォルダがバックアップ漏れの可能性)")
    return problems, parsed


# ---------------------------------------------------------------------------
# ネットワークプローブ (失敗は例外として上げる。呼び側が「不明」に変換する)
# ---------------------------------------------------------------------------

def http_get_default(url, timeout=5.0):
    """GET して (status, body) を返す。到達不能で例外。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ops-syncthing-acceptance"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.status, resp.read(1024).decode("utf-8", "replace")


def probe_tcp(addr, timeout=5.0):
    """host:port への TCP 接続確認。張れれば True、失敗で例外。"""
    host, _, port = addr.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"sync addr が host:port 形でない: {addr!r}")
    with socket.create_connection((host, int(port)), timeout=timeout):
        return True


# ---------------------------------------------------------------------------
# ファイルシステム層
# ---------------------------------------------------------------------------

def locate_identity_files(data_dir):
    """cert.pem / key.pem / config.xml の所在を探す。

    レイアウトが 2 通りありうる (PVC 直下 / config/ 配下 — モジュール docstring
    の既知の死角参照) ので両方を見る。直下を優先。
    戻り値: {"cert": Path|None, "key": Path|None, "config_xml": Path|None,
             "layout": "flat"|"nested"|None}
    """
    names = {"cert": "cert.pem", "key": "key.pem", "config_xml": "config.xml"}
    found_flat = {k: data_dir / v for k, v in names.items()}
    if all(p.is_file() for p in found_flat.values()):
        found_flat["layout"] = "flat"
        return found_flat
    cfg = data_dir / "config"
    found_nested = {k: cfg / v for k, v in names.items()}
    if all(p.is_file() for p in found_nested.values()):
        found_nested["layout"] = "nested"
        return found_nested
    layout = None
    if any(p.exists() for p in found_nested.values()):
        layout = "nested"
    elif any(p.exists() for p in found_flat.values()):
        layout = "flat"
    partial = {k: (found_flat[k] if found_flat[k].exists() else found_nested[k])
               for k in names}
    partial["layout"] = layout
    return partial


def pvc_roundtrip(data_dir):
    """データディレクトリに書いて読み戻す。成功 (True, 詳細) / 失敗 (False, 理由)。"""
    payload = uuid.uuid4().hex.encode()
    probe = data_dir / f".acceptance-probe-{os.getpid()}.tmp"
    try:
        probe.write_bytes(payload)
        if probe.read_bytes() != payload:
            return False, "書き込んだ内容を読み戻すと一致しない"
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
    return True, f"{probe.name} ({len(payload)} B) の書き込み→読み戻し OK"


# ---------------------------------------------------------------------------
# check サブコマンド: 検査の組み立て
# ---------------------------------------------------------------------------

def run_checks(*, data_dir, expect_root=DEFAULT_ROOT, gui_url=DEFAULT_GUI_URL,
               sync_addr=DEFAULT_SYNC_ADDR,
               restic_manifest=DEFAULT_RESTIC_MANIFEST,
               http_get=None, tcp_connect=None):
    """検査リストを実行して結果のリストを返す。

    http_get / tcp_connect は注入ポイント (テストでネットワークなしにする)。
    http_get は「具体 URL を受けて (status, body) を返す」契約で、gui-health は
    <gui-url>/rest/noauth/health を渡す。例外を投げたら「不明」扱い。
    """
    http_get = http_get or http_get_default
    tcp_connect = tcp_connect or probe_tcp
    results = []

    def add(name, required, fn):
        try:
            status, detail = fn()
        except Exception as e:  # noqa: BLE001 — プローブ失敗は一律で不明にする
            status, detail = UNKNOWN, f"{type(e).__name__}: {e}"
        results.append(make_result(name, required, status, detail))

    located = locate_identity_files(data_dir)
    cert_pem_text = None
    config_info = None

    def chk_identity_files():
        missing = [str(p) for k, p in located.items()
                   if k != "layout" and not (p and p.is_file())]
        if missing:
            return FAIL, (f"identity ファイルが見つからない: {missing} "
                          f"(layout 判定={located['layout']})")
        # 読み出しは全部通ってから共有変数に載せる (途中失敗で後続検査が
        # 部分的に走るのを防ぐ)
        cert_read = None
        try:
            cert_read = located["cert"].read_text(encoding="ascii")
            pem_to_der(cert_read)
            located["key"].read_bytes()
            xml_text = located["config_xml"].read_text(encoding="utf-8")
            parsed = parse_config(xml_text)
        except (ValueError, UnicodeDecodeError, OSError) as e:
            return FAIL, f"ファイルの読み出しに失敗: {e}"
        nonlocal cert_pem_text, config_info
        cert_pem_text = cert_read
        config_info = parsed
        return PASS, (f"cert.pem / key.pem / config.xml を確認 "
                      f"(layout={located['layout']}, {located['cert']})")

    add("identity-files", True, chk_identity_files)

    def chk_device_id():
        assert cert_pem_text is not None  # identity-files が pass している前提
        derived = derive_device_id(cert_pem_text)
        ok, normalized = validate_device_id(derived)
        if not ok:
            return FAIL, f"導出した device ID が正規形でない: {normalized}"
        return PASS, f"導出 device ID: {derived}"

    def chk_device_id_guarded():
        if cert_pem_text is None:
            return UNKNOWN, "identity-files が通っていないため導出できない"
        return chk_device_id()

    add("device-id-format", True, chk_device_id_guarded)

    def chk_self_device():
        derived = derive_device_id(cert_pem_text)
        declared = []
        for did in config_info["device_ids"]:
            try:
                declared.append(canonical_device_id(did))
            except ValueError:
                declared.append(did)  # 壊れた宣言はそのまま比較に残す (fail させる)
        if derived in declared:
            return PASS, f"config.xml がこの device を宣言している ({len(declared)} 台中)"
        return FAIL, (f"導出 device ID {derived} が config.xml の device 一覧に無い "
                      f"(宣言済み {len(declared)} 台。cert と config の取り違えの疑い)")

    def chk_self_device_guarded():
        if cert_pem_text is None or config_info is None:
            return UNKNOWN, "前提検査 (identity/config) が通っていない"
        return chk_self_device()

    add("self-device-declared", True, chk_self_device_guarded)

    def chk_folder_paths():
        problems, n = evaluate_folder_paths(config_info["folders"], expect_root)
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, f"{n} フォルダすべてが {expect_root} 配下に収まる"

    def chk_folder_paths_guarded():
        if config_info is None:
            return UNKNOWN, "config.xml が読めていないため判定できない"
        return chk_folder_paths()

    add("folder-paths", True, chk_folder_paths_guarded)

    def chk_pvc():
        try:
            ok, detail = pvc_roundtrip(data_dir)
        except OSError as e:
            return FAIL, f"データディレクトリに書き込めない: {e}"
        return (PASS, detail) if ok else (FAIL, detail)

    add("pvc-rw", True, chk_pvc)

    def chk_restic():
        try:
            text = Path(restic_manifest).read_text(encoding="utf-8")
        except OSError as e:
            return UNKNOWN, (f"restic マニフェストを読めない ({e})。"
                             f"リポジトリ checkout から実行するか "
                             f"--restic-manifest で指定すること")
        problems, parsed = evaluate_restic_coverage(text)
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, (f"{RESTIC_BACKUP_TARGET} を対象、除外 {parsed['excludes']} は既知の安全組")

    add("restic-coverage", True, chk_restic)

    def chk_gui():
        url = gui_url.rstrip("/") + "/rest/noauth/health"
        try:
            status, body = http_get(url)
        except urllib.error.HTTPError as e:
            # 応答はあるが health エンドポイントが無い = 別の何かが応答している
            return FAIL, f"{url} が HTTP {e.code} を返した"
        if status == 200 and '"OK"' in body.replace(" ", ""):
            return PASS, f"{url} が health OK を返した"
        return FAIL, f"{url} の応答が異常: status={status} body={body[:80]!r}"

    add("gui-health", False, chk_gui)

    def chk_sync():
        tcp_connect(sync_addr)
        return PASS, f"{sync_addr} に TCP 接続できた"

    add("tailnet-sync", False, chk_sync)

    return results


# ---------------------------------------------------------------------------
# exercise サブコマンド: 合成データでの空回し演習 (DoD 2)
# ---------------------------------------------------------------------------

class SyncthingApiError(RuntimeError):
    pass


class SyncthingApi:
    """config.xml の <gui><apikey> を使う最小限の REST クライアント。

    HTTP 層は request() に集約してあり、テストでは差し替えられる。
    """

    def __init__(self, base_url, api_key, timeout=10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def request(self, method, path, payload=None):
        url = self.base_url + path
        data = None
        headers = {"User-Agent": "ops-syncthing-acceptance",
                   "X-API-Key": self.api_key}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                body = resp.read(4096).decode("utf-8", "replace")
                return resp.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise SyncthingApiError(
                f"{method} {path} -> HTTP {e.code}: {e.read(200)!r}") from e
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise SyncthingApiError(f"{method} {path} -> {type(e).__name__}: {e}") from e


def run_exercise(api, *, data_dir, self_device_id, expect_root=DEFAULT_ROOT,
                 folder_id=DEFAULT_EXERCISE_FOLDER, marker_name=EXERCISE_MARKER,
                 sleep=time.sleep, poll_interval_s=2.0, max_polls=30,
                 restic_manifest=DEFAULT_RESTIC_MANIFEST):
    """合成データでの空回し演習。戻り値は make_result と同型のリスト。

    手順: ダミーフォルダ作成 → マーカファイル書き込み → REST でフォルダ登録 →
    rescan → idle 収束待ち → 読み戻し照合 → restic 静的カバレッジ確認 →
    後始末 (フォルダ削除 + ディレクトリ削除。失敗経路でも必ず試みる)。

    後始末以外で落ちても本番データには触れない (触るのは folder_id 専用)。
    """
    results = []
    folder_path = Path(expect_root) / folder_id
    # 実際の書き込み先は data_dir 配下の同じ相対位置 (Job/Pod 内で PVC を
    # expect_root 以外に mount している場合に備える)
    local_dir = data_dir / folder_id
    marker = local_dir / marker_name
    payload = uuid.uuid4().hex.encode()

    def add(name, status, detail):
        results.append(make_result(name, True, status, detail))

    def cleanup():
        details = []
        try:
            api.request("DELETE", f"/rest/config/folders/{folder_id}")
            details.append("folder 登録を削除")
        except SyncthingApiError as e:
            details.append(f"folder 削除に失敗 ({e}) — GUI から手動で削除すること")
        try:
            marker.unlink()
            local_dir.rmdir()
            details.append("ダミーディレクトリを削除")
        except OSError as e:
            details.append(f"ダミーディレクトリ削除に失敗 ({e})")
        ok = all("失敗" not in d for d in details)
        add("exercise-cleanup", PASS if ok else UNKNOWN, "; ".join(details))

    registered = False
    converged = None
    try:
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
            marker.write_bytes(payload)
            add("exercise-write", PASS, f"{marker} に {len(payload)} B 書き込み")
        except OSError as e:
            add("exercise-write", FAIL, f"ダミーデータの書き込みに失敗: {e}")
            return results

        folder_conf = {
            "id": folder_id,
            "label": "acceptance exercise (P-0163)",
            "filesystemType": "basic",
            "path": str(folder_path),
            "type": "sendreceive",
            "paused": False,
            "rescanIntervalS": 30,
            "fsWatcherEnabled": True,
            "devices": [{"deviceID": self_device_id}],
        }
        try:
            # 前回の残骸があれば掃除してから登録 (無くても構わない)
            api.request("DELETE", f"/rest/config/folders/{folder_id}")
        except SyncthingApiError:
            pass
        try:
            api.request("PUT", f"/rest/config/folders/{folder_id}", folder_conf)
            registered = True
            add("exercise-folder-add", PASS,
                f"folder {folder_id!r} を登録 (path={folder_path})")
        except SyncthingApiError as e:
            add("exercise-folder-add", UNKNOWN, f"フォルダ登録に失敗: {e}")

        if registered:
            try:
                api.request("POST", f"/rest/db/scan?folder={folder_id}")
                last = ""
                for i in range(max_polls):
                    sleep(poll_interval_s if i else 0.0)
                    _, st = api.request("GET", f"/rest/db/status?folder={folder_id}")
                    last = json.dumps(
                        {k: st.get(k) for k in ("state", "globalBytes", "invalid")})
                    if st.get("invalid"):
                        converged = (FAIL, f"フォルダが invalid 状態: {st['invalid']}")
                        break
                    if (st.get("state") == "idle"
                            and st.get("globalBytes", 0) >= len(payload)):
                        converged = (PASS, f"rescan が収束 (poll {i + 1} 回目)")
                        break
                if converged is None:
                    converged = (UNKNOWN,
                                 f"rescan が時間内に収束しない (最後の状態: {last})")
            except SyncthingApiError as e:
                converged = (UNKNOWN, f"rescan 中に API エラー: {e}")
            add("exercise-rescan", *converged)

        if converged is not None and converged[0] == PASS:
            try:
                got = marker.read_bytes()
                if got == payload:
                    add("exercise-readback", PASS, f"{len(got)} B 読み戻して一致")
                else:
                    add("exercise-readback", FAIL, "読み戻した内容が一致しない")
            except OSError as e:
                add("exercise-readback", UNKNOWN, f"読み戻しに失敗: {e}")
        else:
            add("exercise-readback", UNKNOWN,
                "前提 (フォルダ登録と rescan 収束) が通っていないため判定しない")

        try:
            text = Path(restic_manifest).read_text(encoding="utf-8")
            problems, _parsed = evaluate_restic_coverage(text)
            # backup Pod は PVC を /mnt/syncthing-data に mount する。演習フォルダは
            # data_dir (= 同じ PVC) 配下にあるので、対象の包含は静的検査で足りる
            under = local_dir.resolve().is_relative_to(data_dir.resolve())
            if problems:
                add("exercise-restic-covered", FAIL, "; ".join(problems))
            elif under:
                add("exercise-restic-covered", PASS,
                    f"backup 対象 {RESTIC_BACKUP_TARGET} (= data_dir) 配下 "
                    f"(folder path={folder_path})")
            else:
                add("exercise-restic-covered", FAIL,
                    f"{local_dir} が data-dir 配下にない")
        except OSError as e:
            add("exercise-restic-covered", UNKNOWN,
                f"restic マニフェストを読めない: {e}")

    finally:
        cleanup()

    return results


# ---------------------------------------------------------------------------
# 表示と exit code
# ---------------------------------------------------------------------------

_MARKS = {PASS: "[合格]", FAIL: "[不合格]", UNKNOWN: "[不明] "}


def render(results, title):
    lines = [title]
    for r in results:
        req = "*" if r["required"] else " "
        lines.append(f" {_MARKS[r['status']]} {req} {r['name']}: {r['detail']}")
    n_fail = sum(1 for r in results if r["status"] == FAIL)
    n_unknown = sum(1 for r in results if r["status"] == UNKNOWN)
    n_pass = sum(1 for r in results if r["status"] == PASS)
    failed_names = [r["name"] for r in results if r["status"] == FAIL]
    unknown_names = [r["name"] for r in results if r["status"] == UNKNOWN]
    lines.append(f" 合計: 合格 {n_pass} / 不合格 {n_fail} / 不明 {n_unknown}"
                 f"  (* = 必須検査)")
    if failed_names:
        lines.append(f" 判定: 不合格 — {', '.join(failed_names)}")
    elif unknown_names:
        lines.append(f" 判定: 未確定あり — 不明: {', '.join(unknown_names)}"
                     f" (--strict ではこれも不合格)")
    else:
        lines.append(" 判定: 合格")
    return "\n".join(lines)


def exit_code(results, strict=False):
    """check の exit code。

    FAIL は必須/任意を問わず不合格 (「応答が異常」のような確定的な否定情報を
    見逃さないため)。UNKNOWN は単独では許容するが --strict では不合格。
    """
    if any(r["status"] == FAIL for r in results):
        return 1
    if strict and any(r["status"] == UNKNOWN for r in results):
        return 1
    return 0


def exercise_exit_code(results):
    """exercise の exit code。演習は確定させるものなので不明も不合格。"""
    return 1 if any(r["status"] in (FAIL, UNKNOWN) for r in results) else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="syncthing_acceptance.py",
        description="syncthing 移行の受け入れ検査 (P-0163)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="検査リストを実行する")
    p_check.add_argument("--data-dir", required=True, type=Path,
                         help="syncthing のホーム (PVC root)。cert.pem/config.xml を探す")
    p_check.add_argument("--root", default=DEFAULT_ROOT,
                         help="folder path が収まっているべき新 root")
    p_check.add_argument("--gui-url", default=DEFAULT_GUI_URL)
    p_check.add_argument("--sync-addr", default=DEFAULT_SYNC_ADDR)
    p_check.add_argument("--restic-manifest", type=Path,
                         default=DEFAULT_RESTIC_MANIFEST)
    p_check.add_argument("--strict", action="store_true",
                         help="「不明」も不合格にする (最終ゲート用)")

    p_ex = sub.add_parser("exercise", help="合成データでの空回し演習")
    p_ex.add_argument("--data-dir", required=True, type=Path)
    p_ex.add_argument("--root", default=DEFAULT_ROOT)
    p_ex.add_argument("--gui-url", default=DEFAULT_GUI_URL)
    p_ex.add_argument("--api-key", help="未指定なら data-dir の config.xml から読む")
    p_ex.add_argument("--folder-id", default=DEFAULT_EXERCISE_FOLDER)
    p_ex.add_argument("--timeout-s", type=float, default=120.0,
                      help="rescan 収束待ちの上限秒")

    args = parser.parse_args(argv)

    if not args.data_dir.is_dir():
        print(f"data-dir が存在しない: {args.data_dir}", file=sys.stderr)
        return 2

    if args.command == "check":
        results = run_checks(
            data_dir=args.data_dir, expect_root=args.root, gui_url=args.gui_url,
            sync_addr=args.sync_addr, restic_manifest=args.restic_manifest)
        print(render(results, f"syncthing 受け入れ検査 (data-dir={args.data_dir})"))
        return exit_code(results, strict=args.strict)

    # exercise
    located = locate_identity_files(args.data_dir)
    if not (located["cert"] and located["config_xml"]):
        print("cert.pem / config.xml が見つからない (先に `check` を通すこと)",
              file=sys.stderr)
        return 2
    try:
        self_device_id = derive_device_id(located["cert"].read_text(encoding="ascii"))
    except (ValueError, UnicodeDecodeError, OSError) as e:
        print(f"cert.pem を読めない: {e}", file=sys.stderr)
        return 2
    api_key = args.api_key
    if not api_key:
        try:
            api_key = parse_config(
                located["config_xml"].read_text(encoding="utf-8"))["api_key"]
        except (ValueError, OSError, UnicodeDecodeError) as e:
            print(f"config.xml から api key を取れない: {e}", file=sys.stderr)
            return 2
        if not api_key:
            print("config.xml に <gui><apikey> が無い (--api-key で指定すること)",
                  file=sys.stderr)
            return 2
    polls = max(1, int(args.timeout_s / 2.0))
    results = run_exercise(
        SyncthingApi(args.gui_url, api_key), data_dir=args.data_dir,
        self_device_id=self_device_id, expect_root=args.root,
        folder_id=args.folder_id, max_polls=polls)
    print(render(results, f"syncthing 空回し演習 (folder={args.folder_id})"))
    return exercise_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
