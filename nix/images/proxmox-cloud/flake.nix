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
              # qcow2 イメージビルド用モジュール
              (
                { config, lib, pkgs, modulesPath, ... }:
                {
                  system.build.qcow2 = import "${modulesPath}/../lib/make-disk-image.nix" {
                    inherit lib config pkgs;
                    diskSize = "auto";
                    format = "qcow2";
                    partitionTableType = "hybrid";
                    name = "nixos-proxmox-cloud";
                  };
                }
              )
            ];
          };
        };
        perSystem = {
          # qcow2 イメージ (Terraform の import_from で使用)
          packages = {
            qcow2 = self.nixosConfigurations.proxmox-cloud.config.system.build.qcow2;
            default = self.nixosConfigurations.proxmox-cloud.config.system.build.qcow2;
          };
        };
      }
    );
}
