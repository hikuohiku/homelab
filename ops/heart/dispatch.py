"""即時 dispatch (設計 rev3 Phase D) のレコード組み立て。純関数のみ。

コアは `dispatch_task` で「いま着手してほしい」を heart に**同期で**頼む。
判定 (可否) は reconcile.admit()、実行 (Job 作成) は gate.py、
`Project` CR への登録は reconcile.decide() が担う。ここはその 3 者が受け渡す
レコードの形だけを持つ。

なぜ id を内容から導くか:
  コアが同じ依頼を 2 回投げても Job が 2 本立ってはいけない。dispatch_id は
  (title, body) のハッシュなので、再送・再試行は必ず同じ id になり、
  台帳と Job 名の両方で 1 件に畳まれる。

verify を取らないこと (2026-08-24, 所有者判断):
  受入検証も採択ゲートも dispatch 経路から外した。verify を書くのは LLM なので
  いくらでも迂回でき、機械の判定として意味を成さない。残る機械のゲートは
  CI と soak だけで、完成の判断は reviewer とコアの確認に移る
  (ops/heart/README.md の「dispatch 経路で失われる保証」)。

なぜプロジェクト id を P-9NNN にするか:
  curriculum が archive.jsonl から採番する系列 (P-0NNN) と衝突させないため。
  番号空間を分けておくと、`P-9` で始まる id を見ただけで「コアが即時 dispatch
  したもの = main の archive.jsonl に spec が無いもの」と分かる。
"""

import hashlib
import json

from .statefiles import now_iso

# /data 直下。バス inbox (command-bus) と同じ「サイドカーが書いて heart が読む」
# 流儀に揃えるが、書き手は同じプロセスの gate スレッドなので別ディレクトリにする
DISPATCH_DIR = "dispatch"
INBOX = "inbox"
DONE = "done"
LEDGER_FILE = "ledger.jsonl"

# 即時 dispatch のプロジェクト id 空間。curriculum の採番 (P-0NNN) と重ならない
PROJECT_ID_PREFIX = "P-9"
PROJECT_ID_MIN = 9000
PROJECT_ID_MAX = 9999

# 引数の上限。コアの Go 側 (dispatch.go の maxCommand*Runes) と揃える。
# heart 側にも置くのは、バス経由でない直の HTTP を信用しないため
MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 4000

# 受理の結末。inbox のレコードが持つ状態はこれだけ
DISPATCHED = "dispatched"  # Job を作った
ABORTED = "aborted"  # 受理から Job 作成までの間に停止がかかった / 作れなかった


def dispatch_id(title, body):
    """要求の内容から決定論的に導く id。時刻も乱数も混ぜない (冪等の要)。"""
    raw = "\x00".join([str(title), str(body)])
    return "d-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize(request):
    """HTTP で来た dict を (title, body) に正規化する。

    verify は受け取らない。付いて来ても黙って落とす — 古いコアからの要求でも
    dispatch_id が内容だけで決まり、冪等が壊れないようにするため。
    """
    title = str(request.get("title") or "").strip()
    body = str(request.get("body") or "").strip()
    return title, body


def allocate_project_id(used):
    """使用済み id 集合から次の P-9NNN を返す。空きが無ければ None。"""
    taken = set(used or ())
    for n in range(PROJECT_ID_MIN, PROJECT_ID_MAX + 1):
        pid = f"P-{n}"
        if pid not in taken:
            return pid
    return None


def spec_of(title, body, project_id):
    """runner が読む spec。main の archive.jsonl には載らないので、heart が
    Job の env (HEART_SPEC_JSON) で渡す。改竄不能なのは、これを組むのが
    heart だけで、runner のブランチからは触れないため。"""
    return {
        "id": project_id,
        "title": title,
        "why": body,
        "dod": body,
        # 受入検証は持たない。runner は「作業を終えた」時点で PR を出し、
        # 完成の判断は reviewer とコアが担う (2026-08-24 の所有者判断)
        "verify": [],
        "irreversible": False,
        # コアは capability を宣言できない (admit が要求ごと弾く)。
        # ここを空で固定するのが SA 宣言連鎖の入口側の守り
        "capabilities": [],
        "touches_apps": False,
        "confidence": "unsure",
        "adopted": True,
        "proposed_by": "core-dispatch",
        "cell": ["self", "feature"],
    }


