# NixOS template from custom GitHub Release image
resource "null_resource" "nixos_template" {
  # Only run if template doesn't exist
  triggers = {
    template_id = "9001"
  }

  provisioner "local-exec" {
    command = <<-EOT
      ssh root@hikuo-homeserver.tailae6c2.ts.net bash -s <<'ENDSSH'
      # Check if template already exists
      if qm status 9001 &>/dev/null; then
        echo "Template 9001 already exists, skipping..."
        exit 0
      fi

      # Download image if not exists
      cd /var/lib/vz/dump
      IMAGE_NAME="vzdump-qemu-nixos-26.05.20251208.addf7cf.vma.zst"
      if [ ! -f "$IMAGE_NAME" ]; then
        wget https://github.com/hikuohiku/homelab/releases/download/cloud-image-v0.0.0/$IMAGE_NAME
      fi

      # Restore VM
      zstdcat "$IMAGE_NAME" | qmrestore - 9001

      # Set boot order to virtio disk
      qm set 9001 --boot order=virtio0

      # Convert to template
      qm template 9001

      echo "NixOS template 9001 created successfully"
ENDSSH
    EOT
  }
}
