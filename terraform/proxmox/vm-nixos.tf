resource "proxmox_virtual_environment_vm" "node01" {
  name      = "node01"
  node_name = var.proxmox_node

  bios = "seabios"  # Match template's BIOS mode

  boot_order = ["virtio0"]  # Boot from virtio disk

  clone {
    vm_id = 9001  # NixOS template created from Hydra prebuilt image
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
    dedicated = 4096
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "virtio0"
    size         = 20
    file_format  = "raw"
  }

  initialization {
    ip_config {
      ipv4 {
        address = "dhcp"
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
  value = proxmox_virtual_environment_vm.node01.ipv4_addresses[1][0] # Adjust index based on interface
}
