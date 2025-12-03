# Proxmox Backup Server VM Configuration

resource "proxmox_virtual_environment_vm" "pbs" {
  name        = "pbs"
  description = "Proxmox Backup Server - Managed by Terraform"
  node_name   = "pve"

  # Use SeaBIOS (default, compatible with PBS)
  bios = "seabios"

  # Boot from CDROM first for installation, then from disk
  boot_order = ["ide2", "scsi0"]

  # Mount PBS ISO for installation
  cdrom {
    file_id   = "local:iso/proxmox-backup-server_3.2-1.iso"
    interface = "ide2"
  }

  # System disk configuration
  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = 64
    file_format  = "raw"
  }

  # CPU configuration
  cpu {
    cores = 4
    type  = "host"
  }

  # Memory configuration
  memory {
    dedicated = 8192
  }

  # Network configuration
  network_device {
    bridge = "vmbr0"
  }

  # Enable QEMU guest agent
  agent {
    enabled = true
  }

  # Create VM in stopped state for manual installation
  started = false

  tags = ["terraform", "pbs", "backup"]
}

# Output PBS VM information
output "pbs_vm_id" {
  description = "The ID of the PBS VM"
  value       = proxmox_virtual_environment_vm.pbs.id
}

output "pbs_ipv4_addresses" {
  description = "The IPv4 addresses of the PBS VM"
  value       = proxmox_virtual_environment_vm.pbs.ipv4_addresses
}
