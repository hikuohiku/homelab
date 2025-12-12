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

# Create a GitHub release with the Proxmox image
release-cloud-image version:
    nix build ./nix/images/proxmox-cloud#packages.x86_64-linux.proxmox-image -o ./nix/images/proxmox-cloud/result
    gh release create "cloud-image-{{ version }}" \
        --title "Proxmox Image {{ version }}" \
        --notes "NixOS Proxmox Cloud image (minimal base with Cachix cache)." \
        "./nix/images/proxmox-cloud/result/*.vma.zst"
