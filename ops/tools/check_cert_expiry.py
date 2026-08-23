#!/usr/bin/env python3
"""TLS 証明書の期限台帳 (P-0188)。

「現行証明書がいつ切れるか」を機械で見ている者がいない状態をなくす。切れれば
Proxmox への到達自体が死に (T-0107 が terraform apply 禁止中なので復旧手順の
前提が崩れる)、クラスタ内の手作り TLS Secret (ArgoCD/Dex 等) も失効まで無音。
P-0105 (SOPS 地図)・P-0144 (tailnet 鍵期限) と同じ『秘密の賞味期限』系列。

見る対象:

- クラスタ内の type=kubernetes.io/tls の Secret 全件。tls.crt を DER パースして
  notAfter / SAN を取り出す。**data["tls.key"] には決して触れない** — 台帳に載せる
  のは派生値 (期限・残日数・SAN) だけで、Secret の値そのものは出力しない
- Proxmox API GET /nodes/{node}/certificates/info (Sys.Audit、agent token の
  読み取り権限内)。pveproxy 証明書の期限と SAN

判定 (DoD のしきい値。entry_status / summarize を参照):

- 残り >= 30 日: ok
- 残り < 30 日: warn — heart の briefing 対象 (ops/heart/facts.py cert_alert)
- 残り < 7 日: critical — incident 通知対象 (失効済み・負の日数を含む)
- パース不能: parse_error — 「無視」ではなく台帳に載せて fail-closed に寄せる
  (sops_dependency_map の「何も見つけられないのは整合ではなく失敗」と同じ思想)。
  summary は parse_error が 1 つでも warn 以下には沈まない
- unconfigured (Proxmox の token 未設定): budget (P-0128) の流儀にならい警報しない。
  「まだ繋げない」ことと「繋いだら壊れていた」ことを混同しないため

T-0107 フィールド: report["t0107"]["resolved"] は「期待する接続先名が pveproxy
証明書の SAN に含まれるか」の機械比較だけを行う。true になっても terraform apply
禁止の解除には**ならない** — 解除条件は docs/pveproxy-tls.md の 3 条件
(check_pve_tls.sh exit 0 / plan warning 消失 / download_file diff 消失) であり、
信頼 (誰が署名したか) の検証は check_pve_tls.sh の担当として併存する。

DER パースは stdlib のみの最小自前実装 (spec の制約)。イメージに openssl CLI も
cryptography も無く、PEM→DER の base64 デコードと ASN.1 の最小ウォーク
(validity の UTCTime/GeneralizedTime、SAN extension) だけで足りる。対応外の構造は
例外 → parse_error エントリになり、黙って誤判定しない。

fixture (--fixture PATH) で記録済み応答を読んでオフラインで走る。ロジックと I/O
の分離。スキーマは load_fixture() の docstring。

出力は JSON で、実行時刻のタイムスタンプを持たせない (証明書由来の日付と残日数
だけ。sops_dependency_map の「変化のない実行で diff を汚さない」流儀。残日数は
日単位でしか動かないので diff のノイズにならない)。

終了コード: 0 = 台帳が出力できた (warn/critical でも 0。鳴らし方は heart の担当)。
1 = 台帳が出せなかった (fixture 不備など)。観測ツールなので「証明書が危険」で
rc を上げない — rc は「計器が動いたか」だけを見る。

このモジュールは apps/ops-health-reporter/check_cert_expiry.py に**手動同期コピー**
されている (kustomize の configMapGenerator が kustomization.yaml の外のファイルを
読めないため。version_watch.py と同じ事情)。単体テストが 2 枚の同一性を sha256 で
機械検査するので、片方だけ変えると ops.tests.test_cert_expiry が落ちる。

固定テスト: ops/tests/test_cert_expiry.py
(`python3 -m unittest ops.tests.test_cert_expiry`)。ネットワークなしで通る。
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

# --- 判定しきい値 (DoD: 30 日未満で briefing 対象・7 日未満で incident) ---
WARN_DAYS = 30
CRITICAL_DAYS = 7

# T-0107 の「期待する接続先名」。check_pve_tls.sh の PVE_TLS_HOST 既定値と同じ
# 値を意図的に持つ (docs/pveproxy-tls.md 参照)。--expected-pve-name で上書き可
DEFAULT_EXPECTED_PVE_NAME = "hikuo-homeserver.tailae6c2.ts.net"

K8S_TLS_TYPE = "kubernetes.io/tls"

DEFAULT_PROXMOX_HOST = DEFAULT_EXPECTED_PVE_NAME
DEFAULT_PROXMOX_PORT = 8006
DEFAULT_PROXMOX_NODE = "node01"
DEFAULT_PROXMOX_TIMEOUT = "10"

# SAN extension の OID 2.5.29.17 の DER エンコーディング (06 03 55 1d 11)
_SAN_OID_BYTES = b"\x55\x1d\x11"


# ---------------------------------------------------------------------------
# 最小 ASN.1 / X.509 パーサ (stdlib のみ)
# ---------------------------------------------------------------------------


def _read_tlv(data, off):
    """1 個の TLV (tag-length-value) を読む。(tag, content, 次の offset) を返す。

    X.509 の構造に出る短形式・長形式 (4 バイト以内) のみ対応。多バイトタグや
    不定長は出会った時点で ValueError — 黙って読み飛ばして誤判定させない。
    """
    if off + 2 > len(data):
        raise ValueError("DER が途中で終わっている")
    tag = data[off]
    if tag & 0x1F == 0x1F:
        raise ValueError("多バイトタグは未対応 (tag={:#04x})".format(tag))
    first = data[off + 1]
    off += 2
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if n == 0 or n > 4:
            raise ValueError("対応できない長さ表現 (0x{:02x})".format(first))
        if off + n > len(data):
            raise ValueError("長さフィールドが途中で終わっている")
        length = int.from_bytes(data[off : off + n], "big")
        off += n
    end = off + length
    if end > len(data):
        raise ValueError("内容が長さに足りない (切り詰められた DER)")
    return tag, data[off:end], end


def _children(content):
    """SEQUENCE 等の content を子 TLV の列 (tag, value) に分解する。"""
    out = []
    off = 0
    while off < len(content):
        tag, value, off = _read_tlv(content, off)
        out.append((tag, value))
    return out


def _parse_asn1_time(tag, raw):
    """validity の Time (UTCTime=0x17 / GeneralizedTime=0x18) を aware UTC datetime へ。

    UTCTime は YYMMDDHHMMSSZ。世紀の解釈は X.509 の規則 (>=50 なら 19xx) で、
    strptime の %y (69 以上が 19xx) とは境界が違うので手で割る。
    GeneralizedTime は YYYYMMDDHHMMSSZ (小数秒 ".fff" が付いても落とす)。
    """
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("時刻フィールドが ASCII でない") from None
    if tag == 0x17:
        if len(text) != 13 or text[-1] != "Z" or not text[:12].isdigit():
            raise ValueError("UTCTime の書式が想定外: {!r}".format(text))
        yy = int(text[:2])
        year = 1900 + yy if yy >= 50 else 2000 + yy
        digits = text[2:-1]
    elif tag == 0x18:
        core = text[:-1] if text.endswith("Z") else text
        if "." in core:
            core = core.split(".", 1)[0]
        if len(core) != 14 or not core.isdigit():
            raise ValueError("GeneralizedTime の書式が想定外: {!r}".format(text))
        year = int(core[:4])
        digits = core[4:]
        width_year = True
    else:
        raise ValueError("validity が UTCTime/GeneralizedTime でない (tag={:#04x})".format(tag))
    month = int(digits[0:2])
    day = int(digits[2:4])
    hour = int(digits[4:6])
    minute = int(digits[6:8])
    second = int(digits[8:10])
    # datetime() は範囲外の月日に自前で ValueError を出す (fail-closed)
    return datetime.datetime(
        year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc
    )


def _general_name_to_str(tag, value):
    """GeneralNames の 1 要素を人間可読な文字列へ ("DNS:host" / "IP:a.b.c.d" 等)。"""
    if tag == 0x87:  # iPAddress (生オクテット)
        try:
            if len(value) == 4:
                return "IP:" + socket.inet_ntop(socket.AF_INET, value)
            if len(value) == 16:
                return "IP:" + socket.inet_ntop(socket.AF_INET6, value)
        except OSError:
            pass
        return "IP:<" + value.hex() + ">"
    labels = {0x81: "email", 0x82: "DNS", 0x86: "URI"}
    label = labels.get(tag)
    if label is None:
        return "tag{:#04x}:{}".format(tag, value.hex())
    return "{}:{}".format(label, value.decode("ascii", errors="replace"))


def parse_certificate_der(der):
    """X.509 Certificate DER から {"not_after": dt, "san": [str]} を取り出す。

    対応範囲は意図的に最小: TBSCertificate の version ([0]、省略可) → serialNumber
    → signature AlgorithmIdentifier → issuer Name → validity → subject Name →
    subjectPublicKeyInfo → extensions ([3]、省略可) の順に読むだけ。それ以外の
    形 (v1 名前付き拡張等) は ValueError になり parse_error エントリへ落ちる。
    """
    der = bytes(der or b"")
    if not der:
        raise ValueError("DER が空")
    tag, body, end = _read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("Certificate SEQUENCE でない (tag={:#04x})".format(tag))
    if end != len(der):
        raise ValueError("証明書の後ろに余分なバイトがある")
    items = _children(body)
    if len(items) < 3 or items[0][0] != 0x30:
        raise ValueError("Certificate の構成要素が読めない")
    fields = _children(items[0][1])
    idx = 0
    if idx < len(fields) and fields[idx][0] == 0xA0:  # [0] EXPLICIT version (省略可)
        idx += 1
    expected_tags = (0x02, 0x30, 0x30)  # serialNumber INTEGER, sigAlg SEQ, issuer SEQ
    for want in expected_tags:
        if idx >= len(fields) or fields[idx][0] != want:
            raise ValueError("TBSCertificate の構造が想定外 (位置 {})".format(idx))
        idx += 1
    if idx >= len(fields) or fields[idx][0] != 0x30:
        raise ValueError("validity が無い")
    times = _children(fields[idx][1])
    if len(times) != 2:
        raise ValueError("validity の要素数が 2 でない")
    not_after = _parse_asn1_time(*times[1])
    idx += 1
    # subject Name と subjectPublicKeyInfo は読み飛ばす (構造確認のみ)
    idx += 2
    san = []
    for f_tag, f_val in fields[idx:]:
        if f_tag != 0xA3:
            continue
        exts_wrapped = _children(f_val)  # [3] EXPLICIT → 中に SEQUENCE OF Extension
        if len(exts_wrapped) != 1 or exts_wrapped[0][0] != 0x30:
            raise ValueError("extensions の構造が想定外")
        for ext_tag, ext_val in _children(exts_wrapped[0][1]):
            if ext_tag != 0x30:
                continue
            parts = _children(ext_val)
            if len(parts) < 2 or parts[0][0] != 0x06:
                continue
            if parts[0][1] != _SAN_OID_BYTES:
                continue
            # Extension ::= SEQUENCE { OID, critical BOOLEAN DEFAULT, extnValue OCTET STRING }
            octet = next((p for p in parts[1:] if p[0] == 0x04), None)
            if octet is None:
                raise ValueError("SAN extension に extnValue が無い")
            names_seq = _children(octet[1])
            if len(names_seq) != 1 or names_seq[0][0] != 0x30:
                raise ValueError("SAN の GeneralNames が読めない")
            san = [_general_name_to_str(t, v) for t, v in _children(names_seq[0][1])]
    return {"not_after": not_after, "san": san}


_CERT_BLOCK_RE = re.compile(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.S)


def pem_chain_to_ders(pem_bytes):
    """PEM (複数証明書の連鎖を許す) を DER バイト列のリストへ。"""
    try:
        text = bytes(pem_bytes).decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("tls.crt がテキスト (PEM) でない") from None
    ders = []
    for m in _CERT_BLOCK_RE.finditer(text):
        body = "".join(m.group(1).split())
        try:
            ders.append(base64.b64decode(body, validate=True))
        except (ValueError, TypeError) as e:
            raise ValueError("CERTIFICATE ブロックの base64 が壊れている: {}".format(e)) from None
    if not ders:
        raise ValueError("CERTIFICATE ブロックが 1 つも無い")
    return ders


def parse_tls_crt_b64(crt_b64):
    """Secret data の tls.crt (base64 of PEM) をパースし、解析結果のリストを返す。

    チェーン全体を見て一番早く切れるものを束縛側として返す (leaf が先頭に来る
    慣習には依存しない)。どれか 1 枚でも読めなければ例外 — 部分的な成功を
    「読めた」ことにしない。
    """
    try:
        raw = base64.b64decode(str(crt_b64), validate=False)
    except (ValueError, TypeError) as e:
        raise ValueError("tls.crt の base64 が壊れている: {}".format(e)) from None
    parsed = []
    for der in pem_chain_to_ders(raw):
        one = parse_certificate_der(der)
        one["der_len"] = len(der)
        parsed.append(one)
    if not parsed:
        raise ValueError("証明書が 1 枚もパースできなかった")
    binding = min(parsed, key=lambda p: p["not_after"])
    return {
        "not_after": binding["not_after"],
        "san": binding["san"],
        "certs_in_chain": len(parsed),
    }


# ---------------------------------------------------------------------------
# 判定純関数 (I/O を含まない)
# ---------------------------------------------------------------------------


def iso_z(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_until(not_after, now):
    """残り日数 (整数、負で失効済み)。timedelta.days の床 semantics:
    29 日 23 時間後は 29 (<30 で warn)、ちょうど 30 日後は 30 (ok)。"""
    return (not_after - now).days


def entry_status(days_left):
    if days_left is None:
        return "parse_error"
    if days_left < CRITICAL_DAYS:
        return "critical"
    if days_left < WARN_DAYS:
        return "warn"
    return "ok"


def dns_name_from_san(entry):
    """SAN の 1 表記から DNS 名照合に使える値を取り出す。対象外なら None。

    PVE API の SAN は "DNS:x" / "IP:x" 接頭辞付き、DER 由来は素の名前で来る。
    大小文字は照合時に無視する。
    """
    e = entry.strip()
    if e[:4].upper() == "DNS:":
        return e[4:].lower()
    if ":" in e:  # IP: / URI: / email: 等はホスト名照合の対象外
        return None
    return e.lower()


def san_contains(san_entries, expected_name):
    exp = (expected_name or "").strip().lower()
    if not exp:
        return None
    for entry in san_entries or []:
        if dns_name_from_san(entry) == exp:
            return True
    return False


def build_k8s_entry(secret_item, now):
    """type=kubernetes.io/tls の Secret 1 件を台帳エントリへ。

    tls.key は存在しても読まない (エントリに載るのは派生値だけ)。読めない
    Secret は「無視」ではなく status=parse_error として台帳に載る。
    """
    meta = secret_item.get("metadata") or {}
    entry = {
        "kind": "k8s_tls_secret",
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
    }
    crt_b64 = (secret_item.get("data") or {}).get("tls.crt")
    try:
        if not crt_b64:
            raise ValueError("data['tls.crt'] が無い (type だけ kubernetes.io/tls?)")
        parsed = parse_tls_crt_b64(crt_b64)
    except ValueError as e:
        entry["status"] = "parse_error"
        entry["error"] = str(e)
        return entry
    days = days_until(parsed["not_after"], now)
    entry.update(
        not_after=iso_z(parsed["not_after"]),
        days_left=days,
        status=entry_status(days),
        san=parsed["san"] or None,
        certs_in_chain=parsed["certs_in_chain"],
    )
    return entry


def build_proxmox_entries(node, response, now, expected_name):
    """GET certificates/info の応答から pveproxy 証明書のエントリ列を作る。

    応答は {"data": {...}} 包装ありなしの両方を受ける。info 配列は通常 1 件だが、
    複数返ってきた場合は全件を別々のエントリに載せる (どれが「本命」かの推測で
    台帳を嘘で塗らない)。notafter は epoch 秒 (PVE API 実測の形)。
    """
    payload = response if isinstance(response, dict) else {}
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    info = payload.get("info")
    if not isinstance(info, list) or not info:
        return [
            {
                "kind": "proxmox_pveproxy",
                "node": node,
                "status": "parse_error",
                "error": "certificates/info の info 配列が読めない (応答形式の変更?)",
            }
        ]
    out = []
    for cert in info:
        entry = {"kind": "proxmox_pveproxy", "node": node}
        try:
            if not isinstance(cert, dict):
                raise ValueError("info 要素がオブジェクトでない")
            na = cert.get("notafter")
            if isinstance(na, bool) or not isinstance(na, (int, float, str)):
                raise ValueError("notafter が無いか型が想定外: {!r}".format(na))
            not_after = datetime.datetime.fromtimestamp(int(na), tz=datetime.timezone.utc)
            san = [str(s) for s in (cert.get("san") or [])]
            days = days_until(not_after, now)
            match = san_contains(san, expected_name)
            entry.update(
                subject=cert.get("subject"),
                not_after=iso_z(not_after),
                days_left=days,
                status=entry_status(days),
                san=san or None,
                expected_name=expected_name,
                san_match=match,
            )
        except (ValueError, TypeError, OverflowError, OSError) as e:
            entry.clear()
            entry.update(
                kind="proxmox_pveproxy",
                node=node,
                status="parse_error",
                error="{}: {}".format(type(e).__name__, e),
            )
        out.append(entry)
    return out


def build_t0107(entries, expected_name):
    """T-0107 の SAN 不一致状態を機械判定可能なフィールドにする (DoD 3)。

    resolved は「pveproxy 証明書の SAN に期待する接続先名が含まれるか」のみを見る。
    判定できる pveproxy エントリが無い (未設定・到達不能) 場合は None —
    「不一致」でも「解消済み」でもなく「分からない」を出す。
    """
    matches = [
        e["san_match"]
        for e in entries
        if e.get("kind") == "proxmox_pveproxy" and isinstance(e.get("san_match"), bool)
    ]
    resolved = any(matches) if matches else None
    return {
        "expected_name": expected_name,
        "resolved": resolved,
        "note": (
            "SAN 一致は terraform apply 禁止の解除条件の 1 つにすぎない。解除判断は "
            "docs/pveproxy-tls.md の 3 条件すべて揃ったときに限り、人間が行う "
            "(check_pve_tls.sh exit 0 / plan warning 消失 / download_file diff 消失)"
        ),
    }


def summarize(entries):
    """台帳全体を 1 つの status に畳む。最悪値で沈み、parse_error は warn 床。

    reason は決定的な順序 (critical → parse_error → warn、各アルファベット順) で
    組む — 同じ入力なら byte 等しくなるので履歴 diff が意味のある変化だけを
    含む (sops_dependency_map の流儀)。
    """
    counts = {"ok": 0, "warn": 0, "critical": 0, "parse_error": 0, "unconfigured": 0}

    def label(e):
        if e.get("kind") == "proxmox_pveproxy":
            return "proxmox/{}".format(e.get("node") or "?")
        ns, name = e.get("namespace"), e.get("name")
        return "{}/{}".format(ns or "?", name or "?")

    judged = []
    for e in entries:
        counts[e.get("status")] = counts.get(e.get("status"), 0) + 1
        if e.get("status") != "unconfigured":
            judged.append(e)

    critical = sorted(label(e) for e in judged if e.get("status") == "critical")
    broken = sorted(label(e) for e in judged if e.get("status") == "parse_error")
    warn = sorted(label(e) for e in judged if e.get("status") == "warn")

    parts = []
    if critical:
        parts.append("7日未満で失効: " + ", ".join(critical))
    if broken:
        parts.append("読めないため判定不能: " + ", ".join(broken))
    if warn:
        parts.append("30日未満で失効: " + ", ".join(warn))

    if not judged:
        if counts["unconfigured"]:
            reason = "判定対象が無い (Proxmox 未設定 {} 件)".format(counts["unconfigured"])
        else:
            reason = "k8s にも Proxmox にも判定対象の証明書が無かった"
        return {"status": "no_data", "reason": reason, "counts": counts}
    if critical:
        status = "critical"
    elif broken or warn:
        status = "warn"
    else:
        status = "ok"
        parts.insert(0, "{} 件すべて残り {} 日以上".format(len(judged), WARN_DAYS))
    return {"status": status, "reason": "; ".join(parts), "counts": counts}


def build_report(entries, expected_name=DEFAULT_EXPECTED_PVE_NAME):
    return {
        "entries": entries,
        "summary": summarize(entries),
        "t0107": build_t0107(entries, expected_name),
    }


# ---------------------------------------------------------------------------
# 収集 (I/O)。report.py からは k8s_get 注入、standalone では kubectl サブプロセス
# ---------------------------------------------------------------------------


def collect_k8s_tls_secrets(k8s_get):
    """全 namespace の Secret から kubernetes.io/tls 型だけを返す。

    reporter (SA token + urllib) の既存経路。spec の「kubectl で列挙」は対象の
    指定であって手段の指定ではない (PROJECT.md)。値はここで捨てられず後段の
    build_k8s_entry も tls.crt しか読まない。
    """
    data = k8s_get("/api/v1/secrets")
    return [i for i in data.get("items", []) if i.get("type") == K8S_TLS_TYPE]


def collect_k8s_tls_secrets_via_kubectl(kubectl_bin="kubectl"):
    """standalone 実行用。kubectl サブプロセスで Secret を列挙する。"""
    cmd = [kubectl_bin, "get", "secrets", "--all-namespaces", "-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)  # noqa: S603
    if proc.returncode != 0:
        raise RuntimeError(
            "kubectl が失敗 (rc={}): {}".format(
                proc.returncode, proc.stderr.decode(errors="replace").strip()[:300]
            )
        )
    data = json.loads(proc.stdout.decode())
    return [i for i in data.get("items", []) if i.get("type") == K8S_TLS_TYPE]


def proxmox_settings(env=None):
    """環境変数から Proxmox 接続設定を組む。token 未設定なら None (unconfigured)。"""
    env = os.environ if env is None else env
    token_id = env.get("PROXMOX_TOKEN_ID")
    token_secret = env.get("PROXMOX_TOKEN_SECRET")
    if not token_id or not token_secret:
        return None
    return {
        "host": env.get("PROXMOX_HOST", DEFAULT_PROXMOX_HOST),
        "port": int(env.get("PROXMOX_PORT", DEFAULT_PROXMOX_PORT)),
        "node": env.get("PROXMOX_NODE", DEFAULT_PROXMOX_NODE),
        "timeout": float(env.get("PROXMOX_TIMEOUT", DEFAULT_PROXMOX_TIMEOUT)),
        "token_id": token_id,
        "token_secret": token_secret,
    }


def fetch_proxmox_cert_info(settings):
    """GET /nodes/{node}/certificates/info を呼び、JSON 応答を返す。

    TLS 検証をあえて切る: 見に行く対象の証明書こそが今は信頼できない (T-0107)
    ので、検証ありでは観測自体が不可能。台帳は「証明書の中身」を見るもので、
    「誰が署名したか」の検証は check_pve_tls.sh の担当として併存する。
    """
    url = "https://{host}:{port}/api2/json/nodes/{node}/certificates/info".format(**settings)
    auth = "PVEAPIToken={token_id}={token_secret}".format(**settings)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=settings["timeout"]) as resp:
        return json.load(resp)


def proxmox_entry_unconfigured(node=None, error=None):
    return {
        "kind": "proxmox_pveproxy",
        "node": node or DEFAULT_PROXMOX_NODE,
        "status": "unconfigured" if error is None else "parse_error",
        **({} if error is None else {"error": error}),
    }


def collect_report(
    k8s_get=None,
    kubectl_bin=None,
    env=None,
    now=None,
    expected_name=DEFAULT_EXPECTED_PVE_NAME,
):
    """収集から台帳構築まで。report.py は k8s_get=k8s_get で呼ぶ。

    1 ソースの失敗で全体を止めない (collect_pvc_usage と同じ思想)。Proxmox が
    未設定なら unconfigured エントリとして正直に載せる。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    env = os.environ if env is None else env
    entries = []

    try:
        if k8s_get is not None:
            items = collect_k8s_tls_secrets(k8s_get)
        elif kubectl_bin:
            items = collect_k8s_tls_secrets_via_kubectl(kubectl_bin)
        else:
            raise RuntimeError("k8s_get も kubectl_bin も無い (収集手段が指定されていない)")
        entries.extend(build_k8s_entry(item, now) for item in items)
    except Exception as e:  # noqa: BLE001 — k8s 側が死んでも Proxmox 側は見たい
        entries.append(
            {
                "kind": "k8s_tls_secret",
                "namespace": None,
                "name": None,
                "status": "parse_error",
                "error": "{}: {}".format(type(e).__name__, e),
            }
        )

    settings = proxmox_settings(env)
    if settings is None:
        entries.append(proxmox_entry_unconfigured(env.get("PROXMOX_NODE")))
    else:
        try:
            response = fetch_proxmox_cert_info(settings)
        except Exception as e:  # noqa: BLE001 — 到達不能も台帳に載せる
            entries.append(proxmox_entry_unconfigured(settings["node"], "{}: {}".format(type(e).__name__, e)))
        else:
            entries.extend(build_proxmox_entries(settings["node"], response, now, expected_name))

    return build_report(entries, expected_name)


