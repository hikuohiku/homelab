#!/bin/sh
# autopilot の常駐ループ。
#
# これまで autopilot は claude.ai のクラウド定期実行（毎時 cron）で動いていたが、
#   - homelab（Tailscale 内）に到達できない
#   - 毎回コールドスタートで起動オーバーヘッドが大きい
#   - 走っているのか止まっているのかが外から分からない
# の 3 点が問題だった。クラスタ内の常駐 Deployment に移し、`claude -p` を
# ループで回すことでいずれも解消する（VISION 段階 3「常駐基盤を自前に持つ」）。
#
# このスクリプト自体は「回し続けること」だけに責任を持つ。何をするかの判断は
# 一切書かない（prompt.md → ops/VISION.md → ops/CHARTER.md に委ねる）。
#
# 注意: ConfigMap は disableNameSuffixHash: true のため、内容を変えても Deployment は
# 自動で再起動しない。ただし下記 reexec_if_updated がイテレーションの合間に自分の
# digest を見て変更を検知し、Pod を作り直さずに新しい内容で exec し直す
# （導入した commit 2f92db4 時点で 1 回だけ Pod を作り直したのは、この仕組み自体を
# 載せるため。以降の loop.sh 変更はこの仕組みで反映される）。
# prompt.md は毎イテレーション読み直すので、そもそも再読み込みの仕組みが要らない。

set -u

WORKDIR="${WORKDIR:-/work}"
REPO_URL="${REPO_URL:-https://github.com/hikuohiku/homelab.git}"
REPO_DIR="${REPO_DIR:-${WORKDIR}/homelab}"
PROMPT_PLAN="${PROMPT_PLAN:-/config/prompt-plan.md}"
PROMPT_EXECUTE="${PROMPT_EXECUTE:-/config/prompt-execute.md}"
PROMPT_REVIEW_USER="${PROMPT_REVIEW_USER:-/config/prompt-review-user.md}"
PROMPT_REVIEW_ARCH="${PROMPT_REVIEW_ARCH:-/config/prompt-review-arch.md}"
# 何回に 1 回は着手可能なタスクがあっても計画回にする。優先順位が古びるのと、
# inbox が溜まりっぱなしになるのを防ぐため。
PLAN_EVERY="${PLAN_EVERY:-6}"
# 何回に 1 回をレビュー回にするか。計画役は backlog に載っているものを捌き、実行役は
# 渡されたものを作るだけで、「そもそも今あるものが良いか」を誰も見ていなかった（CHARTER §3）。
# 12 の根拠（実測イテレーション時間・レビュー間隔の計算）は ops/CHARTER.md §3 の REVIEW_EVERY 節を参照。
# 0 を入れるとレビュー回を止められる
REVIEW_EVERY="${REVIEW_EVERY:-12}"
# レビューのレンズを交互にするための呼び出し回数。emptyDir 上に置くので loop.sh の
# 再 exec では失われず、Pod の作り直しでリセットされる（リセットしても利用者視点から
# 再開するだけで、どちらかのレンズが永久に回らなくなることはない）
REVIEW_COUNT_FILE="${REVIEW_COUNT_FILE:-${WORKDIR}/.review-count}"
# 1 イテレーションの間隔。短すぎると PR の CI 待ちに対して空回りが増える
INTERVAL_SECONDS="${INTERVAL_SECONDS:-120}"
# 1 イテレーションの上限。エージェントが 1 周を終えられずに居座ると、
# 次の周回が永久に来ない（= 止まったのと同じ）。上限で必ず切って次へ行く
ITERATION_TIMEOUT_SECONDS="${ITERATION_TIMEOUT_SECONDS:-3600}"

# 心拍。イテレーションの開始/終了を stdout に出しておくと、`kubectl logs` だけで
# 「いま何周目の何をしているか」が外から分かる（旧クラウド cron に無かったもの）
log() {
  echo "[autopilot] $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
}

# SIGTERM（Deployment の更新・ノードの排出）で速やかに降りる。走行中の
# イテレーションは中断されるが、CHARTER §2 のとおり引き継ぎは origin 側の
# PR / autopilot-* ブランチ経由なので、途中で落ちても次の Pod が拾える
trap 'log "received SIGTERM, stopping after current step"; exit 0' TERM INT