def new_record(title, body, project_id, now=None):
    """受理した dispatch 1 件。gate が台帳に刻み、結末を書き足して inbox に置く。"""
    return {
        "dispatch_id": dispatch_id(title, body),
        "project_id": project_id,
        "requested_by": "core",
        "accepted_at": now_iso(now),
        "title": title,
        "body": body,
        "spec": spec_of(title, body, project_id),
        "status": None,
        "job": None,
    }


def ledger_entry(record, event, now=None, **extra):
    """台帳 1 行。プロセスが落ちても「何を受理したか」が残る唯一の記録。"""
    return {
        "at": now_iso(now),
        "event": event,
        "dispatch_id": record["dispatch_id"],
        "project_id": record["project_id"],
        "requested_by": record.get("requested_by", "core"),
        **extra,
    }


def to_project(record, now=None):
    """inbox のレコードを projects.json のエントリにする。

    dispatched は **active** で登録する (Job は既に走っている)。aborted は
    終端 (stalled) で登録し、なぜ動かなかったかを projects.json に残す。
    """
    pid = record["project_id"]
    project = {
        "id": pid,
        "title": record.get("title", ""),
        "state": "active",
        "branch": f"project/{pid.lower()}",
        "irreversible": False,
        "capabilities": [],
        "touches_apps": False,
        "verify": [],
        "confidence": "unsure",
        "budget": {"used_tokens": 0},
        "created": now_iso(now)[:10],
        # 即時 dispatch の出自。ダッシュボードと audit がここを読む
        "dispatch_id": record["dispatch_id"],
        "requested_by": record.get("requested_by", "core"),
        # main の archive.jsonl に spec が無いので、Job へは heart が env で渡す
        "spec": record.get("spec") or {},
    }
    if record.get("status") == DISPATCHED:
        project["job"] = record.get("job") or ""
        project["spawn_count"] = 1
        # gate は Job 収集の外側で Job を作る。折り込んだ最初のビートでは
        # 「走っているはずの Job が観測に無い」が必ず成立するので、
        # reconcile が乖離と誤読しないための起点を残す
        project["job_created_at"] = now_iso(now)
    else:
        project["state"] = "stalled"
        project["stalled_reason"] = record.get("reason") or record.get("status") or "dispatch_failed"
    return project


def audit_lines(record, now=None):
    """audit.jsonl に足す行。**誰が要求したか (core / heart) を残す**のがここの本題。"""
    lines = [
        {
            "at": record.get("accepted_at") or now_iso(now),
            "action": "dispatch_admit",
            "project": record["project_id"],
            "requested_by": record.get("requested_by", "core"),
            "dispatch_id": record["dispatch_id"],
            "shadow": False,
        }
    ]
    if record.get("status") == DISPATCHED:
        lines.append(
            {
                "at": now_iso(now),
                "action": "spawn_runner",
                "project": record["project_id"],
                "requested_by": record.get("requested_by", "core"),
                "dispatch_id": record["dispatch_id"],
                "job": record.get("job"),
                "shadow": False,
            }
        )
    else:
        lines.append(
            {
                "at": now_iso(now),
                "action": "dispatch_aborted",
                "project": record["project_id"],
                "requested_by": record.get("requested_by", "core"),
                "dispatch_id": record["dispatch_id"],
                "reason": record.get("reason") or record.get("status"),
                "shadow": False,
            }
        )
    return lines


def dumps(record):
    return json.dumps(record, ensure_ascii=False)
