# NixOS Proxmox image configuration with Tailscale pre-installed
#
# Minimal additions to Hydra base image:
# - Tailscale (for VPN access)
# - Nix settings (Cachix binary cache)
#
# All other configuration should be applied via nixos-rebuild after VM creation.
{
  # Tailscale VPN (auto-authenticate with authkey file)
  services.tailscale = {
    enable = true;
    authKeyFile = "/run/tailscale/authkey";
  };

  # Firewall: trust Tailscale interface
  networking.firewall.trustedInterfaces = [ "tailscale0" ];

  # Nix settings: binary cache for faster nixos-rebuild
  nix.settings = {
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
  system.stateVersion = "25.05";
}
