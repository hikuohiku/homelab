
# Cloud-Init configuration for node01
# Injects Age private key for sops-nix decryption at runtime
resource "proxmox_virtual_environment_file" "node01_cloud_init" {
  content_type = "snippets"
  datastore_id = "local"
  node_name    = var.proxmox_node

  source_raw {
    data = <<-EOT
      #cloud-config
      users:
        - default
        - name: root
          ssh_authorized_keys:
            - ${indent(6, var.ssh_public_key)}
          shell: /run/current-system/sw/bin/bash

      write_files:
        # Age private key for sops-nix runtime decryption
        - path: /var/lib/sops-nix/key.txt
          permissions: '0600'
          content: ${jsonencode(var.age_private_key)}

    EOT
    file_name = "node01-cloud-init.yaml"
  }
}


resource "proxmox_virtual_environment_vm" "node01" {
  name      = "node01"
  node_name = var.proxmox_node

  bios = "seabios" # Match template's BIOS mode

  clone {
    vm_id = 9001 # NixOS template created from Hydra prebuilt image
  }

  depends_on = [
    null_resource.nixos_template
  ]

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

  disk {
    datastore_id = "local-lvm"
    interface    = "virtio0"
    size         = 20
    file_format  = "raw"
  }
  # To resize, use qm resize after VM creation

  initialization {
    user_data_file_id = proxmox_virtual_environment_file.node01_cloud_init.id

    ip_config {
      ipv4 {
        address = "192.168.0.129/24"
        gateway = "192.168.0.1"
      }
    }

    user_account {
      username = "root"
      keys     = [var.ssh_public_key]
    }
  }

  network_device {
    bridge = "vmbr0"
  }

  started = true
}

output "node01_ip" {
  value = "192.168.0.129"
}

