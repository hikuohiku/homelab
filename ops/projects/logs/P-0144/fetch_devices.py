#!/usr/bin/env python3
"""Tailscale tailnet の全デバイスを実測し devices.json / devices.md を書き出す。

P-0144 の実測ツール。読み取り専用で、tailnet への変更は一切しない (DoD (4)):
  - POST https://api.tailscale.com/api/v2/oauth/token   (access token の発行のみ)
  - GET  https://api.tailscale.com/api/v2/tailnet/-/devices
上記以外の通信をしないことを unit test が検査する。

credential (環境変数。上から順に採用):
  TAILSCALE_API_KEY                     個人アクセストークン (Bearer で直接使用)
  TAILSCALE_OAUTH_CLIENT_ID / _SECRET   OAuth client (client credentials flow)
  TAILSCALE_AGENT_CLIENT_ID / _SECRET   同上 (.envrc のエージェント用 read-only client)

対象 tailnet は環境変数 TAILSCALE_TAILNET で明示できる (既定 "-" = credential の所属 tailnet)。

出力:
  -o で指定したパス (既定はこのスクリプトと同じディレクトリ) に devices.json と devices.md。
  devices.json は API 生応答を envelope に包んでそのまま保存する。
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.tailscale.com/api/v2"
TOKEN_PATH = "/oauth/token"
DEVICES_PATH = "/tailnet/-/devices"
TIMEOUT_SECONDS = 30

CREDENTIAL_VARIANTS = (
    ("bearer", ("TAILSCALE_API_KEY",)),
    ("oauth", ("TAILSCALE_OAUTH_CLIENT_ID", "TAILSCALE_OAUTH_CLIENT_SECRET")),
    ("oauth", ("TAILSCALE_AGENT_CLIENT_ID", "TAILSCALE_AGENT_SECRET")),
)

FIELD_NOTES = {
    "expires": "ノードキーの失効日時 (RFC3339)。再認証のたびに伸びるため取得時点のスナップショット",
    "keyExpiryDisabled": "true なら鍵失効が無効 (tagged デバイスは既定で true)。expiry カウントの対象外",
    "lastSeen": "最終接続時刻 (RFC3339)",
}


def die(message):
    print(f"fetch_devices: {message}", file=sys.stderr)
    raise SystemExit(1)


def pick_credentials(env):
    for kind, names in CREDENTIAL_VARIANTS:
        values = [env.get(name) for name in names]
        if all(values):
            return kind, list(values)
    wanted = ", ".join("/".join(names) for _, names in CREDENTIAL_VARIANTS)
    die(f"credential が見つかりません。次のいずれかを環境変数で渡してください: {wanted}")


def http_request(url, method="GET", headers=None, data=None):
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_access_token(client_id, client_secret):
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic "
        + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode(),
    }
    payload = http_request(API_BASE + TOKEN_PATH, method="POST", headers=headers, data=body)
    token = payload.get("access_token")
    if not token:
        die(f"token 応答に access_token がありません (keys={sorted(payload)})")
    return token


def fetch_devices(token, tailnet="-"):
    if tailnet == "-":
        url = API_BASE + DEVICES_PATH
    else:
        url = f"{API_BASE}/tailnet/{urllib.parse.quote(tailnet)}/devices"
    return http_request(url, headers={"Authorization": f"Bearer {token}"})


def parse_expiry(value):
    """RFC3339 を datetime (UTC) へ。取れないものは None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def device_name(device):
    return str(device.get("name") or device.get("hostname") or "")


def device_marks(device):
    """デバイス名/タグからの印。名前パターンの先験を持たない方針なので、
    判定根拠にした文字列をそのまま印にする (docs/tailscale-recovery.md 参照)。"""
    name = device_name(device).lower()
    tags = [str(tag) for tag in (device.get("tags") or [])]
    marks = []
    if "node01" in name:
        marks.append("node01?")
    if name.startswith("k8s-") or any(tag.startswith("tag:k8s") for tag in tags):
        marks.append("cluster-proxy")
    if "autopilot" in name:
        marks.append("autopilot")
    return marks


def sort_key(device):
    """期限が近い順。失効無効 (disabled) と期限不明は末尾へ。"""
    disabled = bool(device.get("keyExpiryDisabled"))
    expiry = parse_expiry(device.get("expires"))
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    if disabled:
        return (2, far_future)
    if expiry is None:
        return (1, far_future)
    return (0, expiry)


def render_table(devices, fetched_at):
    lines = [
        "# tailnet デバイスの鍵期限 (実測)",
        "",
        f"- 取得日時: {fetched_at}",
        "- 並び順: 失効日時が近い順 (keyExpiryDisabled=true と期限不明は末尾)",
        "- 印の意味: `node01?`=node01 名義 / `cluster-proxy`=k8s- 接頭辞または tag:k8s* / `autopilot`=autopilot 名義",
        "",
        "| # | 期限 (expires) | 失効設定 | lastSeen | os | user/tags | 印 | name |",
        "|---|----------------|----------|----------|----|-----------|----|------|",
    ]
    for index, device in enumerate(sorted(devices, key=sort_key), start=1):
        setting = "disabled" if bool(device.get("keyExpiryDisabled")) else "enabled"
        expiry = str(device.get("expires") or "(不明)")
        last_seen = str(device.get("lastSeen") or "(不明)")
        os_name = str(device.get("os") or "(不明)")
        who_parts = [str(device.get("user") or "")] + [str(t) for t in (device.get("tags") or [])]
        who = ", ".join(part for part in who_parts if part) or "(不明)"
        name = device_name(device) or "(名前なし)"
        marks = " ".join(device_marks(device)) or "-"
        lines.append(
            f"| {index} | {expiry} | {setting} | {last_seen} | {os_name} | {who} | {marks} | {name} |"
        )
    return "\n".join(lines) + "\n"


def build_envelope(raw_response, fetched_at, tailnet):
    raw_devices = raw_response.get("devices")
    if not isinstance(raw_devices, list):
        die(f"応答に devices 配列がありません (keys={sorted(raw_response)})")
    return {
        "schema": "p-0144.devices/1",
        "fetched_at": fetched_at,
        "source": "tailscale api v2 GET /tailnet/{tailnet}/devices (read-only; no changes made)",
        "tailnet": tailnet,
        "notes": FIELD_NOTES,
        "raw_response_keys": sorted(raw_response),
        "devices": raw_devices,
    }


def main(argv=None, env=None):
    env = env if env is not None else dict(os.environ)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o", "--out", default=str(Path(__file__).resolve().parent / "devices.json"),
        help="devices.json の出力先 (devices.md も隣に書かれる)",
    )
    args = parser.parse_args(argv)

    kind, values = pick_credentials(env)
    if kind == "bearer":
        token = values[0]
    else:
        token = fetch_access_token(values[0], values[1])

    try:
        raw = fetch_devices(token, tailnet=env.get("TAILSCALE_TAILNET") or "-")
    except urllib.error.HTTPError as error:
        die(f"デバイス一覧の取得に失敗: HTTP {error.code} {error.reason}")

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = build_envelope(raw, fetched_at, env.get("TAILSCALE_TAILNET") or "-")

    out_path = Path(args.out)
    out_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    md_path = out_path.with_suffix(".md")
    md_path.write_text(render_table(raw.get("devices", []), fetched_at))

    print(f"fetch_devices: {len(envelope['devices'])} devices -> {out_path}")
    print(f"fetch_devices: table -> {md_path}")


if __name__ == "__main__":
    main()
