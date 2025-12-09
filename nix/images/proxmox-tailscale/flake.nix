{
  description = "NixOS Proxmox image with Tailscale pre-installed";

  inputs = {
    flake-parts.url = "github:hercules-ci/flake-parts";
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    inputs@{ flake-parts, nixpkgs, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } (
      { self, ... }:
      {
        systems = [ "x86_64-linux" ];
        flake = {
          # NixOS configuration for the image
          nixosConfigurations.proxmox-tailscale = nixpkgs.lib.nixosSystem {
            system = "x86_64-linux";
            modules = [
              "${nixpkgs}/nixos/modules/virtualisation/proxmox-image.nix"
              ./configuration.nix
            ];
          };
        };
        perSystem = {
          packages = {
            proxmox-image = self.nixosConfigurations.proxmox-tailscale.config.system.build.VMA;
            default = self.nixosConfigurations.proxmox-tailscale.config.system.build.VMA;
          };
        };
      }
    );
}
