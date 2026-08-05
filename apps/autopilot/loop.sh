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
# 注意: このファイルは Pod 起動時に 1 度だけ読み込まれる（ConfigMap の
# disableNameSuffixHash: true のため、内容を変えても Deployment は自動で
# 再起動しない）。loop.sh の変更を反映するには Pod の再作成が要る。
# 一方 prompt.md は毎イテレーション読み直すので再起動なしで効く。

set -u

WORKDIR="${WORKDIR:-/work}"
REPO_URL="${REPO_URL:-https://github.com/hikuohiku/homelab.git}"
REPO_DIR="${REPO_DIR:-${WORKDIR}/homelab}"
PROMPT_FILE="${PROMPT_FILE:-/config/prompt.md}"
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

setup_git() {
  # トークンを remote URL に埋め込むと .git/config に平文で残り、`git remote -v` や
  # エラーメッセージに載りうる。credential helper 経由なら環境変数のまま渡せる
  git config --global credential."https://github.com".helper \
    '!f() { printf "username=x-access-token\npassword=%s\n" "${AUTOPILOT_GITHUB_TOKEN}"; }; f'
  git config --global credential."https://github.com".username x-access-token
  # 著者情報は env (GIT_AUTHOR_* / GIT_COMMITTER_*) から渡す。ここでは設定しない
}

# 1 イテレーション: リポジトリを最新化 → claude -p を実行 → 終了コードを返す
iterate() {
  if [ ! -d "${REPO_DIR}/.git" ]; then
    log "cloning ${REPO_URL} into ${REPO_DIR}"
    git clone --quiet "${REPO_URL}" "${REPO_DIR}" || return 1
  fi

  cd "${REPO_DIR}" || return 1

  # 前回のイテレーションの作業ブランチ・未コミットの残骸は捨てて main の最新から始める。
  # 中断の引き継ぎは origin 側（オープン PR と autopilot/* ブランチ）から拾う設計
  # なので、ローカルに残す意味が無い（CHARTER §2）
  git fetch --prune --quiet origin || return 1
  git checkout --quiet -B main origin/main || return 1
  git reset --hard --quiet origin/main || return 1
  git clean -fdq || return 1

  PROMPT="$(cat "${PROMPT_FILE}")" || return 1

  # timeout の -k は「猶予後に SIGKILL」。GNU coreutils と busybox の双方で使える書式。
  # 上限に達した場合の終了コードは 124
  # headless では既定で権限確認が要る。Pod は read-only RBAC の非 root コンテナで、
  # 確認に応じられる人間もいないので bypass する（CHARTER §5.1 の「止まるくらいなら
  # 最初から確認を出さない」と同じ理由）。
  timeout -k 30 "${ITERATION_TIMEOUT_SECONDS}" \
    claude -p --permission-mode bypassPermissions "${PROMPT}"
}

main() {
  # HOME は emptyDir 配下を指す（deployment.yaml 参照）。git/npm/claude が
  # ホームに書けるよう、先に実体を作っておく
  mkdir -p "${WORKDIR}" "${HOME}"
  setup_git

  log "started (interval=${INTERVAL_SECONDS}s timeout=${ITERATION_TIMEOUT_SECONDS}s repo=${REPO_URL})"

  i=0
  while true; do
    i=$((i + 1))
    started_at="$(date -u '+%s')"
    log "iteration #${i} start"

    iterate
    rc=$?

    elapsed=$(($(date -u '+%s') - started_at))
    if [ "${rc}" -eq 124 ]; then
      log "iteration #${i} end exit=124 (timed out after ${ITERATION_TIMEOUT_SECONDS}s) elapsed=${elapsed}s"
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
