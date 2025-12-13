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
    NIX_SSHOPTS="-o ProxyJump=root@hikuo-homeserver" nix run nixpkgs#nixos-rebuild -- switch --flake ./nix/hosts/node01#default --target-host root@192.168.0.129 --fast

# Inject K3s secrets from Doppler to node01
inject-secrets:
    @echo "Injecting K3s secrets from Doppler..."
    @doppler secrets download --project homelab --config prd --no-file --format json | jq -r ' \
        "mkdir -p /var/lib/rancher/k3s/server/tls\n" + \
        "echo \"" + .K3S_TOKEN + "\" > /etc/rancher/k3s/token\n" + \
        "echo \"" + .K3S_CA_CERT + "\" > /var/lib/rancher/k3s/server/tls/server-ca.crt\n" + \
        "echo \"" + .K3S_CA_KEY + "\" > /var/lib/rancher/k3s/server/tls/server-ca.key\n" + \
        "chmod 600 /etc/rancher/k3s/token /var/lib/rancher/k3s/server/tls/server-ca.key\n" + \
        "systemctl restart k3s" \
        ' | ssh -J root@hikuo-homeserver root@192.168.0.129 "cat | sh"
