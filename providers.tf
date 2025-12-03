terraform {
  required_version = "1.14.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.88.0"
    }
  }
}

provider "proxmox" {
  endpoint = var.proxmox_endpoint
  username = var.proxmox_username
  password = var.proxmox_password
  insecure = var.proxmox_insecure

  ssh {
    agent    = true
    username = var.proxmox_ssh_username
  }
}
