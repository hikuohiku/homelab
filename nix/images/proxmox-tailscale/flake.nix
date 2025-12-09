{
  description = "NixOS Proxmox image with Tailscale pre-installed";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      # NixOS configuration for the image
      nixosConfigurations.proxmox-tailscale = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          "${nixpkgs}/nixos/modules/virtualisation/proxmox-image.nix"
          ./configuration.nix
        ];
      };

      # Build the Proxmox VMA image
      # Usage: nix build .#proxmox-image
      packages.${system} = {
        proxmox-image = self.nixosConfigurations.proxmox-tailscale.config.system.build.VMA;
        default = self.packages.${system}.proxmox-image;
      };
    };
}
