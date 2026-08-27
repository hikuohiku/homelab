#!/usr/bin/env bash
# ArgoCD preview の後片付け (P-9047 実測。P-0028 の手順を機械化)。
#
# 何をするか: `just preview` と等価の kubectl patch で feature ブランチに向けられた
# ArgoCD Application と、auto-sync を外された root apps を、**merge 後に**元へ戻す。
#
#   1. autopilot Application の targetRevision を HEAD に戻す
#   2. apps (root) Application の syncPolicy.automated {prune:true, selfHeal:true} を復元
#      (apps/apps.yaml の定義どおり)
#   3. 両方の sync.status が Synced になるまで待つ
#
# 使い方:
#   ops/tools/argocd_preview_reset.sh            # 確認を取って実行
#   ops/tools/argocd_preview_reset.sh --yes      # 非対話で実行
#   ops/tools/argocd_preview_reset.sh --check    # 状態表示。preview 中なら rc=1
#
# 前提: kubectl がクラスタ (argocd ns) の Application に patch/get 権限を持つこと。
# 冪等: preview でない状態で実行しても何も変えず rc=0。
#
# 注意: autopilot を HEAD に戻して sync すると、branch にしか無いリソース (P-9047 の
# RBAC Role/RoleBinding) は prune される。**merge 前に実行すると verify が落ちる**ため、
# 必ず PR の merge 後に実行する (merge 後は main に同リソースが入るので失われない)。
set -euo pipefail

NS="argocd"
AUTOPILOT_APP="autopilot"
ROOT_APP="apps"
BRANCH_PREFIX="project/"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

state() {
  local rev automated sync
  rev=$(kubectl get application "$AUTOPILOT_APP" -n "$NS" -o jsonpath='{.spec.source.targetRevision}')
  automated=$(kubectl get application "$ROOT_APP" -n "$NS" -o jsonpath='{.spec.syncPolicy.automated}' 2>/dev/null || true)
  sync=$(kubectl get application "$ROOT_APP" -n "$NS" -o jsonpath='{.status.sync.status}')
  echo "autopilot:      targetRevision=$rev"
  echo "apps (root):    syncPolicy.automated=${automated:-<none>} / status.sync=$sync"
}

# preview 状態か = autopilot が feature ブランチを追跡している、または root の auto-sync が無い
preview_active() {
  local rev automated
  rev=$(kubectl get application "$AUTOPILOT_APP" -n "$NS" -o jsonpath='{.spec.source.targetRevision}')
  automated=$(kubectl get application "$ROOT_APP" -n "$NS" -o jsonpath='{.spec.syncPolicy.automated}' 2>/dev/null || true)
  [[ "$rev" == "$BRANCH_PREFIX"* ]] || [[ -z "$automated" ]]
}

wait_synced() {
  local app="$1"
  for _ in $(seq 1 60); do
    local s
    s=$(kubectl get application "$app" -n "$NS" -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "")
    if [[ "$s" == "Synced" ]]; then
      echo "$app: Synced"
      return 0
    fi
    sleep 3
  done
  echo "$app: 180s 以内に Synced にならない" >&2
  return 1
}

case "${1:-}" in
  --check)
    state
    if preview_active; then
      echo "preview が有効です (merge 前なら正常。merge 後に実行して解消する)" >&2
      exit 1
    fi
    echo "preview は解消済みです"
    exit 0
    ;;
  -h | --help)
    usage
    exit 0
    ;;
esac

if ! preview_active; then
  echo "preview 状態ではありません (何もしない)"
  exit 0
fi

state
if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "autopilot を HEAD に戻し、apps の auto-sync を復元します。よろしいですか? [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "中止"; exit 1; }
fi

rev=$(kubectl get application "$AUTOPILOT_APP" -n "$NS" -o jsonpath='{.spec.source.targetRevision}')
if [[ "$rev" == "$BRANCH_PREFIX"* ]]; then
  kubectl patch application "$AUTOPILOT_APP" -n "$NS" --type merge \
    -p '{"spec":{"source":{"targetRevision":"HEAD"}}}'
fi

automated=$(kubectl get application "$ROOT_APP" -n "$NS" -o jsonpath='{.spec.syncPolicy.automated}' 2>/dev/null || true)
if [[ -z "$automated" ]]; then
  kubectl patch application "$ROOT_APP" -n "$NS" --type merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
fi

wait_synced "$AUTOPILOT_APP"
wait_synced "$ROOT_APP"
state
echo "preview の後片付けが完了しました"