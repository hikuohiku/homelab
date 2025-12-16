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
      version = "= 0.89.1"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "~> 0.18"
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

# Tailscale provider for generating ephemeral auth keys
provider "tailscale" {
  oauth_client_id     = var.tailscale_oauth_client_id
  oauth_client_secret = var.tailscale_oauth_client_secret
}
