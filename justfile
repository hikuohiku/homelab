# Terraform commands (run via Doppler)
plan:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform plan)

apply:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform apply -auto-approve)

destroy:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform destroy)

# NixOS build & cache
build:
    nix build ./nix/hosts/node01#nixosConfigurations.default.config.system.build.toplevel --no-link

push-cache:
    nix build ./nix/hosts/node01#nixosConfigurations.default.config.system.build.toplevel --no-link --json \
      | jq -r '.[0].outputs.out' \
      | cachix push hikuohiku

# Prepare for deployment (build & push)
prepare: build push-cache

# Legacy deploy (direct nixos-rebuild via SSH)
deploy-legacy:
    NIX_SSHOPTS="-o ProxyJump=root@hikuo-homeserver" nix run nixpkgs#nixos-rebuild -- switch --flake ./nix/hosts/node01#default --target-host root@192.168.0.129 --fast
