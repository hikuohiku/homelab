# NixOS Proxmox image configuration with Tailscale pre-installed
#
# This is a minimal base image. Host-specific configuration (k3s, etc.)
# should be applied via nixos-rebuild after VM creation.
{ config, pkgs, lib, ... }:

{
  # Proxmox-specific settings
  proxmox = {
    qemuConf = {
      cores = 2;
      memory = 4096;
      bios = "seabios";
      virtio0 = "local-lvm:vm-9002-disk-0";
    };
    # Enable cloud-init for IP configuration
    cloudInit = {
      enable = true;
    };
  };

  # Tailscale VPN (pre-installed, ready for `tailscale up`)
  services.tailscale.enable = true;

  # SSH server for remote access
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
  };

  # QEMU guest agent for Proxmox integration
  services.qemuGuest.enable = true;

  # Firewall
  networking.firewall = {
    enable = true;
    allowedTCPPorts = [ 22 ];
    trustedInterfaces = [ "tailscale0" ];
  };

  # Basic packages
  environment.systemPackages = with pkgs; [
    vim
    git
    wget
    curl
    htop
  ];

  # Timezone
  time.timeZone = "Asia/Tokyo";

  # Locale
  i18n.defaultLocale = "en_US.UTF-8";

  # Nix settings
  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    # Binary cache (Cachix) - speeds up nixos-rebuild
    substituters = [
      "https://cache.nixos.org"
      "https://hikuohiku.cachix.org"
    ];
    trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "hikuohiku.cachix.org-1:AZwUw2nnqdfm6k5oLyczGRRHMBEQXz0Fo1HzI+RwApg="
    ];
  };

  # System state version
  system.stateVersion = "24.11";
}
