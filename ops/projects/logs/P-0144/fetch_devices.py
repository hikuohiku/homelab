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

復元モード (credential が無い環境で、外で実測した結果を原本として取り込む):
  --from-md TABLE.md     render_table() が書き出した形式の表 (issue #56 に貼られたもの等) から復元
  --from-json DATA.json  貼り付けられたデバイス一覧 JSON (API 生応答または devices.json) から復元
  --fetched-at TS        実測された時刻が分かる場合に記録する
  復元モードは一切通信しない。取り込んだデータが転写である旨を envelope に明記する。
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
SCHEMA = "p-0144.devices/1"
UNKNOWN_CELL = "(不明)"

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
        "schema": SCHEMA,
        "fetched_at": fetched_at,
        "source": "tailscale api v2 GET /tailnet/{tailnet}/devices (read-only; no changes made)",
        "tailnet": tailnet,
        "notes": FIELD_NOTES,
        "raw_response_keys": sorted(raw_response),
        "devices": raw_devices,
    }


TRANSCRIPTION_NOTE = (
    "このファイルは実測の転写 (transcription) であり API 生応答ではない。"
    "元の実測はこのリポジトリ外で読み取り専用に行われた。"
    "転写元に現れた項目のみを保持し、欠けている情報は捏造せず省略している"
)


def parse_markdown_table(text):
    """render_table() が書き出した表をデバイス録へ戻す (--from-md 用)。

    データ行の選別は「第 1 セルが数字」で判定する (ヘッダ・区切り行を確実に除外する。
    セッション1 の実測: ヘッダ除外のインデックスずれで最初のデータ行を落とす罠があるため)。
    捏造しない原則: 表に無い情報は補わず、(不明) セルはキーごと省略する。
    """
    devices = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or not cells[0].isdigit():
            continue
        if len(cells) < 8:
            die(f"表の列数が足りません (8 列必要、{len(cells)} 列): {line}")
        _num, expiry, setting, last_seen, os_name, who, _marks, name = cells[:8]
        device = {"name": "" if name == "(名前なし)" else name}
        if expiry != UNKNOWN_CELL:
            device["expires"] = expiry
        device["keyExpiryDisabled"] = setting.lower() == "disabled"
        if last_seen != UNKNOWN_CELL:
            device["lastSeen"] = last_seen
        if os_name != UNKNOWN_CELL:
            device["os"] = os_name
        if who != UNKNOWN_CELL:
            parts = [part.strip() for part in who.split(",") if part.strip()]
            tags = [part for part in parts if part.startswith("tag:")]
            users = [part for part in parts if not part.startswith("tag:")]
            if users:
                device["user"] = users[0]
            if tags:
                device["tags"] = tags
        devices.append(device)
    return devices


def build_ingest_envelope(devices, origin, fetched_at=None, tailnet="-"):
    """復元モード用の envelope。実測が転写であることを source/notes に明記する。"""
    return {
        "schema": SCHEMA,
        "fetched_at": fetched_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": (
            f"transcribed from {origin}; original read-only fetch was performed "
            "outside this repository (no changes made)"
        ),
        "tailnet": tailnet,
        "notes": dict(FIELD_NOTES, transcription=TRANSCRIPTION_NOTE),
        "devices": devices,
    }


def ingest_from_json(payload, origin, fetched_at=None):
    """貼り付け JSON を envelope へ整える (--from-json 用)。

    受け付けるのは (1) API 生応答 {"devices":[...]} と (2) このツールが書き出した
    envelope 全体。既存の実測フィールドは上書きせず保持し、転写経路である旨だけ
    source / notes に足す。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        die(f"JSON に devices 配列がありません (keys={keys})")
    if not payload["devices"]:
        die("devices 配列が空です (実測結果が入っているか確認してください)")
    if payload.get("schema") == SCHEMA:
        envelope = dict(payload)
        if fetched_at:
            envelope["fetched_at"] = fetched_at
        elif not envelope.get("fetched_at"):
            envelope["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        envelope["source"] = f"{payload.get('source')} ; saved via fetch_devices.py --from-json ({origin})"
        notes = dict(FIELD_NOTES)
        notes.update(payload.get("notes") or {})
        notes.setdefault("transcription", TRANSCRIPTION_NOTE)
        envelope["notes"] = notes
        return envelope
    return build_ingest_envelope(payload["devices"], origin, fetched_at=fetched_at)


def write_outputs(envelope, out_path):
    out_path = Path(out_path)
    out_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    md_path = out_path.with_suffix(".md")
    md_path.write_text(render_table(envelope["devices"], str(envelope.get("fetched_at") or "")))
    print(f"fetch_devices: {len(envelope['devices'])} devices -> {out_path}")
    print(f"fetch_devices: table -> {md_path}")


def main(argv=None, env=None):
    env = env if env is not None else dict(os.environ)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o", "--out", default=str(Path(__file__).resolve().parent / "devices.json"),
        help="devices.json の出力先 (devices.md も隣に書かれる)",
    )
    parser.add_argument(
        "--from-md", dest="from_md", metavar="TABLE.md",
        help="API を叩かず、render_table() 形式の表 (issue #56 に貼られたもの等) から devices.json を復元する",
    )
    parser.add_argument(
        "--from-json", dest="from_json", metavar="DATA.json",
        help="API を叩かず、貼り付けられたデバイス一覧 JSON (API 生応答または devices.json) から復元する",
    )
    parser.add_argument(
        "--fetched-at", dest="fetched_at", metavar="TIMESTAMP",
        help="実測された時刻が分かる場合にその値を記録する (省略時は転写時刻を入れ、source に転写である旨を残す)",
    )
    args = parser.parse_args(argv)

    if args.from_md and args.from_json:
        die("--from-md と --from-json は同時に指定できません")

    if args.from_md or args.from_json:
        if args.from_md:
            origin = f"markdown table ({args.from_md})"
            devices = parse_markdown_table(Path(args.from_md).read_text(encoding="utf-8"))
            if not devices:
                die(f"{args.from_md} からデータ行 (第 1 セルが数字の行) を見つけられませんでした")
            envelope = build_ingest_envelope(devices, origin, fetched_at=args.fetched_at)
        else:
            payload = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
            envelope = ingest_from_json(payload, f"pasted device list JSON ({args.from_json})", fetched_at=args.fetched_at)
        write_outputs(envelope, args.out)
        return

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

    write_outputs(envelope, args.out)


if __name__ == "__main__":
    main()
