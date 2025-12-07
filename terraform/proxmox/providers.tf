terraform {
  required_version = "1.14.1"

  # Terraform Cloud backend for state management
  cloud {
    organization = "hikuohiku"

    workspaces {
      name = "homelab"
    }
  }

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.88.0"
    }
  }
}

provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token

  ssh {
    agent    = true
    username = var.proxmox_ssh_username
  }
}
