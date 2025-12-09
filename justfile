# Retrieve terraform.tfvars from Bitwarden
sync-tfvars:
    bw get notes homelab-terraform-secret > ./terraform/proxmox/terraform.tfvars

# Run terraform plan in proxmox directory
plan:
    (cd terraform/proxmox && terraform plan)

# Run terraform apply in proxmox directory
apply:
    (cd terraform/proxmox && terraform apply -auto-approve)

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

# =============================================================================
# Custom Proxmox Image (with Tailscale pre-installed)
# =============================================================================

# Build Proxmox VMA image with Tailscale
build-image:
    nix build ./nix/images/proxmox-tailscale#proxmox-image -o result-proxmox-image
    @echo "Image built: result-proxmox-image/"
    @ls -lh result-proxmox-image/

# Create a GitHub release with the Proxmox image
# Usage: just release-image v1.0.0
release-image version:
    #!/usr/bin/env bash
    set -euo pipefail

    # Build the image
    echo "Building Proxmox image..."
    nix build ./nix/images/proxmox-tailscale#proxmox-image -o result-proxmox-image

    # Find the VMA file
    VMA_FILE=$(find result-proxmox-image -name "*.vma.zst" | head -1)
    if [[ -z "$VMA_FILE" ]]; then
        echo "Error: VMA file not found"
        exit 1
    fi

    echo "Found image: $VMA_FILE"
    echo "Size: $(du -h "$VMA_FILE" | cut -f1)"

    # Create GitHub release
    echo "Creating GitHub release {{version}}..."
    gh release create "{{version}}" \
        --title "Proxmox Image {{version}}" \
        --notes "NixOS Proxmox image with Tailscale pre-installed.

## Usage
1. Download the .vma.zst file
2. Upload to Proxmox: \`scp vzdump-*.vma.zst root@proxmox:/var/lib/vz/dump/\`
3. Restore as template: \`qmrestore /var/lib/vz/dump/vzdump-*.vma.zst 9002 && qm template 9002\`
" \
        "$VMA_FILE"

    echo "Release created: https://github.com/hikuohiku/homelab/releases/tag/{{version}}"
