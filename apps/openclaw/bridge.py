#!/usr/bin/env python3
"""OpenClaw → ops-feedback 決定論パススルーブリッジ (P-0107)。

Telegram の受信メッセージ生テキストを、OpenClaw の LLM を一切経由せず
ops-feedback ブランチの inbox (ops/feedback/inbox/<id>.json) へ保存する
sidecar コンテナ用スクリプト。停止/veto/依頼の判定は heart の triage に任せる
(絶対条件)。この経路に判定ロジックは存在しない — SQLite 読み取り → JSON 変換 →
Contents API PUT のみ。

機構の実測 (2026-08-22, digest pin 済み image ghcr.io/openclaw/openclaw@sha256:6a31d44b…,
app/dist 内のソースを読んだ結果。spec の「ingress-spool-* を tail」は実態が違った):

- OpenClaw は Telegram getUpdates で受けた全 update を **allowlist フィルタより先に**
  状態 DB の channel_ingress_events テーブルへ spool する
  (extensions/telegram/src/polling-session.ts の writeTelegramSpooledUpdate 呼び出し)。
- 同テーブルへの書き込みは SQLite (`/home/node/.openclaw/state/openclaw.sqlite`)。
  「ingress-spool-<account>」というパスはキュー名の導出に使われるだけで、
  ファイルスプールは存在しない (dist/ingress-queue-Cxwis8TN.js 実読)。
- payload_json = {version: 1, updateId, receivedAt(epoch ms), update: <Telegram 生 update>}。
  処理済み update も墓石 (status='completed') として 30 日 / 上限 1000 件残るため、
  全 status を走査しても 1 update = 1 回だけ拾える。
- queue_name = JSON.stringify([channelId, accountId]) = '["telegram","default"]'
  (pluginId=telegram / accountId 未指定時の既定値 "default")。

この差分により本スクリプトは「ログやスプールファイルを tail」せず、状態 DB を
読み取り専用 (mode=ro) で走査する。OpenClaw 内部には触れず、「LLM を経由しない」
強制がファイル (DB) 読み取りというコードレベルの事実になる点は spec (b) の意図どおり。

許可されるのは allowlist 内送信者 (TELEGRAM_ALLOWED_USER_ID と一致する message.from.id)
の message.text / message.caption のみ。env 未設定・非数値の場合は 1 件も保存しない
(fail-closed。config.yaml の dmPolicy: allowlist と同じ向き)。
kind: task-request は付けない (spec 明記の禁じ手。生 body の保存のみ)。

保存形式は apps/ops-dashboard/app/src/app/api/feedback/route.ts の原本に合わせる:
{id, source: "telegram", received, body} / id = YYYYMMDD-HHMMSS-<hex3byte> /
JSON (indent 1) + 末尾改行 / ensureBranch → Contents API PUT / 422 衝突で id 振り直し。
id の時刻部には現在時刻ではなく spool の receivedAt を使う (受信順にソート可能にするため)。

運用上の前提と限界 (PROGRESS.md も参照):
- 初回起動 (cursor ファイル無し) は過去履歴を保存しない。heart の collect_feedback が
  初回 cursor を「現在まで」と置く (レビュー指摘 [7]) のと同じ考え方で、既存の
  max(event_id) までを既読扱いにする
- cursor は update_id の透かし。PVC 上に置くので再起動で続行する。
  gateway 側が古いバックアップから state を復元して update_id が巻き戻った場合は
  再保存されない (head-of-line 防止の skip も含め、取りこぼしより二重保存を避ける)。
  spool 自体は墓石込みで残るので、手動再取得は常に可能
- テスト: ops/tests/test_openclaw_bridge.py が純関数側を固定する。
  リポジトリルートから `python3 -m unittest ops.tests.test_openclaw_bridge`
"""

from __future__ import annotations

import base64
import json
import os
import random
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# --- 設定 (環境変数で上書き可。既定値はこの pod の実配置) ---

