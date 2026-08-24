"""admission gate — コアが同期で着手を要求する口 (設計 rev3 Phase D)。

## なぜ在るか

所有者の依頼が runner に届くまで 2026-08-24 の実測で 1 時間 38 分かかっていた。
支配項は LLM の思考時間ではなく **Job 起動・PR・CI・ビート周期という待ち**で、
そのうち「コアが起票してから heart が拾うまで」はコードで消せる。

コアに k8s の write 権限は渡さない (設計 D29)。**判定と Job 作成は heart のまま**で、
コアはその判定を HTTP で同期に呼べるようになるだけ。判定に使う不変条件
(stop_engaged / max_concurrent / capability の宣言連鎖) は reconcile.admit() の
純関数で、遷移表テストが仕様として効き続ける。

## 経路と到達範囲

  core (MCP サイドカー) --HTTP--> autopilot-heart.autopilot.svc:8099 /dispatch

  - **cluster 内のみ**。ClusterIP Service だけで、Ingress も Tailscale も通さない
  - **認証を持たない**。同一 namespace の NetworkPolicy で送信元を
    app=autopilot-core の Pod に限る (apps/autopilot/heart-service.yaml)。
    トークンを置かないのは、置けば ops/rules.json の
    allowed_autopilot_doppler_keys に鍵を足すことになり、そこは人間レビュー必須の
    パスだから — 認証を足すなら人間の判断を通す
  - 開けている口は POST /dispatch と GET /healthz の 2 つだけ。Job の種類も
    モデルも SA も指定できない (引数に無い)

## 非同期にしてあるところ

Job 作成 (k8s API) は同期呼び出しの中でやらない。コアには「受理した」を即座に
返し、結末は /data/dispatch/inbox/<id>.json に落として次のビートが
projects.json に取り込む。コアは同じ内容で dispatch_task を呼び直せば現在の
扱いを聞ける (冪等)。

## 採択ゲートを通さない (2026-08-24, 所有者判断)

以前は着手の前に新品 clone で verify を実測していた。**外した** — verify を書くのも
LLM なので迂回でき、機械の判定として意味を成さないため。dispatch 経路に残る機械の
ゲートは CI と soak だけで、完成の判断は reviewer とコアが担う
(ops/heart/README.md「dispatch 経路で失われる保証」)。curriculum 由来の spec に
対する採択ゲートはそのまま残っている。

## スレッドと単一書き手

heart は ops-state の単一書き手であり続ける。gate スレッドは **git を触らない** —
書くのは /data (PVC) の inbox と台帳だけで、projects.json / audit.jsonl への
反映は必ずビート側が行う (audit.jsonl も PVC に居るが、書き手はビートだけ)。gate が直に作るのは k8s Job だけで、それは
決定論的な名前 + 409 冪等 (spawn.create) なので二重に作れない。
"""

import json
import queue
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import dispatch, reconcile, spawn
from .statefiles import now_iso, parse_iso

DEFAULT_LISTEN = "0.0.0.0:8099"
MAX_REQUEST_BYTES = 64 * 1024
# 台帳から読み戻すレート制限用の窓 (受理時刻)。窓より十分長い分だけ覚えていればよい
RECENT_KEEP = 200


def _utcnow():
    return datetime.now(timezone.utc)


