# Tailscale ephemeral auth key for node01
# Generated dynamically via OAuth Client
resource "tailscale_tailnet_key" "node01" {
  reusable      = false
  ephemeral     = true
  preauthorized = true
  tags          = ["tag:k3s"]
  description   = "Terraform: node01 VM"
}

# Cloud-Init configuration for node01
# Injects Age private key, SSH keys, and Tailscale auth key
# NOTE: Using cicustom overrides Proxmox's sshkeys, so we must include SSH keys here
resource "proxmox_virtual_environment_file" "node01_cloud_init" {
  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.proxmox_node

  source_raw {
    data = <<-EOT
      #cloud-config
      users:
        - name: root
          ssh_authorized_keys:
            - ${var.ssh_public_key}

      write_files:
        - path: /var/lib/sops-nix/key.txt
          permissions: '0600'
          content: ${jsonencode(var.age_private_key)}
        - path: /var/lib/tailscale/auth-key
          permissions: '0600'
          content: ${tailscale_tailnet_key.node01.key}

    EOT
    file_name = "node01-cloud-init.yaml"
  }
}


resource "proxmox_virtual_environment_vm" "node01" {
  name      = "node01"
  node_name = var.proxmox_node

  bios = "seabios"

  agent {
    enabled = true
  }

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 8192
  }

  # Import disk from downloaded qcow2 image (no template/clone needed)
  disk {
    datastore_id = "local-lvm"
    interface    = "virtio0"
    size         = 20
    file_format  = "raw"
    import_from  = proxmox_virtual_environment_download_file.nixos_image.id
  }

  initialization {
    user_data_file_id = proxmox_virtual_environment_file.node01_cloud_init.id

    ip_config {
      ipv4 {
        address = "192.168.0.129/24"
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
    proxmox_virtual_environment_download_file.nixos_image
  ]

  # Recreate VM when the base image changes
  lifecycle {
    replace_triggered_by = [
      proxmox_virtual_environment_download_file.nixos_image.id
    ]
  }
}

output "node01_ip" {
  value = "192.168.0.129"
}

