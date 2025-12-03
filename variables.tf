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
