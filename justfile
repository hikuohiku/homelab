# Retrieve terraform.tfvars from Bitwarden
sync-tfvars:
    bw get notes homelab-terraform-secret > ./terraform/proxmox/terraform.tfvars

# Run terraform plan in proxmox directory
plan:
    (cd terraform/proxmox && terraform plan)

# Run terraform apply in proxmox directory
apply:
    (cd terraform/proxmox && terraform apply -auto-approve)

# Generate Tailscale auth key and run terraform apply
apply-with-tailscale:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Generating Tailscale auth key..."
    # Create a preauthorized, single-use auth key that expires in 10 minutes
    KEY_OUTPUT=$(tailscale key auth create --preauth --expiry 10m 2>&1)
    TAILSCALE_AUTH_KEY=$(echo "$KEY_OUTPUT" | grep -o 'tskey-auth-[a-zA-Z0-9-]*')
    if [[ -z "$TAILSCALE_AUTH_KEY" ]]; then
        echo "Failed to generate Tailscale auth key"
        echo "Output: $KEY_OUTPUT"
        exit 1
    fi
    echo "Auth key generated successfully (expires in 10 minutes)"
    cd terraform/proxmox
    export TF_VAR_tailscale_auth_key="$TAILSCALE_AUTH_KEY"
    terraform apply -auto-approve

# Build NixOS configuration for node01 (local build)
build-node01:
    nix build ./nix/hosts/node01#nixosConfigurations.default.config.system.build.toplevel --no-link

# Push built derivations to Cachix
push-cache:
    nix build ./nix/hosts/node01#nixosConfigurations.default.config.system.build.toplevel --no-link --json \
      | jq -r '.[0].outputs.out' \
      | cachix push hikuohiku

# Build and push to cache (run before terraform apply)
prepare: build-node01 push-cache

# Deploy NixOS configuration to node01 (legacy: local build & deploy)
deploy-node01:
    nix run nixpkgs#nixos-rebuild -- switch --flake ./nix/hosts/node01#default --target-host root@192.168.0.129 --fast
