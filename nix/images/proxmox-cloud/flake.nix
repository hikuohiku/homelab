{
  description = "NixOS Proxmox Cloud image for node01";

  inputs = {
    flake-parts.url = "github:hercules-ci/flake-parts";
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    sops-nix.url = "github:Mic92/sops-nix";
    sops-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    inputs@{
      flake-parts,
      nixpkgs,
      sops-nix,
      ...
    }:
    flake-parts.lib.mkFlake { inherit inputs; } (
      { self, ... }:
      {
        systems = [ "x86_64-linux" ];
        flake = {
          # NixOS configuration for the Proxmox Cloud image
          nixosConfigurations.proxmox-cloud = nixpkgs.lib.nixosSystem {
            system = "x86_64-linux";
            modules = [
              "${nixpkgs}/nixos/modules/virtualisation/proxmox-image.nix"
              sops-nix.nixosModules.sops
              ./configuration.nix
            ];
          };
        };
        perSystem = {
          # qcow2 イメージ (Terraform の import_from で使用)
          packages = {
            qcow2 = self.nixosConfigurations.proxmox-cloud.config.system.build.qcow2;
            default = self.nixosConfigurations.proxmox-cloud.config.system.build.qcow2;
            # レガシー: VMA イメージ (手動インポート用)
            vma = self.nixosConfigurations.proxmox-cloud.config.system.build.VMA;
          };
        };
      }
    );
}
