# Cloud-Init configuration for node01
# Injects Age private key and SSH keys for sops-nix decryption at runtime
# NOTE: Using cicustom overrides Proxmox's sshkeys, so we must include SSH keys here
resource "proxmox_virtual_environment_file" "node01_cloud_init" {
  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.proxmox_node

  source_raw {
    data      = <<-EOT
      #cloud-config
      users:
        - name: root
          ssh_authorized_keys:
            - ${var.ssh_public_key}

      write_files:
        - path: /var/lib/sops-nix/key.txt
          permissions: '0600'
          content: ${jsonencode(var.age_private_key)}

    EOT
    file_name = "node01-cloud-init.yaml"
  }
}


locals {
  # node01 の静的 IP。ip_config と output の2箇所で同じ事実を書かないための単一情報源
  node01_ip = "192.168.0.129"
}

resource "proxmox_virtual_environment_vm" "node01" {
  name      = "node01"
  node_name = var.proxmox_node

  bios = "seabios"

  agent {
    enabled = true
  }

  cpu {
    cores = 4
    type  = "host"
  }

  memory {
    dedicated = 12288
  }

  # Import disk from downloaded qcow2 image (no template/clone needed)
  disk {
    datastore_id = "local-lvm"
    interface    = "virtio0"
    size         = 256
    file_format  = "raw"
    import_from  = proxmox_download_file.nixos_image.id
  }

  initialization {
    user_data_file_id = proxmox_virtual_environment_file.node01_cloud_init.id

    ip_config {
      ipv4 {
        address = "${local.node01_ip}/24"
        gateway = "192.168.0.1"
      }
    }
    # NOTE: user_account は cicustom で上書きされるため削除
    # SSH キーは cicustom YAML 内の users セクションで設定
  }

  network_device {
    bridge = "vmbr0"
  }

  started = true

  # Ensure image is downloaded before VM creation
  depends_on = [
    proxmox_download_file.nixos_image
  ]

  # Recreate VM when the base image changes
  lifecycle {
    replace_triggered_by = [
      proxmox_download_file.nixos_image.id
    ]
  }
}

output "node01_ip" {
  value = local.node01_ip
}