# ---------------------------------------------------------------------------
# fixture (オフライン再生) と CLI
# ---------------------------------------------------------------------------

FIXTURE_SCHEMA_DOC = """fixture JSON スキーマ (未知のキーは無視される):

{
  "now": "2026-08-23T12:00:00Z",          // 任意。評価基準時刻の固定 (再現性)
  "k8s_secrets": {"items": [ ...Secret... ]},   // GET /api/v1/secrets 応答相当
  "proxmox": {                                   // 任意 (無ければ unconfigured 扱い)
    "node": "node01",
    "response": {"data": {"info": [...]}},       // certificates/info 応答相当
    "error": "..."                               // response の代わりに記録された失敗
  }
}
"""


def load_fixture(path):
    text = Path(path).read_text(encoding="utf-8")
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("fixture のトップレベルがオブジェクトでない")
    return doc


def _fixture_now(doc):
    raw = doc.get("now")
    if not raw:
        return datetime.datetime.now(datetime.timezone.utc)
    text = str(raw).strip()
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    dt = datetime.datetime.fromisoformat(candidate)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def run_from_fixture(doc, expected_name=DEFAULT_EXPECTED_PVE_NAME):
    now = _fixture_now(doc)
    entries = []
    secrets = doc.get("k8s_secrets") or {}
    items = secrets.get("items", []) if isinstance(secrets, dict) else list(secrets)
    entries.extend(build_k8s_entry(item, now) for item in items)

    px = doc.get("proxmox")
    if px is None:
        entries.append(proxmox_entry_unconfigured())
    elif isinstance(px, dict):
        if isinstance(px.get("response"), dict):
            entries.extend(
                build_proxmox_entries(px.get("node", "?"), px["response"], now, expected_name)
            )
        elif px.get("error"):
            entries.append(proxmox_entry_unconfigured(px.get("node"), str(px["error"])))
        else:
            entries.append(proxmox_entry_unconfigured(px.get("node"), "response も error も無い"))
    else:
        entries.append(proxmox_entry_unconfigured(None, "proxmox キーがオブジェクトでない"))
    return build_report(entries, expected_name)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="check_cert_expiry.py",
        description="TLS 証明書の期限台帳を作る (P-0188)。--fixture でオフライン再生。",
    )
    parser.add_argument(
        "--fixture",
        metavar="PATH",
        help="記録済み応答 JSON を読んでクラスタ/Proxmox に触らずに走る",
    )
    parser.add_argument(
        "--expected-pve-name",
        default=DEFAULT_EXPECTED_PVE_NAME,
        help="T-0107 判定に使う接続先名 (既定: %(default)s)",
    )
    parser.add_argument(
        "--kubectl",
        default="kubectl",
        help="live 実行時に使う kubectl バイナリ (既定: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        if args.fixture:
            report = run_from_fixture(load_fixture(args.fixture), args.expected_pve_name)
        else:
            report = collect_report(
                kubectl_bin=args.kubectl,
                env=os.environ,
                expected_name=args.expected_pve_name,
            )
    except (OSError, ValueError, RuntimeError) as e:
        print("check_cert_expiry: {}: {}".format(type(e).__name__, e), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
