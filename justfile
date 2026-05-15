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
    eval "$(direnv export bash 2>/dev/null)" && curl -sk --max-time 10 -H "Authorization: PVEAPIToken=${PROXMOX_API_TOKEN}" "${PROXMOX_HOST}:8006/api2/json/version" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Proxmox VE', d['data']['version'])"
    echo ""
    echo "✓ All layers reachable"

# Proxmox Cloud Image build & cache
prepare:
    cachix watch-exec hikuohiku -- nix build ./nix/images/proxmox-cloud#packages.x86_64-linux.qcow2 --no-link
