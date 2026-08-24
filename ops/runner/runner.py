"""runner — プロジェクト Job 内の wrapper。

「長命なのはプロジェクトであってセッションではない」(決定 #2) の実装。
毎セッション フレッシュな `claude -p` を起動し、文脈の実体は
PROJECT.md / progress.md / git log に置く。

wrapper がプロンプトに依存せず強制するもの (決定 #5〜#10 のハーネス側):
  - 開始前: verify 全項目が fail であることの実測確認 (未完了の仕事を
    「完了済み」から始めさせない)
  - 毎セッション後: verify 実行 (完成宣言は wrapper の実測のみが下す)
  - **verify を持たない spec (dispatch 由来。2026-08-24 の所有者判断) では
    上の 2 つは効かない。** セッションが 1 度正常に終わった時点で PR を出し、
    完成の判断は reviewer とコアの確認に移る。PR が無ければ heart が
    no_pr_reported で止める (PR は機械が確認できる事実なので緩めない)
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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ops.heart.gh import Gh  # noqa: E402

# --- セッションの死因 (P-0026) ---
# 待機の既定値はここ (モジュール定数) に置く。ops/rules.json は人間レビュー必須パスの
# 単一情報源なので、この程度の調整のために触らない (既存キーは読むだけ)。
DEFAULT_QUOTA_WAIT_SECONDS = 900
MIN_QUOTA_WAIT_SECONDS = 60
QUOTA_WAIT_MARGIN_SECONDS = 30
STDERR_TAIL_CHARS = 2000

STDERR_KEEP_LINES = 400

# 1 つのセッション枠で見逃す連続異常終了の上限。worker ループの
# 「3 回連続 error で諦める」(mode_worker) と同じ数で、curriculum の 1 フェーズ
# (P-0227) と initializer (P-0278) もこれを共有する。opencode は自前のリトライ
# (~6 回・指数退避, 2026-08-23 transcript 実測) を使い切った上で死ぬので、
# その後の即時再試行は「器レベルの一時的故障」への最後の歯止めになる
SESSION_MAX_CONSECUTIVE_ERRORS = 3
# P-0227 から使われている別名。curriculum 側の呼び名を壊さない
CURRICULUM_MAX_CONSECUTIVE_ERRORS = SESSION_MAX_CONSECUTIVE_ERRORS

# 判定順が意味を持つ (429 系は文言が重なるので上限に寄せる):
# usage_limit > auth > network > unknown
#
# 出典:
#   - claude 分: 2026-08-08 の上限死以前からの既知の出力形に基づく候補 (実文字列は未観測)。
#     models.json は PR 経由で claude に戻せるのでロールバック経路として温存する
#   - opencode 分: P-0101 の実測 (2026-08-22, v1.18.21, 実測原本は
#     ops/tests/fixtures/engine_stderr/)。opencode は死因を stderr に出さず stdout の
#     type=error イベントで流すため、分類入力は consume_stream_event() が抽出した
#     error.data.message になる
#   - opencode の上限死 (HTTP 429) と鍵未設定は UnknownError
#     ("Unexpected server error.") に潰され区別できない → 未観測のパターンは
#     捏造せず unknown に落とす (P-0101, substrate「claude セッション / 利用上限」節)
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
        # "Invalid API key." (statusCode 401) は opencode v1.18.21 実測で確認済み。
        # ただし鍵が「無い」場合は UnknownError になり auth には分類できない
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
        # "Cannot connect to API: Unable to connect. ..." — 接続拒否と DNS 失敗の
        # 2 条件で同一文言を実測 (P-0101)
        r"cannot connect to api",
        # "Provider finish_reason: network_error" — プロバイダとの接続が
        # ストリーム途中で切れた回。実測原本は
        # ops/projects/logs/P-0227/raw-result-20260823T171940Z.json ほか計 5 件。
        # 表に無かった間この文言は unknown に落ち、直後のプローブが偶発的に返した
        # 401 に死因を乗っ取られて auth と記録されていた (鍵は有効だった)
        r"network_error",
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
    """エンジンセッションの死因を 'usage_limit'|'auth'|'network'|'unknown' に分類する純関数。

    claude は死因を stderr に出す (2026-08-09 実測)。opencode は stderr を出さず
    stdout の type=error イベントで流すため、runner は error.data.message を
    分類入力に混ぜる (consume_stream_event, P-0101 実測)。どちらもここには
    「stderr の末尾 + エラーイベントから抽出した本文」が入る。

    opencode v1.18.21 の実測原本は ops/tests/fixtures/engine_stderr/ で、
    ops/tests/test_failure_patterns.py が fixture → 本関数の分類を固定する。
    知らない文字列を勝手に分類しないこと (既定は 'unknown')。新しい文言を
    観測したらその回の result.json の `stderr_tail` を証拠に fixture・表・テストへ
    追記する — そのための stderr_tail である。
    """
    text = (stderr_tail or "").lower()
    if not text.strip():
        return "unknown"
    for kind, patterns in FAILURE_PATTERNS:
        for pat in patterns:
            if re.search(pat, text):
                return kind
    return "unknown"


def _epoch_to_utc(value):
    """epoch 秒 / ミリ秒を aware datetime (UTC) へ。壊れた値は None。"""
    if value > 10_000_000_000:  # ミリ秒表記
        value //= 1000
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# opencode 形の best-effort 解析 (P-0141) のキーワード。本物の上限メッセージの
# 出力形はまだ未観測 (substrate.md, P-0101) — 「reset / retry の語の近くにある
# 時刻」だけを拾う。キーワードなしの数字を拾うと本文中の無関係な数値に誤爆する
_RESET_KEYWORD_RE = r"(?:resets?|retry\s+(?:after|at))"


def parse_usage_limit_reset(text):
    """上限メッセージから reset 時刻 (aware datetime) を取り出す。取れなければ None。

    claude CLI は `Claude AI usage limit reached|<epoch 秒>` の形で付けることがある。
    分類 (classify_session_failure) と時刻抽出は混ぜない。
    opencode 形は未観測のため best-effort (P-0141): reset / retry 語の近くの
    ISO 8601 か epoch を拾う。合致しなければ None を正直に返す (捏造しない)。
    """
    if not text:
        return None
    m = re.search(r"limit reached\s*\|\s*(\d{9,13})", text, re.IGNORECASE)
    if m:
        return _epoch_to_utc(int(m.group(1)))
    # opencode 形 (best-effort・未観測)。ISO 8601 → epoch の順
    m = re.search(
        _RESET_KEYWORD_RE + r"\D{0,24}"
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
        text,
        re.IGNORECASE,
    )
    if m:
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except ValueError:
            dt = None
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    m = re.search(
        _RESET_KEYWORD_RE + r"\D{0,24}(\d{9,13})", text, re.IGNORECASE
    )
    if m:
        return _epoch_to_utc(int(m.group(1)))
    return None


# --- unknown 死の直後の死因プローブ (P-0141) ---
# opencode は HTTP 429 / 鍵未設定をどちらも UnknownError に潰すため、
# 「上限死が unknown に落ちる」経路が実在する (substrate.md 2026-08-22 実測)。
# unknown 死の直後に推論 API へ軽量プローブ (1 リクエスト) を打ち、真の死因を
# 機械的に確定する。endpoint の導出元は models.json 流の model 文字列
# (provider 部) と既存 env OPENCODE_API_KEY のみ — 新しい設定は増やさない。
PROBE_TIMEOUT_SECONDS = 15

# 出典: ops/tests/fixtures/engine_stderr/*.txt の error.data.metadata.url 実測
# (2026-08-22, opencode CLI v1.18.21)。知らない provider の endpoint は捏造せず、
# プローブを打たず unknown を維持する (claude ロールバック経路もここに落ちる)
PROVIDER_PROBE_ENDPOINTS = {
    "opencode-go": "https://opencode.ai/zen/go/v1/chat/completions",
}


def probe_endpoint(model):
    """model 文字列 (provider/model) からプローブ先 URL を導出する純関数。

    知らない provider・provider 部を持たない (claude 形) model は None。
    """
    if not model or "/" not in model:
        return None
    return PROVIDER_PROBE_ENDPOINTS.get(model.split("/", 1)[0])


def probe_failure_kind(http_status):
    """プローブの HTTP status → 既知死因への写像。確定できなければ None (純関数)。

    401 → auth / 429 → usage_limit。それ以外 (200・400・5xx 等) は「API には
    届いたが死因とは言えない」なので None — 呼び出し側は unknown を維持する。
    """
    if http_status == 401:
        return "auth"
    if http_status == 429:
        return "usage_limit"
    return None


def probe_inference_api(model=None, env=None, urlopen=None):
    """推論 API へ 1 リクエスト打って真の死因を確定する。

    戻り値は `(http_status or None, 'auth'|'usage_limit'|'network'|None)`:
      - HTTP 401 → ("auth"), 429 → ("usage_limit") — API からの応答が返った
      - 接続不可 (URLError / OSError) → network
      - それ以外の応答・予期しない例外は `(status, None)`: プローブが死因を
        確定できなかったということしか言えないので unknown を維持する
    リトライ無し・1 リクエスト (spec)。HTTP 層は urlopen 引数で注入可能で、
    テストは network フリーで通る。Authorization ヘッダに実鍵を載せるため、
    この関数は鍵やリクエストをログに出さない。
    """
    env = os.environ if env is None else env
    endpoint = probe_endpoint(model or "")
    key = (env.get("OPENCODE_API_KEY") or "").strip()
    if not endpoint or not key:
        return None, None
    opener = urlopen or urllib.request.urlopen
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {"model": model, "messages": [], "max_tokens": 1}
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": (
                "homelab-runner-probe/1 "
                "(+https://github.com/hikuohiku/homelab)"
            ),
        },
        method="POST",
    )
    try:
        with opener(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as e:
        status = e.code  # 4xx/5xx も「API から応答が返った」証拠
    except (urllib.error.URLError, OSError):
        return None, "network"
    except Exception:
        return None, None
    return status, probe_failure_kind(status)


def build_failure_info(blob, model=None, outcome="error", prober=None):
    """非 completed 終了の死因情報を組み立てる (Session.run から抽出・P-0141)。

    分類が unknown だった回だけプローブを打つ (`outcome="error"` のみ —
    timeout / 無活動 kill はエンジンの報告した死ではないので数えない)。
    プローブが既知死因に寄せられた場合は failure_kind を差し替える。
    **プローブ自体も失敗した場合のみ unknown を維持する** — 捏造しない原則の延長。
    プローブを打った回だけ `probe_status` / `probe_http_status` を載せる
    (キーが無い = プローブ未実施)。
    """
    blob = blob or ""
    info = {"failure_kind": None, "stderr_tail": "", "reset_at": None}
    kind = classify_session_failure(blob)
    if kind == "unknown" and outcome == "error":
        probe_http, probe_kind = (prober or probe_inference_api)(model)
        info["probe_http_status"] = probe_http
        info["probe_status"] = probe_kind
        if probe_kind:
            kind = probe_kind
    info["failure_kind"] = kind
    # マスクは 2000 文字に切る「前」に掛ける (切ってから掛けると
    # 途中で切れた秘密が生き残る)
    info["stderr_tail"] = mask_secrets(blob)[-STDERR_TAIL_CHARS:]
    if kind == "usage_limit":
        reset = parse_usage_limit_reset(blob)
        if reset:
            info["reset_at"] = reset.strftime("%Y-%m-%dT%H:%M:%SZ")
    return info


def should_withhold_review(failure_kind, review_exists):
    """reviewer が上限で死んだ回を「レビュー不合格」に読み替えないための判定 (純関数)。

    verdict=fail を書くと heart は review_cycles を 1 消費して worker に偽の findings を
    渡す。上限が続けば rules.review.max_cycles で review_rejected = ここでもループが
    止まる。reviewer が verdict を書き切っていれば (review.json が在れば) それは有効な
    結果なので、書かずに終えるのは「上限で死に、かつ verdict が無い」回だけ。
    """
    return failure_kind == "usage_limit" and not review_exists


def session_retry_action(outcome, failure_kind, consecutive_errors,
                         max_consecutive=SESSION_MAX_CONSECUTIVE_ERRORS):
    """1 セッション終了直後の次の一手 (純関数)。curriculum と initializer の共有。

    戻り値は 'done' | 'retry' | 'quota_wait' | 'give_up' の 4 値。

    - completed → done
    - usage_limit → quota_wait。上限は器の外側の事実であって停滞ではない
      (P-0026)。何回来ても連続エラーには数えない
    - それ以外の非 completed (auth / network / unknown / session_timeout /
      inactive_killed) → max_consecutive 回目の失敗で give_up、それまでは retry

    P-0278: この判断は元々 curriculum 経路にしか無く、worker の initializer は
    同じ死因で 1 回目に即 error を書いていた。同じ鍵・同じモデルの隣接実行が
    成功している一時的な失敗 (2026-08-24 実測) で、プロジェクトが作業 1 行も
    無いまま stalled になっていた。判断をここに集約して両者で共有する。
    """
    if outcome == "completed":
        return "done"
    if failure_kind == "usage_limit":
        return "quota_wait"
    if consecutive_errors + 1 >= max_consecutive:
        return "give_up"
    return "retry"


def curriculum_next_action(outcome, artifact_exists, failure_kind, consecutive_errors):
    """curriculum の 1 フェーズ (generate/judge) 終了直後の次の一手 (純関数, P-0227)。

    戻り値は 'done' | 'retry' | 'quota_wait' | 'give_up' の 4 値。
    実測の死因 (ops/projects/logs/P-0227/failures.md, 2026-08-23) はプロバイダとの
    接続がストリーム途中で切れた回で、隣接実行は成功している — それなのに
    runner は 1 回死んだだけで Job 全体 (backoffLimit: 0) を落とし、judge フェーズの
    死は生成済み proposals.json を道連れにしていた。発火条件をこの純関数に集約し、
    ops/tests/test_curriculum_resilience.py で固定する。

    - completed + 産物あり → done
    - completed + 産物なし → give_up。セッションは成功を名乗ったのに契約の産物が
      無い回は、もう一度走らせるのが高価なだけで当てがない (2026-08-22 実績:
      「completed なのにファイル無し」は heart の次回 spawn で自然回復した)。
      エンジン死ではないので連続カウンタにも載せない
    - usage_limit → quota_wait。上限は器の外側の事実であり停滞ではない
      (P-0026)。worker と同じく待って同じセッションから再開する —
      curriculum にもこの配線が無く、上限死が即 error になるのが
      PROJECT.md 前提 (a) の未適用箇所
    - 上記以外の非 completed (auth / network / unknown / session_timeout /
      inactive_killed) → consecutive_errors + 1 回目の失敗が上限に達するまで
      retry。未知の死因でも「有界な再試行」は安全 (最悪でも 3 セッション分)
    """
    if outcome == "completed" and not artifact_exists:
        return "give_up"
    return session_retry_action(
        outcome, failure_kind, consecutive_errors,
        CURRICULUM_MAX_CONSECUTIVE_ERRORS,
    )


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


def build_archive_records(proposals, adopted):
    """全案を archive.jsonl の行レコードへ整形する (純粋関数。P-0210)。

    判定役の scores を id で引き、**棄却案だけ** に reject_reason / improve_hint を
    転記する (採択案は触らない)。判定の教師信号が生成に戻る唯一の経路で、
    ここが切れていると生成役は死因を知らず同型再提案を繰り返す。
    scores 側の欠落 (旧契約の出力・判定役の書き忘れ) は転記を飛ばすだけで落とさない —
    案自体は採否にかかわらず archive に残すのがこの関数の責務。
    """
    adopted_ids = {a.get("id") for a in adopted.get("adopted", [])}
    scores_by_id = {
        s.get("id"): s for s in adopted.get("scores", []) if isinstance(s, dict)
    }
    out = []
    for p in proposals.get("proposals", []):
        rec = dict(p)
        rec["adopted"] = p.get("id") in adopted_ids
        score = scores_by_id.get(p.get("id"))
        if not rec["adopted"] and isinstance(score, dict):
            for key in ("reject_reason", "improve_hint"):
                value = score.get(key)
                if isinstance(value, str) and value.strip():
                    rec[key] = value.strip()
        rec["proposed_at"] = now_iso()
        out.append(rec)
    return out


class Budget:
    """消費量の**計測**と、無限ループの最後の歯止め (セッション数) だけを持つ。

    2026-08-24: トークンの soft cap を廃止した。定額移行済みで、消費量を理由に
    仕事を止めるのは実害だけがあった (stalled の最多要因の 1 つ)。
    used_tokens / used_cost_usd は記録として残す。"""

    def __init__(self, max_sessions):
        self.max_sessions = max_sessions
        self.used_tokens = 0
        self.used_cost = 0.0
        self.sessions = 0

    def session_limit_reached(self):
        return self.sessions >= self.max_sessions

    def snapshot(self):
        return {
            "used_tokens": self.used_tokens,
            "used_cost_usd": round(self.used_cost, 4),
            "sessions": self.sessions,
        }


def build_session_cmd(model, prompt):
    """モデル名の形式で思考エンジンを選ぶ (2026-08-22 opencode go 移行)。

    provider/model 形式 (例 opencode-go/ox-alpha-free) なら opencode CLI、
    それ以外 (例 claude-sonnet-5) なら claude CLI。どちらも 1 行 1 JSON の
    イベントを stdout に流すので、transcript と無活動監視は共通で扱える。
    opencode の認証は環境変数 OPENCODE_API_KEY (spawn.py が注入)。
    """
    if "/" in model:
        return ["opencode", "run", "--model", model, "--format", "json", prompt]
    return [
        "claude", "-p",
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        prompt,
    ]


def consume_stream_event(ev, usage, result_errors):
    """1 イベントから usage (tokens/cost) とエラー本文を拾う。両エンジン対応の純関数。

    claude:   type=result に total_cost_usd / usage.input_tokens/output_tokens。
              エラー回は subtype/error/result の本文を分類の入力に混ぜる
    opencode: type=step_finish の part.tokens {input, output} / part.cost。
              type=error は error.data.message (2026-08-22 v1.18.21 実測)
    """
    etype = ev.get("type")
    if etype == "result":
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
    elif etype == "step_finish":
        part = ev.get("part") or {}
        usage["cost"] += float(part.get("cost") or 0.0)
        t = part.get("tokens") or {}
        usage["tokens"] += int(t.get("input") or 0) + int(t.get("output") or 0)
    elif etype == "error":
        err = ev.get("error") or {}
        msg = (err.get("data") or {}).get("message") or err.get("name") or ""
        if msg:
            result_errors.append(str(msg))


class Session:
    """1 回のフレッシュセッション (claude -p または opencode run)。JSON イベントを
    transcript に tee しつつ無活動を監視する。"""

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

        cmd = build_session_cmd(self.model, self.prompt)
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
                consume_stream_event(ev, usage, result_errors)
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
            info = build_failure_info(blob, self.model, outcome=outcome)
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
        self.setup_opencode()
        self.spec = self.load_spec()
        self.budget = Budget(self.rules["runner"]["max_sessions_per_project"])

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

    def setup_opencode(self):
        """opencode は cwd 外の読み書きを external_directory パーミッションで
        auto-reject する (非対話では常に拒否。2026-08-22 実測)。reviewer が /data に
        verdict を書けず全レビューが既定 fail になり、採択 2 件が 3 巡で stalled した
        事故の原因。/data の入出力契約 (review.json / proposals / critic) を claude と
        同一に保つため、グローバル設定で許可する。trust_workspace と同じく起動時 1 回。"""
        cfg_dir = (
            Path(os.environ.get("XDG_CONFIG_HOME")
                 or Path(os.environ.get("HOME", "/work/home")) / ".config")
            / "opencode"
        )
        cfg_dir.mkdir(parents=True, exist_ok=True)
        path = cfg_dir / "opencode.json"
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            cfg = {"$schema": "https://opencode.ai/config.json"}
        cfg.setdefault("permission", {})["external_directory"] = "allow"
        path.write_text(json.dumps(cfg))

    def load_spec(self):
        """自分の採択 spec を読む。読み先は **heart が Job の env に載せた spec** だけ。

        以前は ops-state の projects.json → main の archive.jsonl → env の 3 段
        だったが、状態が git から Project CR へ出た (設計 state-out-of-git 4b-2a)
        ので前の 2 つは畳んだ。**CR を読みに行く形は採らなかった**:

        - worker Job は `autopilot-runner` SA で走り、`automountServiceAccountToken`
          は false (ops/heart/spawn.py)。トークンが無いのは事故ではなく決定 #5 の
          境界そのもので、spec を読むためだけにそこを開けるのは割に合わない
        - env は heart が Job の spec に固定するので、runner のブランチからは
          書き換えられない。**改竄できないという性質は git 経由と同じ**
        - 即時 dispatch は Job 作成が状態の書き込みより先なので、そもそも env が
          唯一確実な経路だった (D32 で既にそう書いてある)

        env が無ければ {} を返す。呼び出し側は spec_error で止まる (静かに
        空の spec で実装を始めない)。
        """
        return self.spec_from_env()

    def spec_from_env(self):
        """heart が Job の env に載せた spec (即時 dispatch 用)。無ければ {}。

        id が食い違うものは受け取らない — 取り違えた spec で実装する方が、
        spec_error で止まるより高くつく。
        """
        raw = os.environ.get("HEART_SPEC_JSON", "").strip()
        if not raw:
            return {}
        try:
            spec = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(spec, dict) or spec.get("id") != self.project_id:
            return {}
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
            f"total={self.budget.used_tokens} "
            f"sessions={self.budget.sessions}/{self.budget.max_sessions}"
        )
        return outcome

    def failure_fields(self):
        """異常終了系の write_result に載せる死因。プローブを打った回は
        その結果 (`probe_status` / `probe_http_status`) も証跡として載せる
        (キーが無い = プローブ未実施)。"""
        info = self.last_session or {}
        fields = {
            "failure_kind": info.get("failure_kind"),
            "stderr_tail": info.get("stderr_tail", ""),
        }
        if "probe_status" in info:
            fields["probe_status"] = info.get("probe_status")
            fields["probe_http_status"] = info.get("probe_http_status")
        return fields

    def hit_usage_limit(self):
        return (self.last_session or {}).get("failure_kind") == "usage_limit"

    def quota_wait_or_yield(self, waited, budget, **result_kw):
        """usage_limit で死んだ回の待機。initializer とループの両方から呼ぶ。

        戻り値は `(累積待機秒, rc)`。rc が None なら待機し終えたので同じセッションを
        再試行してよい。rc が int なら `waiting_quota` を書き終えているので、その rc で
        プロセスを終える (heart が resume_after まで待って runner を出し直す)。
        沈黙は禁物 — heartbeat ログが唯一の外からの観測経路なので必ず log() する。
        """
        wait = self.quota_wait_seconds()
        remaining = budget - waited
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
                "waiting_quota", resume_after=resume_after,
                **result_kw, **self.failure_fields(),
            )
            return waited, 0
        log(
            f"usage_limit: {wait}s 待機して再開する "
            f"(reset_at={reset_at}, 待機予算 {waited + wait}/{budget}s)"
        )
        time.sleep(wait)
        return waited + wait, None

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
            f"- spec: Project CR {self.project_id.lower()} (autopilot ns)\n"
            + (
                "- 検証: wrapper が verify 全項目 green を実測済み。"
                "独立レビュー (reviewer Job) が再実測してから merge される\n"
                if self.spec.get("verify")
                else "- 検証: この仕様は受入検証を持たない (dispatch 由来)。"
                "完成の判断は独立レビュー (reviewer Job) と CI が担う\n"
            )
        )
        pr = self.gh.request(
            "POST", f"/repos/{self.repo}/pulls",
            {"title": title, "head": self.branch, "base": "main", "body": body},
        )
        return pr["number"]

    def mode_worker(self):
        if not self.branch or not self.spec:
            self.write_result("spec_error",
                              error="PROJECT_BRANCH または HEART_SPEC_JSON が無い")
            return 1
        self.checkout_branch()

        progress = self.progress_md
        first_time = not self.project_md.exists()
        verify = self.run_verify()
        # この runner プロセスが上限待ちに使ってよい総量。Job の生存時間
        # (activeDeadlineSeconds) より session_max_seconds の方が先に効くので、
        # これを待機予算の上限として読む。initializer も同じ財布から待つ
        quota_wait_budget = self.rules["runner"]["session_max_seconds"]
        quota_waited = 0
        if first_time:
            # verify を持たない spec (dispatch 由来) はこのゲートを素通りする。
            # 測る基準が無いことは spec の不良ではない (2026-08-24 の所有者判断)
            if verify and any(v["ok"] for v in verify):
                # 始める前から通っている受入基準は「基準になっていない」。
                # spec の作り直しを要求する (plan 検証 #5)
                self.write_result(
                    "spec_error",
                    error="開始前に verify が pass している",
                    verify=verify,
                )
                return 1
            consecutive_init_error = 0
            while True:
                if self.budget.session_limit_reached():
                    # max_sessions_per_project は無限ループの最後の歯止め
                    # (待機予算とは別軸)。上限リトライでもここは外さない
                    self.write_result("session_limit", verify=verify)
                    return 0
                outcome = self.run_session(
                    self.prompt_text("initializer"), "s0-init"
                )
                kind = (self.last_session or {}).get("failure_kind")
                action = session_retry_action(
                    outcome, kind, consecutive_init_error
                )
                if action == "done":
                    break
                # 沈黙は禁物 — heartbeat ログが唯一の外からの観測経路
                log(f"initializer: outcome={outcome} kind={kind} -> {action}")
                if action == "quota_wait":
                    # **新規プロジェクトの最初のセッションこそ最も上限に当たりやすい。**
                    # ここを stalled + incident のままにすると、本プロジェクトが消しに
                    # 来た症状 (上限を実装詰まりと読み違える) が initializer にだけ
                    # residual として残る (P-0023 / P-0025 の死に方)
                    quota_waited, rc = self.quota_wait_or_yield(
                        quota_waited, quota_wait_budget, verify=verify
                    )
                    if rc is not None:
                        return rc
                    continue
                if action == "give_up":
                    self.write_result(
                        "error",
                        error=(
                            f"initializer: {outcome} が "
                            f"{consecutive_init_error + 1} 回連続 "
                            f"(failure_kind={self.failure_fields()['failure_kind']})"
                        ),
                        **self.failure_fields(),
                    )
                    return 1
                consecutive_init_error += 1
            self.push_if_committed()

        consecutive_inactive = 0
        consecutive_error = 0
        # unknown のまま残った死 (プローブでも死因を確定できなかった回) は、
        # 既知死因の consecutive_error とは別のカウンタで数える (P-0141)。
        # 閾値は rules.json runner.unknown_error_max_rounds (単一情報源)
        consecutive_unknown = 0
        unknown_max = self.rules["runner"]["unknown_error_max_rounds"]
        # レビュー差し戻し (findings) 付きで起動された場合、verify が全 green のままでも
        # 最低 1 セッションは findings 対応を回す。これが無いと品質理由の fail
        # (verify green のまま) に一度も対処せず即 ready_for_review を再宣言してしまう
        # (レビュー指摘 [2])
        findings = os.environ.get("REVIEW_FINDINGS", "")
        findings_pending = bool(findings.strip())
        # verify を持たない spec では「作業を終えた」の唯一の機械可読な合図が
        # セッションの正常終了になる。1 セッション回してから PR を出す
        session_done = False
        while True:
            verify = self.run_verify()
            (self.project_dir / "verify.json").write_text(
                json.dumps({"at": now_iso(), "results": verify}, ensure_ascii=False)
            )
            # 完成の判定。verify があるならその全 green、無いならセッションが
            # 1 度正常に終わったこと (2026-08-24 の所有者判断で dispatch 経路から
            # verify を外した。完成の判断は reviewer とコアが担う)
            done = all(v["ok"] for v in verify) if verify else session_done
            if done and not findings_pending:
                self.push_if_committed()
                pr = self.ensure_pr()
                self.write_result("ready_for_review", pr=pr, verify=verify)
                return 0
            if self.budget.session_limit_reached():
                self.run_session(self.prompt_text("checkpoint"), "checkpoint")
                self.push_if_committed()
                self.write_result("session_limit", verify=verify)
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
            if self.hit_usage_limit():
                # 上限は器の外側の事実であって停滞ではない。連続エラーに数えず、
                # 明けるまで待って同じセッションを再開する
                quota_waited, rc = self.quota_wait_or_yield(
                    quota_waited, quota_wait_budget, verify=verify
                )
                if rc is not None:
                    return rc
            elif outcome == "inactive_killed":
                consecutive_inactive += 1
                if consecutive_inactive >= 2:
                    self.write_result("stalled_inactive", **self.failure_fields())
                    return 1
            elif outcome == "error":
                if (self.last_session or {}).get("failure_kind") == "unknown":
                    # 上限疑いはプローブが usage_limit に寄せているので、ここに
                    # 来るのは「上限か実装詰まりか本当に分からない死」だけ。
                    # 停滞 (stalled) ではなく障害報告の対象 — 閾値を超えたら
                    # heart の既存配線 (result state "error" → incident 型通知)
                    # で人間に渡す。送信経路は新設しない
                    consecutive_unknown += 1
                    if consecutive_unknown >= unknown_max:
                        self.write_result(
                            "error",
                            error=(
                                f"セッションが {consecutive_unknown} 回連続で "
                                "unknown 死 (直後の API プローブでも死因を確定"
                                "できず)。上限か実装詰まりか不明 — 確認してください"
                            ),
                            **self.failure_fields(),
                        )
                        return 1
                else:
                    # 既知の死因に寄せられた回は unknown 連続を数え直す
                    consecutive_unknown = 0
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
                consecutive_unknown = 0
                session_done = True

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
        if should_withhold_review(
            self.failure_fields()["failure_kind"], review_path.exists()
        ):
            # 上限で死んだ回を「レビュー不合格」に読み替えない。ここで verdict=fail を
            # 書くと review_cycles が 1 減り、worker には偽の findings が渡る。
            # 何も書かずに非ゼロで終え、heart 側の既存の見張り
            # (REVIEW_TIMEOUT_HOURS × REVIEW_MAX_RETRIES) の再試行に任せる
            log(
                "usage_limit: reviewer セッションが上限で死んだ。review.json を書かず "
                "非ゼロで終える (heart の reviewer 再試行に任せる)"
            )
            return 1
        verdict = {"verdict": "fail", "findings": ["reviewer セッションが verdict を書かなかった"]}
        if review_path.exists():
            try:
                verdict = json.loads(review_path.read_text())
            except ValueError:
                pass
        # 機械強制: wrapper の実測で verify が全 green でなければ、
        # レビューアが何と言おうと pass にしない (自己申告の排除は reviewer にも適用)。
        # verify を持たない spec (dispatch 由来) にはこの強制が無い — 判断は
        # reviewer と CI に委ねる (2026-08-24 の所有者判断)
        if not all(v["ok"] for v in verify):
            verdict["verdict"] = "fail"
            verdict.setdefault("findings", []).append(
                "wrapper 実測で verify が green でない"
            )
        verdict["probes"] = {"verify": verify, "session_outcome": outcome}
        review_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
        log(f"review verdict: {verdict['verdict']}")
        return 0

    # --- curriculum (生成 → 判定の 2 段) ---
    def run_curriculum_phase(self, prompt_name, tag, extra, artifact):
        """生成/判定の 1 フェーズを、エンジンの瞬間死で Job 全体を殺さずに回す。

        戻り値: None = 成功 / "waiting_quota" = 待機予算を使い切り
        quota_wait_or_yield が waiting_quota を書き終えた (rc 0 で終えてよい) /
        その他の文字列 = 最終 outcome (呼び出し側が既存の error 経路で書く)。

        P-0227: 発火条件は curriculum_next_action (純関数) にあり、テストで
        固定される。judge フェーズの再試行は同じ Pod 内で走るので /work の
        proposals.json が生き続け、生成し直し (20〜30 万トークン) を避けられる。
        沈黙は禁物 — heartbeat ログが唯一の外からの観測経路なので log() する
        """
        consecutive = 0
        waited = 0
        # 待機予算は worker と同じ財布。Job の生存時間より session_max_seconds
        # が先に効く (initializer も同じ財布から待つ)
        wait_budget = self.rules["runner"]["session_max_seconds"]
        while True:
            outcome = self.run_session(self.prompt_text(prompt_name, extra), tag)
            kind = (self.last_session or {}).get("failure_kind")
            action = curriculum_next_action(
                outcome, artifact.exists(), kind, consecutive,
            )
            if action == "done":
                return None
            log(f"phase {tag}: outcome={outcome} kind={kind} -> {action}")
            if action == "quota_wait":
                waited, rc = self.quota_wait_or_yield(waited, wait_budget)
                if rc is not None:
                    return "waiting_quota"
                continue
            if action == "give_up":
                return outcome
            consecutive += 1

    def mode_curriculum(self):
        gen_out = Path("/work/proposals.json")
        judge_out = Path("/work/adopted.json")
        extra = {"PROPOSALS_PATH": str(gen_out), "ADOPTED_PATH": str(judge_out),
                 # 採択上限 = パイプラインの空き (heart が spawn 時に注入)
                 "ADOPT_LIMIT": os.environ.get("ADOPT_LIMIT", "2"),
                 # 人間の未処理タスク依頼 (JSON 配列。heart が spawn 時に注入、
                 # P-0091)。無ければ空配列で置換が常に成立する
                 "TASK_REQUESTS": os.environ.get("TASK_REQUESTS", "[]"),
                 # 過去案の台帳。heart が Project CR から書き出した PVC 上の
                 # jsonl (設計 state-out-of-git 4b-2a)。Job にはクラスタ API の
                 # トークンが無いので、読める形に落として渡してもらう
                 "PROPOSALS_HISTORY": os.environ.get(
                     "PROPOSALS_HISTORY", "/data/curriculum/proposals.jsonl")}
        outcome = self.run_curriculum_phase(
            "curriculum-generate", "cur-gen", extra, gen_out
        )
        if outcome is not None:
            if outcome == "waiting_quota":
                return 0
            # incident 通知の本文に出るのは error フィールドだけなので、死因を本文に含める
            self.write_result(
                "error",
                error=(
                    f"curriculum generate: {outcome} "
                    f"(failure_kind={self.failure_fields()['failure_kind']})"
                ),
                **self.failure_fields(),
            )
            return 1
        judge_model = None
        with open(self.repo_dir / "ops" / "models.json") as f:
            judge_model = json.load(f)["roles"]["curriculum_judge"]
        self.model = judge_model
        extra["PROPOSALS_JSON"] = gen_out.read_text()[:20000]
        outcome = self.run_curriculum_phase(
            "curriculum-judge", "cur-judge", extra, judge_out
        )
        if outcome is not None:
            if outcome == "waiting_quota":
                return 0
            self.write_result(
                "error",
                error=(
                    f"curriculum judge: {outcome} "
                    f"(failure_kind={self.failure_fields()['failure_kind']})"
                ),
                **self.failure_fields(),
            )
            return 1
        try:
            proposals = json.loads(gen_out.read_text())
            adopted = json.loads(judge_out.read_text())
        except ValueError as e:
            self.write_result("error", error=f"curriculum output parse: {e}")
            return 1
        records = build_archive_records(proposals, adopted)
        # 採択 spec は **result.json に載せて heart に直接渡す** (設計 rev3 D32)。
        # heart はこれを ops-state の projects.json に登録し、そこから着手する。
        # 下の PR は台帳 (archive.jsonl) への追記で、着手を待たせない
        adopted_records = [r for r in records if r.get("adopted")]
        pr = self.fix_to_archive(records)
        self.write_result(
            "curriculum_done", pr=pr,
            adopted=[a.get("id") for a in adopted.get("adopted", [])],
            adopted_specs=adopted_records,
        )
        return 0

    def archive_backfill_records(self):
        """heart が env で渡す「まだ台帳に無い採択 spec」(設計 rev3 D32)。

        dispatch の正が ops-state に移ったので、採択は archive.jsonl を待たずに
        動き出す。台帳に欠落を残さないため、動き出した spec は次の curriculum の
        PR に**まとめて**載せる (非同期・バッチ)。

        台帳の検査 (ops/validate.py check_projects_archive) を満たさない行は
        落とす — 1 行でも欠けると CI が赤になり、以後どの案も台帳に載らなくなる。
        """
        raw = os.environ.get("ARCHIVE_BACKFILL_JSON", "").strip()
        if not raw:
            return []
        try:
            specs = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(specs, list):
            return []
        out = []
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            cell = spec.get("cell")
            if not (
                re.match(r"^P-\d{4}$", str(spec.get("id", "")))
                and spec.get("verify")
                and isinstance(cell, list) and len(cell) == 2
                and "irreversible" in spec
            ):
                log(f"archive backfill: 台帳の形を満たさない spec を落とす: {spec.get('id')}")
                continue
            rec = dict(spec)
            rec["adopted"] = True
            rec.setdefault("proposed_at", now_iso())
            out.append(rec)
        return out

    def fix_to_archive(self, records):
        """全案 (採択・棄却) と backfill を archive.jsonl に追記する PR を作る。

        この PR は**台帳**であって、着手の前提ではない (設計 rev3 D32)。
        採択 spec の正は ops-state の projects.json 側にある。
        """
        date = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"heart/curriculum-{date}"
        sh(["git", "checkout", "--quiet", "-B", branch, "origin/main"],
           cwd=self.repo_dir)
        path = self.repo_dir / "ops" / "projects" / "archive.jsonl"
        backfill = self.archive_backfill_records()
        lines = list(records) + backfill
        with open(path, "a") as f:
            for rec in lines:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sh(["git", "add", str(path)], cwd=self.repo_dir)
        sh(["git", "commit", "--quiet", "-m",
            f"curriculum: {len(records)} 案 "
            f"(採択 {sum(1 for r in records if r['adopted'])}"
            + (f", 台帳追記 {len(backfill)}" if backfill else "")
            + ")"], cwd=self.repo_dir)
        sh(["git", "push", "--quiet", "-u", "origin", branch], cwd=self.repo_dir)
        pr = self.gh.request(
            "POST", f"/repos/{self.repo}/pulls",
            {"title": f"curriculum: プロジェクト立案 {date}",
             "head": branch, "base": "main",
             "body": "curriculum Job による立案の**台帳追記**。全案 (棄却含む) を "
                     "ops/projects/archive.jsonl に追記する。\n\n"
                     "着手はこの PR を待たない — 採択 spec は result.json 経由で "
                     "heart が ops-state の projects.json に登録し、そこから "
                     "予告・着手する (設計 rev3 D32)。"},
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
