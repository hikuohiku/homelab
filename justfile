# Terraform commands (run via Doppler)
plan:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform plan)

apply:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform apply -auto-approve)

destroy:
    (cd terraform/proxmox && doppler run --project homelab --config prd --name-transformer tf-var -- terraform destroy)

# Proxmox Cloud Image build & cache
prepare:
    nix build ./nix/images/proxmox-cloud#packages.x86_64-linux.qcow2 --no-link --json \
      | jq -r '.[0].outputs.out' \
      | cachix push hikuohiku
