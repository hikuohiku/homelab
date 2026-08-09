"""runner — プロジェクト Job 内の wrapper。

「長命なのはプロジェクトであってセッションではない」(決定 #2) の実装。
毎セッション フレッシュな `claude -p` を起動し、文脈の実体は
PROJECT.md / progress.md / git log に置く。

wrapper がプロンプトに依存せず強制するもの (決定 #5〜#10 のハーネス側):
  - 開始前: verify 全項目が fail であることの実測確認 (未完了の仕事を
    「完了済み」から始めさせない)
  - 毎セッション後: verify 実行 (完成宣言は wrapper の実測のみが下す)
  - 予算: usage 累積がソフト上限を超えたら checkpoint 最終セッション → 終了
  - 無活動: stream イベントが途絶えたセッションを kill
  - transcript: 生 stream-json を PVC に tee (git には持ち出さない)

モード (RUNNER_MODE): worker | review | curriculum | consolidation | critic | chore
consolidation / critic / chore の spawn 配線は Phase 3 (heart 側 reconcile の拡張) で
有効化する。モード自体はここで先に実装しておく。
"""

import collections
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ops.heart.gh import Gh, GhError  # noqa: E402

# --- セッションの死因 (P-0026) ---
# 待機の既定値はここ (モジュール定数) に置く。ops/rules.json は人間レビュー必須パスの
# 単一情報源なので、この程度の調整のために触らない (既存キーは読むだけ)。
DEFAULT_QUOTA_WAIT_SECONDS = 900
MIN_QUOTA_WAIT_SECONDS = 60
QUOTA_WAIT_MARGIN_SECONDS = 30
STDERR_TAIL_CHARS = 2000
STDERR_KEEP_LINES = 400

# 判定順が意味を持つ (429 系は文言が重なるので上限に寄せる):
# usage_limit > auth > network > unknown
FAILURE_PATTERNS = (
    ("usage_limit", (
        r"claude ai usage limit reached",
        r"usage limit",
        r"5-hour limit",
        r"limit reached ∙ resets",
        r"rate_limit_error",
        r"429[^\n]{0,120}rate limit",
        r"rate limit[^\n]{0,120}429",
    )),
    ("auth", (
        r"invalid api key",
        r"authentication_error",
        r"oauth token",
        r"please run /login",
        r"\b401\b",
        r"unauthorized",
        r"forbidden",
    )),
    ("network", (
        r"enotfound",
        r"econnrefused",
        r"etimedout",
        r"econnreset",
        r"socket hang up",
        r"fetch failed",
        r"getaddrinfo",
    )),
)

# stderr_tail に混ざりうる秘密。(a) 環境変数の実値そのものの literal 置換が最も確実で、
# (b) 正規表現は env に無い経路 (git remote の埋め込み等) の保険。
SECRET_ENV_KEYS = (
    "AUTOPILOT_GITHUB_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
)
SECRET_PATTERNS = (
    (r"ghp_[A-Za-z0-9]{8,}", "***"),
    (r"github_pat_[A-Za-z0-9_]{8,}", "***"),
    (r"sk-ant-[A-Za-z0-9\-_]{8,}", "***"),
    (r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***"),
    (r"x-access-token:[^@\s]+", "x-access-token:***"),
)


def classify_session_failure(stderr_tail):
    """claude セッションの死因を 'usage_limit'|'auth'|'network'|'unknown' に分類する純関数。

    上限の実文字列はこのリポジトリに残っていない (runner が stderr を DEVNULL で
    捨ててきたのが P-0026 の発端で、journal にも記録が無い)。下の表は claude CLI の
    既知の出力形を根拠にした候補であり、**別の文言を観測したらその回の result.json の
    `stderr_tail` を証拠に表とテストへ追記する** — そのための stderr_tail である。
    知らない文字列を勝手に分類しないこと (既定は 'unknown')。
    """
    text = (stderr_tail or "").lower()
    if not text.strip():
        return "unknown"
    for kind, patterns in FAILURE_PATTERNS:
        for pat in patterns:
            if re.search(pat, text):
                return kind
    return "unknown"


def parse_usage_limit_reset(text):
    """上限メッセージから reset 時刻 (aware datetime) を取り出す。取れなければ None。

    claude CLI は `Claude AI usage limit reached|<epoch 秒>` の形で付けることがある。
    分類 (classify_session_failure) と時刻抽出は混ぜない。
    """
    if not text:
        return None
    m = re.search(r"limit reached\s*\|\s*(\d{9,13})", text, re.IGNORECASE)
    if not m:
        return None
    value = int(m.group(1))
    if value > 10_000_000_000:  # ミリ秒表記
        value //= 1000
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def mask_secrets(text, env=None):
    """stderr に混ざった秘密を潰す純関数。env を引数で受けるのはテストのため。"""
    if not text:
        return ""
    env = os.environ if env is None else env
    for key in SECRET_ENV_KEYS:
        value = (env.get(key) or "").strip()
        if len(value) >= 8:
            text = text.replace(value, "***")
    for pat, repl in SECRET_PATTERNS:
        text = re.sub(pat, repl, text)
    return text


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[runner] {now_iso()} {msg}", flush=True)


def sh(args, cwd=None, check=True, timeout=300):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args[:3])}...: rc={p.returncode} {p.stderr.strip()[:300]}"
        )
    return p