class AdmissionGate:
    """判定と Job 作成を持つ本体。HTTP は薄い殻 (下の handler)。"""

    def __init__(self, cfg_provider, k8s_provider, data_dir, repo_url,
                 create_job=None, now=None):
        self._cfg = cfg_provider
        self._k8s = k8s_provider
        self.data_dir = Path(data_dir)
        self.repo_url = repo_url
        self._create_job = create_job or spawn.create
        self._now = now or _utcnow

        self.lock = threading.Lock()
        self.snapshot = None
        self.inflight = {}  # dispatch_id -> project_id (受理済み・未登録)
        self.allocated = set()  # 払い出し済みの P-9NNN
        self.recent = []  # 受理時刻 (ISO)
        self.queue = queue.Queue()
        self._server = None
        self._load_ledger()

    # --- 台帳 (プロセスが落ちても受理の事実を失わない) ---

    @property
    def dispatch_dir(self):
        return self.data_dir / dispatch.DISPATCH_DIR

    @property
    def ledger_path(self):
        return self.dispatch_dir / dispatch.LEDGER_FILE

    def _load_ledger(self):
        try:
            lines = self.ledger_path.read_text().splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("project_id"):
                self.allocated.add(rec["project_id"])
            if rec.get("event") == "accepted" and rec.get("at"):
                self.recent.append(rec["at"])
        self.recent = self.recent[-RECENT_KEEP:]

    def _append_ledger(self, entry):
        self.dispatch_dir.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _write_inbox(self, record):
        inbox = self.dispatch_dir / dispatch.INBOX
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{record['dispatch_id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(dispatch.dumps(record))
        tmp.replace(path)  # 半端な JSON を facts に読ませない

    # --- ビートとの受け渡し ---

    def update(self, doc, rules, now, shadow):
        """ビートの終わりに heart が呼ぶ。判定に使う状態の写しを差し替える。

        写しの鮮度そのものが安全装置になっている (reconcile.admit の state_stale)。
        ビートが詰まればゲートは自動的に閉じる。
        """
        dispatch_ids = {
            p["dispatch_id"]: p["id"]
            for p in doc["projects"]
            if p.get("dispatch_id")
        }
        running = sum(
            1 for p in doc["projects"]
            if p["state"] in ("active", "in_review", "merging")
        )
        with self.lock:
            self.snapshot = {
                "at": now_iso(now),
                "stop_engaged": bool(doc.get("stop_engaged")),
                # doc の全体にかかる値で、Project CR には載らない (CR は 1 件 1
                # プロジェクト)。読み手 (コアの shadow) が git を読まずに済むよう
                # /healthz に出す — 設計 state-out-of-git 4b-2a
                "last_curriculum_at": doc.get("last_curriculum_at") or "",
                "shadow": bool(shadow),
                "running": running,
                "max_concurrent": rules["runner"]["max_concurrent"],
                "dispatch_ids": dispatch_ids,
            }
            self.allocated |= {p["id"] for p in doc["projects"]}
            # 登録が済んだものは inflight から落とす。ここを落とし忘れると
            # 走行数を二重に数えて、空きがあるのに永久に capacity で断る
            for did in list(self.inflight):
                if did in dispatch_ids:
                    self.inflight.pop(did, None)

    # --- 判定 (同期・ミリ秒) ---

    def dispatch_request(self, payload):
        """POST /dispatch の本体。(HTTP status, 応答 dict) を返す。"""
        now = self._now()
        rules = self._cfg().rules
        with self.lock:
            snapshot = dict(self.snapshot) if self.snapshot else None
            inflight = set(self.inflight)
            recent = list(self.recent)
        verdict = reconcile.admit(
            payload, snapshot, rules, now, inflight=inflight, recent=recent
        )
        if verdict["status"] != reconcile.ADMIT_ACCEPTED:
            # duplicate は失敗ではない。冪等に畳んだという応答なので 200 で返す
            code = 200 if verdict["status"] == reconcile.ADMIT_DUPLICATE else 409
            return code, verdict

        title, body = dispatch.normalize(payload)
        with self.lock:
            used = set(self.allocated) | set(self.inflight.values())
            if snapshot:
                used |= set(snapshot.get("dispatch_ids", {}).values())
            pid = dispatch.allocate_project_id(used)
            if pid is None:
                return 409, {
                    "status": reconcile.ADMIT_DENIED, "reason": "no_project_id",
                    "message": "即時 dispatch のプロジェクト id 空間が尽きています",
                    "dispatch_id": verdict["dispatch_id"],
                }
            record = dispatch.new_record(title, body, pid, now)
            self.allocated.add(pid)
            self.inflight[record["dispatch_id"]] = pid
            self.recent = (self.recent + [now_iso(now)])[-RECENT_KEEP:]
        self._append_ledger(dispatch.ledger_entry(record, "accepted", now))
        self.queue.put(record)
        return 202, {
            "status": reconcile.ADMIT_ACCEPTED,
            "reason": "",
            "message": f"受理しました ({pid})。runner Job を起動します",
            "dispatch_id": record["dispatch_id"],
            "project_id": pid,
        }

    def health(self):
        with self.lock:
            snapshot = dict(self.snapshot) if self.snapshot else None
            inflight = len(self.inflight)
        age = None
        if snapshot:
            age = int((self._now() - parse_iso(snapshot["at"])).total_seconds())
        return {
            "ok": snapshot is not None,
            "snapshot_age_seconds": age,
            "inflight": inflight,
            # doc 全体にかかる状態。プロジェクト 1 件ずつの Project CR には
            # 載らないので、CR を読む側 (コア) がここから引く。写しが無い
            # (起動直後) ときは既定値で、ok=False が「まだ判断材料が無い」を示す
            "stop_engaged": bool(snapshot and snapshot.get("stop_engaged")),
            "last_curriculum_at": (snapshot or {}).get("last_curriculum_at", ""),
        }

    # --- Job 作成 (非同期) ---

    def run_one(self, record):
        """1 件ぶんの Job 作成 → inbox への書き出し。"""
        try:
            # 受理から Job 作成までの間に「止めて」が来ていないか見直す
            with self.lock:
                stopped = bool(self.snapshot and self.snapshot.get("stop_engaged"))
            if stopped:
                record["status"] = dispatch.ABORTED
                record["reason"] = "human_stop"
                record["detail"] = "Job を作る前に全停止が指示されました"
            else:
                record["job"] = self._create_job(
                    self._k8s(), self._cfg(), "runner",
                    project={
                        "id": record["project_id"],
                        # **空で固定する** — spawn.build_job はここに
                        # kubectl-write があるときだけ writer SA を注入する。
                        # 即時 dispatch は capability を名乗れない (admit が弾く)
                        "capabilities": [],
                        "branch": f"project/{record['project_id'].lower()}",
                    },
                    attempt=1,
                    # main の archive.jsonl に spec が無いので env で渡す。
                    # runner.load_spec() がこれを読む
                    extra_env={"HEART_SPEC_JSON": json.dumps(
                        record["spec"], ensure_ascii=False)},
                )
                record["status"] = dispatch.DISPATCHED
        except Exception as e:  # noqa: BLE001 — 何が起きても inbox に理由を残す
            record["status"] = dispatch.ABORTED
            record["reason"] = "spawn_error"
            record["detail"] = str(e)[:300]
        self._write_inbox(record)
        self._append_ledger(dispatch.ledger_entry(
            record, record["status"], self._now(),
            reason=record.get("reason"), job=record.get("job"),
        ))
        return record

    def _worker(self):
        while True:
            record = self.queue.get()
            if record is None:
                return
            try:
                self.run_one(record)
            except Exception as e:  # noqa: BLE001 — worker を死なせない
                print(f"[autopilot] dispatch worker error: {e}", flush=True)
            finally:
                self.queue.task_done()

    # --- 起動 ---

    def start(self, listen):
        """HTTP と worker を daemon スレッドで起こす。失敗しても heart は止めない。"""
        threading.Thread(target=self._worker, daemon=True, name="dispatch-worker").start()
        host, _, port = listen.rpartition(":")
        self._server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), _handler(self))
        self._server.daemon_threads = True
        threading.Thread(
            target=self._server.serve_forever, daemon=True, name="dispatch-http"
        ).start()
        return self._server


def _handler(gate):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # アクセスログは heart の書式に寄せる
            print(f"[autopilot] {now_iso()} gate {fmt % args}", flush=True)

        def _send(self, code, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler の規約
            if self.path.rstrip("/") == "/healthz":
                self._send(200, gate.health())
                return
            self._send(404, {"message": "そんな口は開けていない"})

        def do_POST(self):  # noqa: N802
            if self.path.rstrip("/") != "/dispatch":
                self._send(404, {"message": "そんな口は開けていない"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_REQUEST_BYTES:
                self._send(413, {"message": "要求が大きすぎる"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError as e:
                self._send(400, {"message": f"JSON を解釈できない: {e}"})
                return
            if not isinstance(payload, dict):
                self._send(400, {"message": "JSON オブジェクトを送ること"})
                return
            try:
                code, body = gate.dispatch_request(payload)
            except Exception as e:  # noqa: BLE001 — 500 を返してでも heart は生かす
                self._send(500, {"message": f"gate の内部エラー: {e}"})
                return
            self._send(code, body)

    return Handler


__all__ = ["AdmissionGate", "DEFAULT_LISTEN"]