STATE_DB = os.environ.get("OPENCLAW_BRIDGE_STATE_DB", "/home/node/.openclaw/state/openclaw.sqlite")
CURSOR_PATH = os.environ.get("OPENCLAW_BRIDGE_CURSOR", "/home/node/.openclaw/bridge-cursor.json")
QUEUE_NAME = os.environ.get("OPENCLAW_BRIDGE_QUEUE_NAME", '["telegram","default"]')
REPO = os.environ.get("OPENCLAW_BRIDGE_REPO", "hikuohiku/homelab")
BRANCH = os.environ.get("OPENCLAW_BRIDGE_BRANCH", "ops-feedback")
BASE_BRANCH = os.environ.get("OPENCLAW_BRIDGE_BASE_BRANCH", "main")
INBOX_DIR = os.environ.get("OPENCLAW_BRIDGE_INBOX_DIR", "ops/feedback/inbox")
POLL_SECONDS = float(os.environ.get("OPENCLAW_BRIDGE_POLL_SECONDS", "15"))
API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")

# 同じ先頭レコードが連続で失敗したら諦めて飛ばす閾値 (spool に原本は残る)。
MAX_HEAD_ATTEMPTS = 3

# channel_ingress_events のうち読む列。event_id は 16 桁 zero-pad の文字列で
# 文字列比較 = 数値順 (update_id の単調性を Telegram が保証する)
SQL_COLUMNS = ("event_id", "payload_json")

BRANCH_READY = False


# --- 純関数 (unittest の対象。ここから下は IO を持たない) ---


def extract_message_text(update: Any) -> str | None:
    """Telegram 生 update から本文テキストをそのまま返す (trim しない = 生保存)。

    text 無し media は caption を見る。どちらも無ければ None
    (スタicker/位置情報など「生テキストが存在しない」ものは対象外)。
    message 以外 (edited_message / channel_post / callback_query 等) は対象外。
    """
    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def sender_id(update: Any) -> int | None:
    """message.from.id を返す (無ければ None)。"""
    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from")
    if not isinstance(sender, dict):
        return None
    value = sender.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def is_allowed_sender(update: Any, allowed_user_id: str | None) -> bool:
    """送信者が allowlist (TELEGRAM_ALLOWED_USER_ID) と一致するか。

    env 未設定・空・非数値は常に False (fail-closed)。比較は数値化して行う
    ("123" vs 123 の形式差で取りこぼさないため)。
    """
    if allowed_user_id is None:
        return False
    raw = allowed_user_id.strip()
    if not raw:
        return False
    try:
        expected = int(raw)
    except ValueError:
        return False
    actual = sender_id(update)
    return actual is not None and actual == expected