trust_workspace() {
  # claude は未 trust のワークスペースでは repo 側 .claude/settings.json の
  # permissions.allow を無視する（起動直後のログで実際に 18 件が無視された）。
  # Pod には trust dialog に答える人間がいないので、受理済みとして書いておく。
  mkdir -p "${HOME}"
  CLAUDE_JSON="${HOME}/.claude.json" REPO="${REPO_DIR}" python3 - <<'EOF'
import json
import os

path = os.environ["CLAUDE_JSON"]
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {}
cfg.setdefault("projects", {}).setdefault(os.environ["REPO"], {})["hasTrustDialogAccepted"] = True
with open(path, "w") as f:
    json.dump(cfg, f)
EOF
}

setup_git() {
  # トークンを remote URL に埋め込むと .git/config に平文で残り、`git remote -v` や
  # エラーメッセージに載りうる。credential helper 経由なら環境変数のまま渡せる
  git config --global credential."https://github.com".helper \
    '!f() { printf "username=x-access-token\npassword=%s\n" "${AUTOPILOT_GITHUB_TOKEN}"; }; f'
  git config --global credential."https://github.com".username x-access-token
  # 著者情報は env (GIT_AUTHOR_* / GIT_COMMITTER_*) から渡す。ここでは設定しない
}

# 1 イテレーション: リポジトリを最新化 → claude -p を実行 → 終了コードを返す
# 早期 return の理由は FAIL_REASON に書いて main() のログ行に載せる（T-0158）。
# 「claude -p 自体の異常」と「repo 同期前段での早期return」を外形（heartbeat の
# exit_code のみ）から区別できず、外から『止まっている』のと見分けがつかなかったため
iterate() {
  FAIL_REASON=""

  if [ ! -d "${REPO_DIR}/.git" ]; then
    log "cloning ${REPO_URL} into ${REPO_DIR}"
    git clone --quiet "${REPO_URL}" "${REPO_DIR}" || { FAIL_REASON="git clone failed"; return 1; }
  fi

  cd "${REPO_DIR}" || { FAIL_REASON="cd to repo failed"; return 1; }

  # 前回のイテレーションの作業ブランチ・未コミットの残骸は捨てて main の最新から始める。
  # 中断の引き継ぎは origin 側（オープン PR と autopilot/* ブランチ）から拾う設計
  # なので、ローカルに残す意味が無い（CHARTER §2）
  git fetch --prune --quiet origin || { FAIL_REASON="git fetch failed"; return 1; }
  git checkout --quiet -B main origin/main || { FAIL_REASON="git checkout failed"; return 1; }
  git reset --hard --quiet origin/main || { FAIL_REASON="git reset failed"; return 1; }
  git clean -fdq || { FAIL_REASON="git clean failed"; return 1; }

  # 役の選択。REVIEW_EVERY 回に 1 回はレビュー役、着手可能なタスクが無いか
  # PLAN_EVERY 回に 1 回は計画役、それ以外は実行役。
  # 役を分けているのは、何をやる価値があるかを決める主体とそれをやる主体が同じだと
  # 「やりやすいこと」に寄っていくため（CHARTER §3）。レビュー役も同じ理由で実装しない。
  # not_before（ISO8601, T-0133）が未来のタスクは「時刻待ち」として actionable から除く。
  # 値が無い/過去/壊れている場合は今まで通り actionable 扱い（壊れた値で計画役に
  # 倒れ続けると気づけないため、安全側＝実行役に倒す）
  actionable=$(python3 - <<'EOF'
import json
from datetime import datetime, timezone

try:
    with open("ops/backlog.json") as f:
        tasks = json.load(f)["tasks"]
except (OSError, ValueError, KeyError):
    # 読めないときは実行役に倒す（計画役は repo を変更しないので、
    # 壊れた状態で計画役に入ると何も進まないまま回り続ける）
    print(1)
else:
    now = datetime.now(timezone.utc)
    count = 0
    for t in tasks:
        if t.get("status") not in ("todo", "in_progress"):
            continue
        not_before = t.get("not_before")
        if not_before:
            try:
                gate = datetime.fromisoformat(str(not_before).replace("Z", "+00:00"))
                if gate.tzinfo is None:
                    gate = gate.replace(tzinfo=timezone.utc)
                if gate > now:
                    continue
            except ValueError:
                pass
        count += 1
    print(count)
EOF
)
  lens=""
  # レビュー回は「計画回の直前」に置く（REVIEW_EVERY は PLAN_EVERY の倍数を想定）。
  # レビュー役は ops/inbox.md に指摘を落とすだけなので、直後が計画回でないと指摘が
  # 何周も寝かされる。着手可能タスクが 0 件でもレビューを優先するのは、backlog が
  # 枯れているときこそ「見えている範囲を使い切った」状態でレビューの出番だから
  if [ "${REVIEW_EVERY}" -gt 0 ] && [ $((i % REVIEW_EVERY)) -eq $((REVIEW_EVERY - 1)) ]; then
    n="$(cat "${REVIEW_COUNT_FILE}" 2>/dev/null)"
    case "${n}" in "" | *[!0-9]*) n=0 ;; esac
    echo $((n + 1)) >"${REVIEW_COUNT_FILE}" 2>/dev/null || true
    role=review
    if [ $((n % 2)) -eq 0 ]; then
      lens=user; PROMPT_FILE="${PROMPT_REVIEW_USER}"
    else
      lens=arch; PROMPT_FILE="${PROMPT_REVIEW_ARCH}"
    fi
  elif [ "${actionable}" -eq 0 ] || [ $((i % PLAN_EVERY)) -eq 0 ]; then
    role=plan; PROMPT_FILE="${PROMPT_PLAN}"
  else
    role=execute; PROMPT_FILE="${PROMPT_EXECUTE}"
  fi
  log "role=${role}${lens:+ lens=${lens}} actionable=${actionable}"

  PROMPT="$(cat "${PROMPT_FILE}")" || { FAIL_REASON="failed to read prompt file"; return 1; }

  # timeout の -k は「猶予後に SIGKILL」。GNU coreutils と busybox の双方で使える書式。
  # 上限に達した場合の終了コードは 124
  # headless では既定で権限確認が要る。Pod は read-only RBAC の非 root コンテナで、
  # 確認に応じられる人間もいないので bypass する（CHARTER §5.1 の「止まるくらいなら
  # 最初から確認を出さない」と同じ理由）。
  set -o pipefail
  timeout -k 30 "${ITERATION_TIMEOUT_SECONDS}" \
    claude -p --permission-mode bypassPermissions \
      --output-format stream-json --verbose "${PROMPT}" \
    | python3 /config/render.py
  rc=$?
  set +o pipefail
  return "${rc}"
}

