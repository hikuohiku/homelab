#!/usr/bin/env bash
#
# check_pve_tls.sh — Proxmox VE (pveproxy) の TLS を外側から検証する (P-0103)
#
# 使い方:
#   ops/tools/check_pve_tls.sh [-H HOST] [-p PORT] [-c CAFILE] [-t TIMEOUT]
#
#   -H HOST     対象ホスト (既定: ${PVE_TLS_HOST:-hikuo-homeserver.tailae6c2.ts.net})
#   -p PORT     対象ポート (既定: ${PVE_TLS_PORT:-8006})
#   -c CAFILE   追加で信頼する CA 証明書 (PEM)。省略時はシステムのストアのみ信頼
#   -t TIMEOUT  接続タイムアウト秒 (既定: ${PVE_TLS_TIMEOUT:-10})
#
# 終了コード:
#   0  証明書チェーンとホスト名の検証に成功した (TLS 解消済み)
#   1  TLS 検証に失敗した — unknown authority / 自己署名 / SAN 不一致 / 期限切れ。
#      terraform apply 禁止は継続。差し替え手順は docs/pveproxy-tls.md
#   2  対象に接続できない等、判定不能な障害 (「TLS が壊れている」とは区別する)
#
# openssl 単体コマンドが無い環境 (autopilot Job イメージ) を想定し python3 ssl だけで動く。
# 出力は常に UTF-8 (PYTHONUTF8=1)。

set -u

HOST="${PVE_TLS_HOST:-hikuo-homeserver.tailae6c2.ts.net}"
PORT="${PVE_TLS_PORT:-8006}"
CA_FILE=""
TIMEOUT="${PVE_TLS_TIMEOUT:-10}"

usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,20p'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -H | --host) HOST="$2"; shift 2 ;;
    -p | --port) PORT="$2"; shift 2 ;;
    -c | --ca) CA_FILE="$2"; shift 2 ;;
    -t | --timeout) TIMEOUT="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

export PYTHONUTF8=1
exec python3 - "$HOST" "$PORT" "$CA_FILE" "$TIMEOUT" <<'PY'
import socket
import ssl
import sys

host = sys.argv[1]
port = int(sys.argv[2])
cafile = sys.argv[3] or None
timeout = float(sys.argv[4])
target = f"{host}:{port}"

ctx = ssl.create_default_context()
if cafile:
    ctx.load_verify_locations(cafile=cafile)

try:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
except ssl.SSLCertVerificationError as err:
    print(f"FAIL {target}: TLS 証明書を検証できない ({err.verify_message})", file=sys.stderr)
    print("     -> 信頼できる CA 発行の証明書に差し替わるまで terraform apply は禁止のまま。", file=sys.stderr)
    print("        差し替え手順は docs/pveproxy-tls.md を参照", file=sys.stderr)
    sys.exit(1)
except (ssl.SSLError, OSError) as err:
    print(f"ERROR {target}: {err}", file=sys.stderr)
    sys.exit(2)


def name(rdns):
    return ", ".join("/".join(f"{key}={value}" for key, value in rdn) for rdn in rdns)


print(f"OK {target}: TLS 検証に成功")
print(f"     subject={name(cert.get('subject', []))}")
print(f"     issuer={name(cert.get('issuer', []))}")
print(f"     notAfter={cert.get('notAfter', '?')} (期限切れ前の更新手順も docs/pveproxy-tls.md)")
sys.exit(0)
PY