def format_received(received_at_ms: int) -> str:
    """epoch ms → route.ts の received 形式 (YYYY-MM-DDTHH:MM:SSZ, UTC)。"""
    dt = datetime.fromtimestamp(received_at_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def id_stamp(received_at_ms: int) -> str:
    """epoch ms → route.ts newNoteId() の時刻部 (YYYYMMDD-HHMMSS)。"""
    dt = datetime.fromtimestamp(received_at_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y%m%d-%H%M%S")


def build_note(received_at_ms: int, body: str, hex6: str | None = None) -> dict[str, str]:
    """inbox note 1 件を組み立てる。source 固定・kind は付けない (spec)。"""
    if hex6 is None:
        hex6 = f"{random.getrandbits(24):06x}"
    return {
        "id": f"{id_stamp(received_at_ms)}-{hex6}",
        "source": "telegram",
        "received": format_received(received_at_ms),
        "body": body,
    }


def render_note_json(note: dict[str, Any]) -> str:
    """note → PUT するバイト列。route.ts と同じ JSON.stringify(note, null, 1) + "\\n"。"""
    return json.dumps(note, ensure_ascii=False, indent=1) + "\n"


def parse_event_row(row: tuple[Any, ...]) -> dict[str, Any] | None:
    """(event_id, payload_json) → {update_id, received_at_ms, update}。

    壊れている行 (JSON 不正 / version 不一致 / updateId 非数値) は None で
    飛ばす。spool の書き手 (OpenClaw) と版が違うときの防御。
    """
    event_id, payload_json = row[0], row[1]
    if not isinstance(event_id, str) or not isinstance(payload_json, str):
        return None
    try:
        payload = json.loads(payload_json)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    update_id = payload.get("updateId")
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        return None
    received_at = payload.get("receivedAt")
    if not isinstance(received_at, int) or isinstance(received_at, bool):
        return None
    return {
        "update_id": update_id,
        "received_at_ms": received_at,
        "update": payload.get("update"),
    }


def select_events(
    rows: list[tuple[Any, ...]],
    last_update_id: int,
    allowed_user_id: str | None,
) -> list[dict[str, Any]]:
    """新着行 (event_id > cursor) を update_id 昇順で返す。

    status は問わない (pending / claimed / completed 墓石 / failed のどれでも
    まだ保存していなければ対象。墓石が残る仕様のおかげで取りこぼしが無い)。
    本文が無い行・allowlist 外の行はここで落ちる。判定はすべて決定論
    (キーワードでも LLM でもなく「送信者 ID の一致」のみ)。
    """
    events = []
    seen: set[int] = set()
    for row in rows:
        parsed = parse_event_row(row)
        if parsed is None:
            continue
        update_id = parsed["update_id"]
        if update_id <= last_update_id or update_id in seen:
            continue
        seen.add(update_id)
        text = extract_message_text(parsed["update"])
        if text is None:
            continue
        if not is_allowed_sender(parsed["update"], allowed_user_id):
            continue
        events.append({"update_id": update_id, "received_at_ms": parsed["received_at_ms"], "body": text})
    events.sort(key=lambda e: e["update_id"])
    return events


# --- IO (GitHub Contents API。route.ts の移植) ---


def gh_request(method: str, path: str, token: str, payload: Any = None) -> tuple[int, Any]:
    """GitHub API 1 リクエスト。(status, json) を返す。例外は呼び出し側へ。"""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "openclaw-feedback-bridge/1",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        status = error.code
    try:
        return status, json.loads(body)
    except ValueError:
        return status, {}


def ensure_branch(token: str) -> None:
    """ops-feedback ブランチが無ければ main から作る (dashboard route.ts 同型)。"""
    global BRANCH_READY
    if BRANCH_READY:
        return
    status, _ = gh_request("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}", token)
    if status == 200:
        BRANCH_READY = True
        return
    status, base = gh_request("GET", f"/repos/{REPO}/git/ref/heads/{BASE_BRANCH}", token)
    if status != 200:
        raise RuntimeError(f"base branch {BASE_BRANCH} が読めない ({status})")
    sha = (base.get("object") or {}).get("sha")
    status, created = gh_request(
        "POST",
        "/repos/{}/git/refs".format(REPO),
        token,
        {"ref": f"refs/heads/{BRANCH}", "sha": sha},
    )
    # 422 = 先に誰かが作っていた = それでよい
    if status not in (201, 422):
        raise RuntimeError(f"branch {BRANCH} を作れない ({status})")
    BRANCH_READY = True


def put_note(token: str, received_at_ms: int, body: str) -> tuple[bool, dict[str, str] | None]:
    """note 1 件を inbox へ保存する。成功で (True, 保存した note)。

    422 (同名ファイルあり = hex 衝突) のときだけ id を振り直して 1 度やり直す。
    それ以外の失敗は (False, None) で返し、cursor を進めない (次 tick で再試行)。
    """
    ensure_branch(token)
    for _attempt in range(2):
        note = build_note(received_at_ms, body)
        content = base64.b64encode(render_note_json(note).encode()).decode()
        status, _ = gh_request(
            "PUT",
            f"/repos/{REPO}/contents/{INBOX_DIR}/{note['id']}.json",
            token,
            {
                "message": f"feedback {note['id']} ({note['source']})",
                "content": content,
                "branch": BRANCH,
            },
        )
        if status in (200, 201):
            return True, note
        if status != 422:
            return False, None
    return False, None


# --- cursor (PVC 上の透かし。update_id 単調性に依存) ---


def load_cursor(path: str) -> int | None:
    """保存済み cursor を読む。無い (初回) なら None。"""
    try:
        with open(path) as f:
            value = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(value, dict) and isinstance(value.get("last_update_id"), int):
        return value["last_update_id"]
    return None


def save_cursor(path: str, last_update_id: int) -> None:
    """cursor を原子置換で保存 (中途半端な書き込みを残さない)。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump({"last_update_id": last_update_id, "saved_at": now_iso()}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def fetch_rows(db_path: str, last_event_id: str) -> list[tuple[Any, ...]] | None:
    """spool の新着行を読む。DB がまだ無い/マイグレーション中は None (待機)。"""
    if not os.path.exists(db_path):
        return None
    uri = f"file:{db_path}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        cursor = connection.execute(
            f"SELECT {', '.join(SQL_COLUMNS)} FROM channel_ingress_events "
            "WHERE queue_name = ? AND event_id > ? ORDER BY event_id ASC",
            (QUEUE_NAME, last_event_id),
        )
        return cursor.fetchall()
    except sqlite3.OperationalError:
        # テーブルがまだ無い (gateway 初期化前) / WAL の一時的ロック競合。
        # 書き手に触らないのがこのコンテナの鉄則なので、読み取り専用のまま待つ
        return None
    finally:
        connection.close()


def initial_cursor(db_path: str) -> str:
    """初回起動用: 既存の最大 event_id を「既読」として返す (履歴を遡って保存しない)。"""
    rows = fetch_rows(db_path, "")
    if not rows:
        return "0" * 16
    return max(row[0] for row in rows)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- メインループ ---

# 同じ文言の連続ログは捨てる (gateway 障害時に 15s 毎に同一行を撒かない)。
# 異なる文言が出たときはまた流す
_LAST_LOG_MESSAGE: str | None = None


def log(message: str) -> None:
    global _LAST_LOG_MESSAGE
    if message == _LAST_LOG_MESSAGE:
        return
    _LAST_LOG_MESSAGE = message
    print(f"[openclaw-feedback-bridge] {now_iso()} {message}", flush=True)


# 先頭レコードの失敗回数 (tick をまたいで数える。プロセス再起動でリセット = 最長でも
# 再起動後 MAX_HEAD_ATTEMPTS 回までは再試行する)
HEAD_ATTEMPTS: dict[int, int] = {}


def run_once(token: str, allowed_user_id: str | None, cursor_path: str) -> bool:
    """1 tick 分の処理。cursor を進めたら True。"""
    last_update_id = load_cursor(cursor_path)
    first_run = last_update_id is None
    watermark = last_update_id if last_update_id is not None else 0

    rows = fetch_rows(STATE_DB, f"{watermark:016d}")
    if rows is None:
        log(f"state DB 未準備 ({STATE_DB}). gateway の起動を待つ")
        return False
    if first_run:
        # 履歴は保存しない (初回は現在までを既読にする)。次 tick から新着のみ
        save_cursor(cursor_path, int(initial_cursor(STATE_DB)))
        log(f"初回起動: 既存 spool を既読として cursor 初期化 ({cursor_path})")
        return True

    events = select_events(rows, watermark, allowed_user_id)
    if not events:
        return False

    advanced = False
    for event in events:
        update_id = event["update_id"]
        attempts = HEAD_ATTEMPTS.get(update_id, 0)
        if attempts >= MAX_HEAD_ATTEMPTS:
            # 壊れたレコードで後続が永遠に止まるより、skip して流れを守る。
            # spool (墓石込み) に原本は残るので手動再取得は可能
            log(f"update {update_id}: {MAX_HEAD_ATTEMPTS} 回失敗したため skip")
            HEAD_ATTEMPTS.pop(update_id, None)
            save_cursor(cursor_path, update_id)
            advanced = True
            continue
        note = None
        try:
            ok, note = put_note(token, event["received_at_ms"], event["body"])
        except Exception as error:
            log(f"update {update_id}: 保存で例外 ({type(error).__name__}: {error})")
            ok = False
        if ok:
            log(f"saved ops-feedback:{INBOX_DIR}/{note['id']}.json "
                f"(update {update_id}, {len(event['body'])} chars)")
            HEAD_ATTEMPTS.pop(update_id, None)
            save_cursor(cursor_path, update_id)
            advanced = True
        else:
            HEAD_ATTEMPTS[update_id] = attempts + 1
            break  # 順序維持。失敗の直後は進めない
    return advanced


def main() -> int:
    token = (os.environ.get("AUTOPILOT_GITHUB_TOKEN") or "").strip()
    if not token:
        print("::error::AUTOPILOT_GITHUB_TOKEN が空です (openclaw-credentials を確認)", file=sys.stderr)
        return 1
    allowed_user_id = (os.environ.get("TELEGRAM_ALLOWED_USER_ID") or "").strip()
    if not allowed_user_id:
        log("TELEGRAM_ALLOWED_USER_ID 未設定: fail-closed で何も保存しない待機状態")

    warned_db_missing = False
    while True:
        if not os.path.exists(STATE_DB):
            if not warned_db_missing:
                log(f"state DB がまだ無い ({STATE_DB}). 待機中")
                warned_db_missing = True
        else:
            warned_db_missing = False
            try:
                run_once(token, allowed_user_id or None, CURSOR_PATH)
            except Exception:
                log(f"tick で例外 (継続): {traceback.format_exc(limit=2)}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
