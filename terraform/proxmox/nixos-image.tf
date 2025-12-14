# NixOS qcow2 Image Download
# Downloads the NixOS cloud image from GitHub Releases
# This replaces the old local-exec provisioner approach for idempotency

resource "proxmox_virtual_environment_download_file" "nixos_image" {
  content_type = "import"
  datastore_id = "local"
  node_name    = var.proxmox_node

  # Image URL from GitHub Releases (zstd compressed)
  url                     = "https://github.com/${var.github_repo}/releases/download/${var.nixos_image_version}/nixos-proxmox-cloud-${var.nixos_image_version}.qcow2.zst"
  file_name               = "nixos-proxmox-cloud-${var.nixos_image_version}.qcow2"
  decompression_algorithm = "zst"

  # Optional: checksum verification (of compressed file)
  checksum           = var.nixos_image_checksum
  checksum_algorithm = var.nixos_image_checksum != null ? "sha256" : null

  # Increase timeout for large images
  upload_timeout = 1200

  # Ensure new image is downloaded before old is deleted
  # This prevents issues if the download fails
  lifecycle {
    create_before_destroy = true
  }
}
