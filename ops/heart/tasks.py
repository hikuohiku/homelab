"""タスク依頼 (feedback note の kind: task-request) のキュー管理。純関数のみ。

P-0091。「やって」という人間の依頼は、今の器では triage の fall-through で
briefing に積まれるだけで立案 (curriculum) に流れない。ここは依頼を
「受領 → 未処理キュー → 採択で処理済み」の遷移として固定する部分。

キューの実体は ops-state ブランチの task-requests.jsonl (単一書き手 = heart)。
I/O は heart.py だけが行い、判断はこの純関数群と reconcile.decide() に集約する
(README「原則」と同じ分業)。

ライフサイクル:
  pending   未処理。curriculum-generate に最優先の原料として渡される
  processed 依頼から生まれた案 (request_id を持つ案) が採択された。
            以後 prompt には渡らない = 同じ依頼を毎回立案しない

棄却・破棄された案に対応する依頼は pending のまま残る (まだ叶えられていないので
再挑戦してよい)。毎回同じ形で出し直すことの抑止は、archive.jsonl の既出案が
生成役に見えていることと、プロンプトの指示に任せる (heart 側は決定論のまま)。
"""

import hashlib
import json

from .statefiles import now_iso

# telegram-adapter とのインターフェイスは feedback note JSON のトップレベル
# kind フィールドのこの値だけ。相手の実装に触れず並行で作る
KIND_TASK_REQUEST = "task-request"

QUEUE_FILE = "task-requests.jsonl"
PENDING = "pending"
PROCESSED = "processed"


def make_id(source):
    """依頼 id を source (note パス / issue コメント id) から決定論的に導く。

    受付のたびに変わる id は処理済み化の対応づけを壊すので、ハッシュで固定する。
    """
    return hashlib.sha256(str(source).encode()).hexdigest()[:16]


def new_record(source, body, now=None):
    """facts.collect_feedback() が拾った依頼 1 件をキューのレコードにする。"""
    return {
        "id": make_id(source),
        "source": str(source),
        "body": str(body),
        "received_at": now_iso(now),
        "status": PENDING,
    }


def merge_new(records, entries, now=None):
    """新着依頼を受領済みのキューに足す。id 重複は無視する (冪等取り込み)。"""
    known = {r.get("id") for r in records}
    out = list(records)
    for e in entries:
        rec = new_record(e.get("source"), e.get("body", ""), now)
        if rec["id"] in known:
            continue
        known.add(rec["id"])
        out.append(rec)
    return out


def pending(records):
    """未処理の依頼だけを受信順 (= 古い順) で返す。"""
    return [r for r in records if r.get("status") == PENDING]


def for_env(records, max_requests=20, max_body_chars=1000):
    """curriculum Job へ渡す TASK_REQUESTS 環境変数の中身 (JSON 文字列)。

    古い依頼から順に上限件数まで (新しいものだけで埋めると古い依頼が飢える)。
    本文は長すぎても立案に読めないので切る。空でも "[]" を返す
    (プロンプト側のプレースホルダ置換が常に成立するように)。
    """
    picked = []
    for r in pending(records):
        if len(picked) >= max_requests:
            break
        body = str(r.get("body", ""))
        picked.append({**r, "body": body[:max_body_chars]})
    return json.dumps(picked, ensure_ascii=False)


def mark_processed(records, ids, now=None):
    """採択済み案に紐づく依頼を processed にする。順序を保って新リストを返す。

    - 未知の id は無視 (案が依頼を捏造しても壊れない)
    - 既に processed の id も無視 (冪等。consume と mark の間で落ちて
      再実行しても二重に刻まない)
    """
    want = set(ids or [])
    out = []
    for r in records:
        if r.get("id") in want and r.get("status") != PROCESSED:
            r = {**r, "status": PROCESSED, "processed_at": now_iso(now)}
        out.append(r)
    return out


def done_ids(adopted_specs):
    """採択 spec 群から処理済みにすべき依頼 id を決定論的に集める (重複排除・整列)。"""
    return sorted({s["request_id"] for s in adopted_specs if s.get("request_id")})
