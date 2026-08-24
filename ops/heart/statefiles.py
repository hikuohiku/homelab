"""状態ファイルの読み書きと検証。

書き手は heart だけ (単一書き手)。置き場は 2 つあり、同じクラスで扱う:

  - ops-state ブランチの checkout — projects.json / heartbeat.json。
    外から見える状態。push 前に必ず validate_projects() を通す。main の CI からは
    見えないので、ここでの検証が唯一のゲート。壊れた状態を push すると次のビートの
    自分が読めなくなる — 検証は自分を守るためにある
  - PVC の作業ディレクトリ — WORK_FILES。heart しか読まないので git に出さない
    (設計 state-out-of-git Phase 3)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# git を経由しない作業ファイル。heart だけが読み書きし、誰も外から読み戻さない
# (設計 state-out-of-git Phase 3)。消えて困るのは「未送信の Discord」「受理済みの
# 依頼」「フィードバックの取り込み位置」程度で、記録ではない
WORK_FILES = (
    "outbox.jsonl",
    "sent.jsonl",
    "task-requests.jsonl",
    "briefing-queue.jsonl",
    "commands.jsonl",
    "audit.jsonl",
    "cursors.json",
    "trust.json",
)


def migrate_plan(src_names, dst_names):
    """ops-state から PVC へ移す作業ファイルを決める (純関数)。

    返り値は (コピーするもの, ops-state から消すもの)。PVC に既にあるものは
    **上書きしない** — 移行後に heart が書いた方が正。それでも ops-state 側は
    消す: push に失敗して残った古い写しを置いておくと、次の誰かが正と取り違える。

    どちらも空になったら移行は済んでいる (以後このビートは何もしない)。
    """
    src, dst = set(src_names), set(dst_names)
    remove = [n for n in WORK_FILES if n in src]
    return [n for n in remove if n not in dst], remove


PROJECT_STATES = (
    "proposed",
    "announced",
    "active",
    "in_review",
    "merging",
    "soaking",
    "delivered",
    "stalled",
    "vetoed",
    # 採択されなかった案 (設計 state-out-of-git「棄却された案も CR にする」)。
    # **状態機械には一度も入らない**入り口専用の終端で、reject_reason /
    # improve_hint を次の立案へ返すためだけに存在する。projects.json には
    # 載せない (載せると 250 件超の墓標が毎ビート git を往復する) — 置き場は
    # Project CR だけ
    "rejected",
)
# 終端 = decide() の状態機械が触らないもの。rejected をここに入れるのが
# 「棄却案が一斉に着手される」ことへの唯一の歯止め (reconcile.py の
# `if state in TERMINAL_STATES: continue`)
TERMINAL_STATES = ("delivered", "stalled", "vetoed", "rejected")
CHORE_STATES = ("queued", "running", "done", "failed")

# 必須フィールドは **state で変えない**。CRD の required は spec 直下の静的な
# 一覧で、state ごとに変えるには CEL (x-kubernetes-validations) が要る。
# 棄却案は branch も created も持たないが、そこに既定値 (branch は空文字、
# created は proposed_at の日付) を入れるほうが、スキーマを条件分岐させるより
# 壊れ方が読みやすい。空の branch が通るのは終端だけ — validate_projects の
# 「非終端は project/ で始まること」がそのまま歯止めになっている
REQUIRED_PROJECT_FIELDS = (
    "id",
    "title",
    "state",
    "branch",
    "irreversible",
    "capabilities",
    "budget",
    "created",
)


def now_iso(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def validate_projects(doc):
    """不変条件を検査してエラー文字列のリストを返す (空 = OK)。"""
    errors = []
    if not isinstance(doc, dict) or "projects" not in doc:
        return ["projects.json: トップレベルに projects が無い"]
    seen = set()
    for p in doc["projects"]:
        pid = p.get("id", "?")
        for field in REQUIRED_PROJECT_FIELDS:
            if field not in p:
                errors.append(f"{pid}: {field} が無い")
        if pid in seen:
            errors.append(f"{pid}: id 重複")
        seen.add(pid)
        if p.get("state") not in PROJECT_STATES:
            errors.append(f"{pid}: state が不正: {p.get('state')}")
        if p.get("state") not in TERMINAL_STATES and not p.get("branch", "").startswith(
            "project/"
        ):
            errors.append(f"{pid}: branch は project/ で始めること: {p.get('branch')}")
        budget = p.get("budget", {})
        if not isinstance(budget.get("used_tokens", 0), int):
            errors.append(f"{pid}: budget.used_tokens が int でない")
        prs = p.get("prs", [])
        if not (isinstance(prs, list) and all(isinstance(n, int) for n in prs)):
            errors.append(f"{pid}: prs が int の配列でない")
    for c in doc.get("chores", []):
        if c.get("state") not in CHORE_STATES:
            errors.append(f"chore {c.get('id', '?')}: state が不正")
    return errors


class StateFiles:
    def __init__(self, state_dir):
        self.dir = Path(state_dir)

    def _load_json(self, name, default):
        path = self.dir / name
        if not path.exists():
            return default
        with open(path) as f:
            return json.load(f)

    def _save_json(self, name, doc):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")

    # --- projects.json ---
    def load_projects(self):
        return self._load_json(
            "projects.json", {"version": 1, "projects": [], "chores": []}
        )

    def save_projects(self, doc):
        errors = validate_projects(doc)
        if errors:
            raise ValueError("projects.json validation: " + "; ".join(errors))
        self._save_json("projects.json", doc)

    # --- trust.json ---
    def load_trust(self):
        return self._load_json(
            "trust.json",
            {
                "version": 1,
                "veto_drill_passed": False,
                "_comment": "veto_drill_passed は人間が実際に veto を 1 回通した疎通実績。"
                "これが false の間はいかなる窓短縮もしない (rules.json veto._comment)",
            },
        )

    def save_trust(self, doc):
        self._save_json("trust.json", doc)

    # --- cursors.json (フィードバック取り込みの位置) ---
    def load_cursors(self):
        return self._load_json(
            "cursors.json", {"issue_comments_since": None, "seen_feedback_files": []}
        )

    def save_cursors(self, doc):
        self._save_json("cursors.json", doc)

    # --- 追記ログ (metrics / audit / outbox) ---
    def append_jsonl(self, name, record):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_jsonl(self, name):
        path = self.dir / name
        if not path.exists():
            return []
        records = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
        return records

    def rewrite_jsonl(self, name, records):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # --- heartbeat ---
    def write_heartbeat(self, beat, now=None, usage=None):
        """生存と、当日の使用量。

        usage を載せるのはダッシュボードのため。metrics.jsonl が git から
        出た (設計 state-out-of-git Phase 1) 後、ダッシュボードが要る指標は
        これだけなので、既に毎ビート書いている heartbeat に相乗りさせる。
        """
        doc = {"beat": beat, "at": now_iso(now), "writer": "heart"}
        if usage is not None:
            doc["usage"] = usage
        self._save_json("heartbeat.json", doc)
