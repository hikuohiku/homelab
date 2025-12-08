resource "proxmox_virtual_environment_vm" "node01" {
  name      = "node01"
  node_name = var.proxmox_node

  bios = "seabios"  # Match template's BIOS mode

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
  # To resize, use qm resize after VM creation

  initialization {
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

# NixOS configuration deployment
# Runs nixos-rebuild on the VM using cached binaries from Cachix

# Get flake narHash to detect any changes (including flake.lock)
data "external" "flake_hash" {
  program = ["bash", "-c", "nix flake metadata ${path.module}/../../nix/hosts/node01 --json | jq '{narHash: .locked.narHash}'"]
}

resource "null_resource" "nixos_deploy_node01" {
  depends_on = [proxmox_virtual_environment_vm.node01]

  # Re-run when flake narHash changes (includes all dependencies)
  triggers = {
    flake_nar_hash = data.external.flake_hash.result.narHash
  }

  connection {
    type        = "ssh"
    user        = "root"
    host        = "192.168.0.129"
    private_key = var.ssh_private_key
    timeout     = "5m"
  }

  # Wait for VM to be ready
  provisioner "remote-exec" {
    inline = [
      "echo 'Waiting for system to be ready...'",
      "sleep 10"
    ]
  }

  # Run nixos-rebuild using cached binaries
  provisioner "remote-exec" {
    inline = [
      "echo 'Starting NixOS rebuild from GitHub flake...'",
      <<-EOT
      nixos-rebuild switch \
        --flake github:${var.github_repo}?dir=nix/hosts/node01#default \
        --option substituters "https://cache.nixos.org https://hikuohiku.cachix.org" \
        --option trusted-public-keys "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= hikuohiku.cachix.org-1:AZwUw2nnqdfm6k5oLyczGRRHMBEQXz0Fo1HzI+RwApg=" \
        --refresh
      EOT
      ,
      "echo 'NixOS rebuild completed successfully'"
    ]
  }
}
