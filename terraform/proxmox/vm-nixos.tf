
# Cloud-Init configuration for node01
# Injects Age private key and runs nixos-rebuild
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
        - path: /var/lib/sops-nix/key.txt
          permissions: '0600'
          content: ${jsonencode(var.age_private_key)}

        # nixos-rebuild を実行するスクリプト（bash固定）
        - path: /run/nixos-rebuild-on-boot.sh
          permissions: '0755'
          content: |
            #!/run/current-system/sw/bin/bash
            set -euo pipefail

            /run/current-system/sw/bin/nixos-rebuild boot \
              --flake "github:${var.github_repo}?dir=nix/hosts/node01#default" \
              --option substituters "https://cache.nixos.org https://hikuohiku.cachix.org" \
              --option trusted-public-keys "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= hikuohiku.cachix.org-1:AZwUw2nnqdfm6k5oLyczGRRHMBEQXz0Fo1HzI+RwApg=" \
              --refresh

            touch /var/lib/nixos-rebuild-on-boot.done
            /run/current-system/sw/bin/systemctl reboot

        # cloud-init が「1回だけ」実行するフック
        - path: /var/lib/cloud/scripts/per-once/99-nixos-rebuild
          permissions: '0755'
          content: |
            #!/run/current-system/sw/bin/bash
            set -euo pipefail

            # 念のためネットワークが来るまで待つ（保険）
            /run/current-system/sw/bin/systemctl is-active -q network-online.target \
              || /run/current-system/sw/bin/systemctl wait network-online.target

            exec /run/nixos-rebuild-on-boot.sh

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

