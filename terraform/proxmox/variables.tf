# ===========================================
# Proxmox Connection Variables
# ===========================================
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
}

variable "proxmox_node" {
  description = "Proxmox node name"
  type        = string
}

# ===========================================
# NixOS Image Variables
# ===========================================
variable "nixos_image_version" {
  description = "NixOS image version tag (e.g., v0.3.0)"
  type        = string
}

variable "nixos_image_checksum" {
  description = "SHA256 checksum of the NixOS qcow2 image"
  type        = string
  default     = null
}

variable "github_repo" {
  description = "GitHub repository for NixOS image releases (format: owner/repo)"
  type        = string
}

# ===========================================
# VM Configuration Variables
# ===========================================
variable "ssh_public_key" {
  description = "SSH public key for the VM root user"
  type        = string
}

variable "age_private_key" {
  description = "Age private key for sops-nix decryption (injected via Cloud-Init)"
  type        = string
  sensitive   = true
}

# ===========================================
# Tailscale Variables
# ===========================================
variable "tailscale_oauth_client_id" {
  description = "Tailscale OAuth Client ID for generating auth keys"
  type        = string
}

variable "tailscale_oauth_client_secret" {
  description = "Tailscale OAuth Client Secret"
  type        = string
  sensitive   = true
}
