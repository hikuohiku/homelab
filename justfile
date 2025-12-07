# Retrieve terraform.tfvars from Bitwarden
sync-tfvars:
    bw get notes homelab-terraform-secret > ./terraform/proxmox/terraform.tfvars

# Run terraform plan in proxmox directory
plan:
    (cd terraform/proxmox && terraform plan)

# Run terraform apply in proxmox directory
apply:
    (cd terraform/proxmox && terraform apply)
