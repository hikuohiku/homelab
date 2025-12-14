# NixOS qcow2 Image Download
# Downloads the NixOS cloud image from GitHub Releases
# This replaces the old local-exec provisioner approach for idempotency

resource "proxmox_virtual_environment_download_file" "nixos_image" {
  content_type = "import"
  datastore_id = "local"
  node_name    = var.proxmox_node

  # Image URL from GitHub Releases
  url       = "https://github.com/${var.github_repo}/releases/download/${var.nixos_image_version}/nixos-proxmox-cloud.qcow2"
  file_name = "nixos-proxmox-cloud-${var.nixos_image_version}.qcow2"

  # Optional: checksum verification
  checksum           = var.nixos_image_checksum
  checksum_algorithm = var.nixos_image_checksum != null ? "sha256" : null

  # Increase timeout for large images
  upload_timeout = 1200
}
