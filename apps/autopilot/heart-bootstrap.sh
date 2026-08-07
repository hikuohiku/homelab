#!/bin/sh
# heart の起動スクリプト。仕事は「repo を用意して ops/heart に exec する」だけ。
# ロジックは一切持たない — heart 本体 (ops/heart/) は repo 側にあり、PR で変わる。
# heart は毎ビート origin/main に同期し、ops/heart の tree が変わったら自分で
# exec し直す (旧 loop.sh の reexec_if_updated と同じ性質を repo 側で実現)。
set -eu

REPO_URL="${REPO_URL:-https://github.com/hikuohiku/homelab.git}"
REPO_DIR="${REPO_DIR:-/work/repo}"

mkdir -p "${HOME}" "$(dirname "${REPO_DIR}")"

# トークンは credential helper で渡す (.git/config に平文を残さない。loop.sh と同じ)
git config --global credential."https://github.com".helper \
  '!f() { printf "username=x-access-token\npassword=%s\n" "${AUTOPILOT_GITHUB_TOKEN}"; }; f'
git config --global credential."https://github.com".username x-access-token

if [ ! -d "${REPO_DIR}/.git" ]; then
  git clone --quiet "${REPO_URL}" "${REPO_DIR}"
fi
cd "${REPO_DIR}"
git fetch --prune --quiet origin
git checkout --quiet -B main origin/main

export REPO_DIR
exec python3 -m ops.heart.heart
