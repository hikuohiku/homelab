# NixOS template from custom GitHub Release image
resource "null_resource" "nixos_template" {
  # Trigger recreation when image version changes
  triggers = {
    template_id   = "9001"
    image_version = "v0.2.0"
  }

  provisioner "local-exec" {
    command = <<-EOT
      ssh root@hikuo-homeserver.tailae6c2.ts.net bash -s <<'ENDSSH'
      # Stop and remove existing template if exists
      if qm status 9001 &>/dev/null; then
        echo "Removing existing template 9001..."
        qm destroy 9001 --purge || true
      fi

      # Clean up old images
      cd /var/lib/vz/dump
      rm -f vzdump-qemu-*.vma.zst

      # Download new image
      IMAGE_NAME="vzdump-qemu-node01.vma.zst"
      wget -O "$IMAGE_NAME" https://github.com/hikuohiku/homelab/releases/download/v0.2.0/$IMAGE_NAME

      # Restore VM
      zstdcat "$IMAGE_NAME" | qmrestore - 9001

      # Set boot order to virtio disk
      qm set 9001 --boot order=virtio0

      # Convert to template
      qm template 9001

      echo "NixOS template 9001 created successfully from v0.2.0"
ENDSSH
    EOT
  }
}
