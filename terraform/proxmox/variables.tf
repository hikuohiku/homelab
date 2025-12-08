# Proxmox Connection Variables
variable "proxmox_endpoint" {
  description = "Proxmox VE API endpoint (e.g., https://proxmox.example.com:8006)"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox VE API token (format: username@realm!tokenid=secret)"
  type        = string
  sensitive   = true
}

variable "proxmox_ssh_username" {
  description = "SSH username for Proxmox host"
  type        = string
  default     = "root"
}

variable "ssh_public_key" {
  description = "SSH public key for the VM root user"
  type        = string
}

variable "proxmox_node" {
  description = "Proxmox node name"
  type        = string
  default     = "hikuo-homeserver"
}

variable "ssh_private_key" {
  description = "SSH private key for connecting to VMs (for nixos-rebuild)"
  type        = string
  sensitive   = true
}

variable "github_repo" {
  description = "GitHub repository for NixOS flake (format: owner/repo)"
  type        = string
  default     = "hikuohiku/homelab"
}
