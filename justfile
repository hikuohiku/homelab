# Terraform commands (run via Doppler)
plan:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform plan)

apply:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform apply -auto-approve)

destroy:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform destroy)

# Agent: Tailscale 接続
ts-up:
    tailscale up

# Agent: 全レイヤー接続チェック
preflight:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== Tailscale ==="
    tailscale status | head -3
    echo ""
    echo "=== Kubernetes ==="
    kubectl get nodes --request-timeout=10s
    echo ""
    echo "=== ArgoCD ==="
    curl -sk --max-time 10 https://argocd.tailae6c2.ts.net/api/version | python3 -c "import sys,json; print('ArgoCD', json.load(sys.stdin)['Version'])"
    echo ""
    echo "=== Proxmox ==="
    eval "$(direnv export bash 2>/dev/null)" && curl -sk --max-time 10 -H "Authorization: PVEAPIToken=${PROXMOX_AGENT_TOKEN}" "${PROXMOX_ENDPOINT}:8006/api2/json/version" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Proxmox VE', d['data']['version'])"
    echo ""
    echo "=== Preview Status ==="
    kubectl get applications -n argocd -o json | python3 - <<'PY'
    import sys, json
    apps = json.load(sys.stdin)['items']
    non_head = [a for a in apps if a['spec']['source'].get('targetRevision', 'HEAD') != 'HEAD']
    if non_head:
        for a in non_head:
            print(f"  ⚠ {a['metadata']['name']}: {a['spec']['source']['targetRevision']}")
    else:
        print("  ✓ All apps on HEAD")
    PY
    echo ""
    echo "✓ All layers reachable"

# Preview: フィーチャーブランチを ArgoCD でテストデプロイ
#   既存アプリ: just preview <app> <branch>
#   新規アプリ: just preview apps <branch> (ルートをブランチに向けて新 Application を認識させる)
preview app branch:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{app}}" = "apps" ]; then
      kubectl patch application apps -n argocd --type merge \
        -p "{\"spec\":{\"source\":{\"targetRevision\":\"{{branch}}\"}}}"
      echo "✓ Root 'apps' now tracking '{{branch}}'"
      echo "  Child apps with targetRevision: HEAD still track main."
      echo "  New apps defined on the branch will be created."
    else
      echo "Disabling auto-sync on root 'apps' to prevent selfHeal revert..."
      kubectl patch application apps -n argocd --type json \
        -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]' 2>/dev/null || true
      kubectl patch application "{{app}}" -n argocd --type merge \
        -p "{\"spec\":{\"source\":{\"targetRevision\":\"{{branch}}\"}}}"
      echo "✓ {{app}} now tracking '{{branch}}'"
    fi
    echo "  Run 'just preview-status' to check, 'just preview-reset {{app}}' to revert."

# Preview: アプリを HEAD に戻して auto-sync を復元
preview-reset app:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl patch application "{{app}}" -n argocd --type merge \
      -p '{"spec":{"source":{"targetRevision":"HEAD"}}}'
    if [ "{{app}}" != "apps" ]; then
      echo "Restoring auto-sync on root 'apps'..."
      kubectl patch application apps -n argocd --type merge \
        -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
    fi
    echo "✓ {{app}} reset to HEAD"

# Preview: HEAD 以外を追跡中のアプリ一覧
preview-status:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== Preview Status ==="
    kubectl get applications -n argocd -o json | python3 - <<'PY'
    import sys, json
    apps = json.load(sys.stdin)['items']
    non_head = [a for a in apps if a['spec']['source'].get('targetRevision', 'HEAD') != 'HEAD']
    if non_head:
        for a in non_head:
            print(f"  ⚠ {a['metadata']['name']}: {a['spec']['source']['targetRevision']}")
    else:
        print("  ✓ All apps on HEAD")
    PY

# ArgoCD: ルート Application を再適用 (AutoSync 有効化)
argocd-bootstrap:
    git show main:apps/apps.yaml | kubectl apply -f -

# Proxmox Cloud Image build & cache
prepare:
    cachix watch-exec hikuohiku -- nix build ./nix/images/proxmox-cloud#packages.x86_64-linux.qcow2 --no-link