class Budget:
    def __init__(self, soft_cap_tokens, max_sessions):
        self.soft_cap = soft_cap_tokens
        self.max_sessions = max_sessions
        self.used_tokens = 0
        self.used_cost = 0.0
        self.sessions = 0

    def exhausted(self):
        return self.used_tokens >= self.soft_cap or self.sessions >= self.max_sessions

    def snapshot(self):
        return {
            "used_tokens": self.used_tokens,
            "used_cost_usd": round(self.used_cost, 4),
            "sessions": self.sessions,
            "soft_cap": self.soft_cap,
        }


class Session:
    """1 回のフレッシュ claude -p。stream-json を transcript に tee しつつ
    無活動を監視する。"""

    def __init__(self, prompt, model, transcript_path, rules, cwd):
        self.prompt = prompt
        self.model = model
        self.transcript = transcript_path
        self.rules = rules
        self.cwd = cwd

    def run(self):
        nudge = self.rules["runner"]["inactivity_nudge_seconds"]
        kill_after = self.rules["runner"]["inactivity_kill_seconds"]
        max_seconds = self.rules["runner"]["session_max_seconds"]
        self.transcript.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "claude", "-p",
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json", "--verbose",
            "--model", self.model,
            self.prompt,
        ]
        proc = subprocess.Popen(
            cmd, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        q = queue.Queue()
        # 死因の唯一の証拠。読み捨てずに末尾だけ保持する (放置すると 64KB のパイプ
        # バッファが埋まって claude 側が書き込みでブロックする)。deque.append は
        # スレッドセーフ
        err_tail = collections.deque(maxlen=STDERR_KEEP_LINES)
        result_errors = []

        def reader():
            for line in proc.stdout:
                q.put(line)
            q.put(None)

        def err_reader():
            # **stderr の到着で last_event を更新しない。** 活動の定義は今まで通り
            # stdout イベント。更新すると進捗の無い警告だけで無活動 kill が
            # 永久に発火しなくなる
            for line in proc.stderr:
                err_tail.append(line)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        et = threading.Thread(target=err_reader, daemon=True)
        et.start()

        started = time.time()
        last_event = started
        nudged = False
        usage = {"tokens": 0, "cost": 0.0}
        outcome = "completed"
        with open(self.transcript, "a") as out:
            while True:
                if time.time() - started > max_seconds:
                    outcome = "session_timeout"
                    proc.kill()
                    break
                try:
                    line = q.get(timeout=5)
                except queue.Empty:
                    idle = time.time() - last_event
                    if idle > kill_after:
                        outcome = "inactive_killed"
                        proc.kill()
                        break
                    if idle > nudge and not nudged:
                        log(f"session idle {int(idle)}s (nudge threshold)")
                        nudged = True
                    continue
                if line is None:
                    break
                last_event = time.time()
                out.write(line)
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "result":
                    usage["cost"] += float(ev.get("total_cost_usd") or 0.0)
                    u = ev.get("usage") or {}
                    usage["tokens"] += int(u.get("input_tokens") or 0) + int(
                        u.get("output_tokens") or 0
                    )
                    # 上限メッセージが stderr でなく result イベント側に出る CLI 版が
                    # ありうるので、エラーの回だけ本文も分類の入力に混ぜる。
                    # 成功した回の `result` (= 最終アシスタント本文) は拾わない —
                    # 本文が上限の話題に触れているだけで誤分類する
                    if ev.get("is_error") or (ev.get("subtype") or "success") != "success":
                        for key in ("subtype", "error", "result"):
                            v = ev.get(key)
                            if isinstance(v, str) and v:
                                result_errors.append(v)
        proc.wait(timeout=60)
        # 診断が本体を止めないよう、合流は短い timeout 付き。待ち切れなければ
        # その時点の deque の中身を使う
        et.join(timeout=5)
        # 認証エラー等で即死したセッションを completed 扱いにしない
        # (実体の無いセッションを予算いっぱい繰り返す — レビュー指摘 [14])
        if outcome == "completed" and proc.returncode != 0:
            outcome = "error"
        info = {"failure_kind": None, "stderr_tail": "", "reset_at": None}
        if outcome != "completed":
            blob = "".join(err_tail) + "\n" + "\n".join(result_errors)
            info["failure_kind"] = classify_session_failure(blob)
            # マスクは 2000 文字に切る「前」に掛ける (切ってから掛けると
            # 途中で切れた秘密が生き残る)
            info["stderr_tail"] = mask_secrets(blob)[-STDERR_TAIL_CHARS:]
            if info["failure_kind"] == "usage_limit":
                reset = parse_usage_limit_reset(blob)
                if reset:
                    info["reset_at"] = reset.strftime("%Y-%m-%dT%H:%M:%SZ")
        # usage が取れない CLI バージョンでも予算が空回りしないよう、
        # トークン不明のセッションは概算で数える (コスト非ゼロなら換算、ゼロなら定数)。
        # ただし上限で即死した回は実消費ゼロなので概算を付けない — 付けると
        # 待って再開する前に soft cap が尽きる (待機中に予算が溶ける)
        if usage["tokens"] == 0 and info["failure_kind"] != "usage_limit":
            usage["tokens"] = int(usage["cost"] / 0.000008) if usage["cost"] else 50_000
        return outcome, usage, info


class Runner:
    def __init__(self):
        self.mode = os.environ.get("RUNNER_MODE", "worker")
        self.project_id = os.environ.get("PROJECT_ID", "system")
        self.branch = os.environ.get("PROJECT_BRANCH", "")
        self.model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
        self.repo = os.environ.get("GITHUB_REPO", "hikuohiku/homelab")
        self.repo_dir = REPO_ROOT
        self.data = Path(os.environ.get("HEART_DATA_DIR", "/data"))
        self.project_dir = self.data / "projects" / self.project_id
        self.project_dir.mkdir(parents=True, exist_ok=True)
        # プロジェクトの永続文脈 (PROJECT.md / PROGRESS.md) はリポジトリ直下でなく
        # プロジェクト別ディレクトリに置く。直下に置くと PR 経由で main に混入し、
        # 次のプロジェクトのブランチに前プロジェクトの文脈が残って initializer が
        # スキップされる (P-0004 の初回実走 #406 で発覚)
        self.doc_dir = self.repo_dir / "ops" / "projects" / "logs" / self.project_id
        self.project_md = self.doc_dir / "PROJECT.md"
        self.progress_md = self.doc_dir / "PROGRESS.md"
        with open(self.repo_dir / "ops" / "rules.json") as f:
            self.rules = json.load(f)
        self.gh = Gh(os.environ.get("AUTOPILOT_GITHUB_TOKEN", ""), self.repo)
        self.last_session = {}
        self.trust_workspace()
        self.spec = self.load_spec()
        soft_cap = (self.spec.get("budget") or {}).get(
            "soft_cap_tokens", self.rules["runner"]["default_soft_cap_tokens"]
        )
        self.budget = Budget(soft_cap, self.rules["runner"]["max_sessions_per_project"])

    # --- 共通部品 ---
    def trust_workspace(self):
        """loop.sh の trust_workspace() と同じ理由 (未 trust だと repo 側
        .claude/settings.json の permissions.allow が無視される実測)。"""
        home = Path(os.environ.get("HOME", "/work/home"))
        home.mkdir(parents=True, exist_ok=True)
        path = home / ".claude.json"
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            cfg = {}
        cfg.setdefault("projects", {}).setdefault(str(self.repo_dir), {})[
            "hasTrustDialogAccepted"
        ] = True
        path.write_text(json.dumps(cfg))

    def load_spec(self):
        """main の ops/projects/archive.jsonl から自分の採択 spec を読む。
        (main に固定されたものが唯一の仕様 — runner ブランチからは改竄不能)"""
        path = self.repo_dir / "ops" / "projects" / "archive.jsonl"
        if not path.exists():
            return {}
        spec = {}
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("id") == self.project_id and rec.get("adopted"):
                    spec = rec  # 後の行 (最新) を優先
        return spec

    def prompt_text(self, name, extra=None, from_main=False):
        """from_main=True は origin/main に固定された内容を読む (reviewer 用)。
        project ブランチの作業ツリーから読むと、worker がブランチ上で審査基準
        (reviewer.md) を書き換えられてしまう (レビュー指摘 [10])。"""
        if from_main:
            p = sh(
                ["git", "show", f"origin/main:ops/prompts/{name}.md"],
                cwd=self.repo_dir,
            )
            text = p.stdout
        else:
            path = self.repo_dir / "ops" / "prompts" / f"{name}.md"
            text = path.read_text()
        subst = {
            "PROJECT_ID": self.project_id,
            "PROJECT_BRANCH": self.branch,
            "SPEC_JSON": json.dumps(self.spec, ensure_ascii=False, indent=2),
            "PROJECT_FILE": str(self.project_md.relative_to(self.repo_dir)),
            "PROGRESS_FILE": str(self.progress_md.relative_to(self.repo_dir)),
        }
        subst.update(extra or {})
        for k, v in subst.items():
            text = text.replace("{{" + k + "}}", str(v))
        return text

    def transcript_path(self, tag):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
        return (
            self.data / "transcripts" / self.mode
            / f"{ts}-{self.project_id.lower()}-{tag}.jsonl"
        )

    def run_session(self, prompt, tag, cwd=None):
        self.budget.sessions += 1
        outcome, usage, info = Session(
            prompt, self.model, self.transcript_path(tag), self.rules,
            cwd or self.workdir(),
        ).run()
        # 呼び出し側 5 箇所の署名を変えずに死因を渡す
        self.last_session = info
        self.budget.used_tokens += usage["tokens"]
        self.budget.used_cost += usage["cost"]
        kind = f" kind={info['failure_kind']}" if info.get("failure_kind") else ""
        log(
            f"session {tag}: {outcome}{kind} tokens+={usage['tokens']} "
            f"total={self.budget.used_tokens}/{self.budget.soft_cap}"
        )
        return outcome

    def failure_fields(self):
        """異常終了系の write_result に載せる死因。"""
        info = self.last_session or {}
        return {
            "failure_kind": info.get("failure_kind"),
            "stderr_tail": info.get("stderr_tail", ""),
        }

    def quota_wait_seconds(self):
        """上限が明けるまでの待機秒。reset 時刻が取れなければ既定値。"""
        reset_at = (self.last_session or {}).get("reset_at")
        if reset_at:
            try:
                dt = datetime.strptime(reset_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                dt = None
            if dt:
                delta = (dt - datetime.now(timezone.utc)).total_seconds()
                return int(max(MIN_QUOTA_WAIT_SECONDS, delta + QUOTA_WAIT_MARGIN_SECONDS))
        return DEFAULT_QUOTA_WAIT_SECONDS

    def write_result(self, state, **kw):
        doc = {"state": state, "at": now_iso(), "budget": self.budget.snapshot()}
        doc.update(kw)
        with open(self.project_dir / "result.json", "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        log(f"result: {state}")

    # --- worker ---
    def workdir(self):
        return self.repo_dir

    def checkout_branch(self):
        sh(["git", "fetch", "--quiet", "origin"], cwd=self.repo_dir)
        remote = sh(
            ["git", "ls-remote", "--heads", "origin", self.branch],
            cwd=self.repo_dir, check=False,
        ).stdout.strip()
        if remote:
            sh(["git", "checkout", "--quiet", "-B", self.branch,
                f"origin/{self.branch}"], cwd=self.repo_dir)
        else:
            sh(["git", "checkout", "--quiet", "-B", self.branch, "origin/main"],
               cwd=self.repo_dir)
            sh(["git", "push", "--quiet", "-u", "origin", self.branch],
               cwd=self.repo_dir)

    def run_verify(self):
        """spec の verify コマンド列を実行して [{cmd, ok, output}] を返す。"""
        results = []
        for cmd in self.spec.get("verify", []):
            try:
                p = subprocess.run(
                    ["bash", "-c", cmd], cwd=self.repo_dir, capture_output=True,
                    text=True, timeout=600,
                )
                results.append(
                    {"cmd": cmd, "ok": p.returncode == 0,
                     "output": (p.stdout + p.stderr)[-2000:]}
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                results.append({"cmd": cmd, "ok": False, "output": str(e)[:500]})
        return results

    def push_if_committed(self):
        sh(["git", "push", "--quiet", "origin", f"HEAD:{self.branch}"],
           cwd=self.repo_dir, check=False)

    def ensure_pr(self):
        """このブランチの open PR を返す (無ければ作る)。"""
        owner = self.repo.split("/")[0]
        prs = self.gh.request(
            "GET",
            f"/repos/{self.repo}/pulls?state=open&head={owner}:{self.branch}&per_page=100",
        )
        if prs:
            return prs[0]["number"]
        title = f"{self.project_id}: {self.spec.get('title', self.branch)}"
        body = (
            f"heart-and-projects の runner が作成。\n\n"
            f"- project: {self.project_id}\n"
            f"- spec: ops/projects/archive.jsonl の {self.project_id}\n"
            f"- 検証: wrapper が verify 全項目 green を実測済み。"
            f"独立レビュー (reviewer Job) が再実測してから merge される\n"
        )
        pr = self.gh.request(
            "POST", f"/repos/{self.repo}/pulls",
            {"title": title, "head": self.branch, "base": "main", "body": body},
        )
        return pr["number"]

    def mode_worker(self):
        if not self.branch or not self.spec:
            self.write_result("spec_error",
                              error="PROJECT_BRANCH または archive.jsonl の spec が無い")
            return 1
        self.checkout_branch()

        progress = self.progress_md
        first_time = not self.project_md.exists()
        verify = self.run_verify()
        if first_time:
            if any(v["ok"] for v in verify):
                # 始める前から通っている受入基準は「基準になっていない」。
                # spec の作り直しを要求する (plan 検証 #5)
                self.write_result(
                    "spec_error",
                    error="開始前に verify が pass している",
                    verify=verify,
                )
                return 1
            outcome = self.run_session(
                self.prompt_text("initializer"), "s0-init"
            )
            if outcome != "completed":
                self.write_result(
                    "error", error=f"initializer: {outcome}", **self.failure_fields()
                )
                return 1
            self.push_if_committed()

        consecutive_inactive = 0
        consecutive_error = 0
        # この runner プロセスが上限待ちに使ってよい総量。Job の生存時間
        # (activeDeadlineSeconds) より session_max_seconds の方が先に効くので、
        # これを待機予算の上限として読む
        quota_wait_budget = self.rules["runner"]["session_max_seconds"]
        quota_waited = 0
        # レビュー差し戻し (findings) 付きで起動された場合、verify が全 green のままでも
        # 最低 1 セッションは findings 対応を回す。これが無いと品質理由の fail
        # (verify green のまま) に一度も対処せず即 ready_for_review を再宣言してしまう
        # (レビュー指摘 [2])
        findings = os.environ.get("REVIEW_FINDINGS", "")
        findings_pending = bool(findings.strip())
        while True:
            verify = self.run_verify()
            (self.project_dir / "verify.json").write_text(
                json.dumps({"at": now_iso(), "results": verify}, ensure_ascii=False)
            )
            if verify and all(v["ok"] for v in verify) and not findings_pending:
                self.push_if_committed()
                pr = self.ensure_pr()
                self.write_result("ready_for_review", pr=pr, verify=verify)
                return 0
            if self.budget.exhausted():
                self.run_session(self.prompt_text("checkpoint"), "checkpoint")
                self.push_if_committed()
                self.write_result("budget_exhausted", verify=verify)
                return 0
            extra = {
                "PROGRESS_TAIL": progress.read_text()[-4000:] if progress.exists() else "",
                "GIT_LOG": sh(["git", "log", "--oneline", "-20"],
                              cwd=self.repo_dir).stdout,
                "VERIFY_STATUS": json.dumps(verify, ensure_ascii=False)[:4000],
                "REVIEW_FINDINGS": findings,
            }
            outcome = self.run_session(
                self.prompt_text("worker", extra), f"s{self.budget.sessions}"
            )
            self.push_if_committed()
            findings_pending = False
            if (self.last_session or {}).get("failure_kind") == "usage_limit":
                # 上限は器の外側の事実であって停滞ではない。連続エラーに数えず、
                # 明けるまで待って同じセッションを再開する。
                # 沈黙は禁物 — heartbeat ログが唯一の外からの観測経路
                wait = self.quota_wait_seconds()
                remaining = quota_wait_budget - quota_waited
                reset_at = (self.last_session or {}).get("reset_at")
                if wait > remaining:
                    resume_after = (
                        datetime.now(timezone.utc) + timedelta(seconds=wait)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    log(
                        f"usage_limit: 待機 {wait}s が残り予算 {remaining}s を超える。"
                        f"waiting_quota で終える (resume_after={resume_after})"
                    )
                    self.write_result(
                        "waiting_quota", resume_after=resume_after, verify=verify,
                        **self.failure_fields(),
                    )
                    return 0
                log(
                    f"usage_limit: {wait}s 待機して再開する "
                    f"(reset_at={reset_at}, 待機予算 {quota_waited + wait}/{quota_wait_budget}s)"
                )
                quota_waited += wait
                time.sleep(wait)
            elif outcome == "inactive_killed":
                consecutive_inactive += 1
                if consecutive_inactive >= 2:
                    self.write_result("stalled_inactive", **self.failure_fields())
                    return 1
            elif outcome == "error":
                consecutive_error += 1
                if consecutive_error >= 3:
                    self.write_result(
                        "error",
                        error=(
                            "claude セッションが 3 回連続で異常終了 "
                            f"(failure_kind={self.failure_fields()['failure_kind']})"
                        ),
                        **self.failure_fields(),
                    )
                    return 1
            else:
                consecutive_inactive = 0
                consecutive_error = 0

    # --- review ---
    def mode_review(self):
        if not self.branch or not self.spec:
            self.write_result("error", error="review: branch/spec が無い")
            return 1
        # クリーン checkout: runner の作業ツリーを信用せず、origin から取り直す
        self.checkout_branch()
        verify = self.run_verify()
        diff = sh(
            ["git", "diff", f"origin/main...origin/{self.branch}", "--stat"],
            cwd=self.repo_dir, check=False,
        ).stdout[:3000]
        extra = {
            "VERIFY_STATUS": json.dumps(verify, ensure_ascii=False)[:6000],
            "DIFF_STAT": diff,
        }
        outcome = self.run_session(
            self.prompt_text("reviewer", extra, from_main=True), "review"
        )
        review_path = self.project_dir / "review.json"
        verdict = {"verdict": "fail", "findings": ["reviewer セッションが verdict を書かなかった"]}
        if review_path.exists():
            try:
                verdict = json.loads(review_path.read_text())
            except ValueError:
                pass
        # 機械強制: wrapper の実測で verify が全 green でなければ、
        # レビューアが何と言おうと pass にしない (自己申告の排除は reviewer にも適用)
        if not (verify and all(v["ok"] for v in verify)):
            verdict["verdict"] = "fail"
            verdict.setdefault("findings", []).append(
                "wrapper 実測で verify が green でない"
            )
        verdict["probes"] = {"verify": verify, "session_outcome": outcome}
        review_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
        log(f"review verdict: {verdict['verdict']}")
        return 0

    # --- curriculum (生成 → 判定の 2 段) ---
    def mode_curriculum(self):
        gen_out = Path("/work/proposals.json")
        judge_out = Path("/work/adopted.json")
        extra = {"PROPOSALS_PATH": str(gen_out), "ADOPTED_PATH": str(judge_out)}
        outcome = self.run_session(
            self.prompt_text("curriculum-generate", extra), "cur-gen"
        )
        if outcome != "completed" or not gen_out.exists():
            self.write_result(
                "error", error=f"curriculum generate: {outcome}",
                **self.failure_fields(),
            )
            return 1
        judge_model = None
        with open(self.repo_dir / "ops" / "models.json") as f:
            judge_model = json.load(f)["roles"]["curriculum_judge"]
        self.model = judge_model
        extra["PROPOSALS_JSON"] = gen_out.read_text()[:20000]
        outcome = self.run_session(
            self.prompt_text("curriculum-judge", extra), "cur-judge"
        )
        if outcome != "completed" or not judge_out.exists():
            self.write_result(
                "error", error=f"curriculum judge: {outcome}", **self.failure_fields()
            )
            return 1
        try:
            proposals = json.loads(gen_out.read_text())
            adopted = json.loads(judge_out.read_text())
        except ValueError as e:
            self.write_result("error", error=f"curriculum output parse: {e}")
            return 1
        pr = self.fix_to_archive(proposals, adopted)
        self.write_result(
            "curriculum_done", pr=pr,
            adopted=[a.get("id") for a in adopted.get("adopted", [])],
        )
        return 0

    def fix_to_archive(self, proposals, adopted):
        """全案 (採択・棄却) を archive.jsonl に追記する PR を作る。
        採択 spec は adopted: true で固定され、runner はこれしか信用しない。"""
        date = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"heart/curriculum-{date}"
        sh(["git", "checkout", "--quiet", "-B", branch, "origin/main"],
           cwd=self.repo_dir)
        path = self.repo_dir / "ops" / "projects" / "archive.jsonl"
        adopted_ids = {a.get("id") for a in adopted.get("adopted", [])}
        with open(path, "a") as f:
            for p in proposals.get("proposals", []):
                rec = dict(p)
                rec["adopted"] = p.get("id") in adopted_ids
                rec["proposed_at"] = now_iso()
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sh(["git", "add", str(path)], cwd=self.repo_dir)
        sh(["git", "commit", "--quiet", "-m",
            f"curriculum: {len(proposals.get('proposals', []))} 案 "
            f"(採択 {len(adopted_ids)})"], cwd=self.repo_dir)
        sh(["git", "push", "--quiet", "-u", "origin", branch], cwd=self.repo_dir)
        pr = self.gh.request(
            "POST", f"/repos/{self.repo}/pulls",
            {"title": f"curriculum: プロジェクト立案 {date}",
             "head": branch, "base": "main",
             "body": "curriculum Job による立案。採択 spec の固定 (heart が CI green で merge し、"
                     "merge 後に projects.json へ登録・予告する)。\n\n全案 (棄却含む) を "
                     "ops/projects/archive.jsonl に追記。"},
        )
        return pr["number"]

    # --- 単発モード (Phase 3 で spawn 配線) ---
    def mode_oneshot(self, prompt_name):
        outcome = self.run_session(self.prompt_text(prompt_name), prompt_name)
        if outcome == "completed":
            self.write_result("done", outcome=outcome)
            return 0
        self.write_result("error", outcome=outcome, **self.failure_fields())
        return 1

    def run(self):
        log(
            f"mode={self.mode} project={self.project_id} branch={self.branch} "
            f"model={self.model}"
        )
        if self.mode == "worker":
            return self.mode_worker()
        if self.mode == "review":
            return self.mode_review()
        if self.mode == "curriculum":
            return self.mode_curriculum()
        if self.mode in ("consolidation", "critic", "chore"):
            return self.mode_oneshot(self.mode)
        log(f"unknown mode {self.mode}")
        return 1


def main():
    sys.exit(Runner().run())


if __name__ == "__main__":
    main()