# ConfigMap は disableNameSuffixHash: true なので、loop.sh を書き換えても Deployment の
# spec は変わらず Pod は作り直されない。kubelet が /config を更新したあと、自分で
# 拾い直せるようにする。prompt-*.md が再起動なしで効くのと同じ性質を loop.sh 自身にも持たせる。
SELF="${SELF:-/config/loop.sh}"
self_digest() { md5sum "${SELF}" 2>/dev/null | cut -d" " -f1; }
SELF_DIGEST="$(self_digest)"

reexec_if_updated() {
  now="$(self_digest)"
  if [ -n "${now}" ] && [ "${now}" != "${SELF_DIGEST}" ]; then
    log "loop.sh が更新された（${SELF_DIGEST} -> ${now}）。exec し直す"
    exec bash "${SELF}"
  fi
}

main() {
  # HOME は emptyDir 配下を指す（deployment.yaml 参照）。git/npm/claude が
  # ホームに書けるよう、先に実体を作っておく
  mkdir -p "${WORKDIR}" "${HOME}"
  setup_git
  trust_workspace

  log "started (interval=${INTERVAL_SECONDS}s timeout=${ITERATION_TIMEOUT_SECONDS}s repo=${REPO_URL})"

  i=0
  while true; do
    # イテレーションの途中では入れ替えない
    reexec_if_updated
    i=$((i + 1))
    started_at="$(date -u '+%s')"
    log "iteration #${i} start"

    iterate
    rc=$?

    elapsed=$(($(date -u '+%s') - started_at))
    if [ "${rc}" -eq 124 ]; then
      log "iteration #${i} end exit=124 (timed out after ${ITERATION_TIMEOUT_SECONDS}s) elapsed=${elapsed}s"
    elif [ "${rc}" -ne 0 ] && [ -n "${FAIL_REASON:-}" ]; then
      log "iteration #${i} end exit=${rc} (${FAIL_REASON}) elapsed=${elapsed}s"
    else
      log "iteration #${i} end exit=${rc} elapsed=${elapsed}s"
    fi

    # 失敗しても Pod は落とさない。CrashLoopBackOff で再起動間隔が伸びていくより、
    # 同じ間隔で回り続けて次の周回に賭ける方がループが止まらない（VISION）
    # sleep をバックグラウンドにして wait で待つ。前面で sleep すると sh は
    # それが終わるまで trap を実行しないため、待機中の SIGTERM に反応できず
    # terminationGracePeriodSeconds を待たずに SIGKILL される
    sleep "${INTERVAL_SECONDS}" &
    wait $!
  done
}

main "$@"
