terraform {
  # 完全一致 pin は nix 管理の terraform (nixpkgs 更新で動く) と噛み合わないため範囲指定。
  required_version = "~> 1.15"

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
      version = "= 0.89.1"
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
