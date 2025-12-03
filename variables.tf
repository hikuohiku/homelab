# Proxmox Connection Variables
variable "proxmox_endpoint" {
  description = "Proxmox VE API endpoint (e.g., https://proxmox.example.com:8006)"
  type        = string
}

variable "proxmox_username" {
  description = "Proxmox VE username (e.g., root@pam)"
  type        = string
}

variable "proxmox_password" {
  description = "Proxmox VE password"
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS verification (set to true for self-signed certificates)"
  type        = bool
  default     = false
}

variable "proxmox_ssh_username" {
  description = "SSH username for Proxmox host"
  type        = string
  default     = "root"
}
