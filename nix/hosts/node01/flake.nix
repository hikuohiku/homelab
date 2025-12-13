{
  description = "NixOS configuration for node01";

  inputs = {
    # NixOS unstable (node01 が 26.05pre-git なので合わせる)
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # sops-nix for secret management
    sops-nix.url = "github:Mic92/sops-nix";
    sops-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    { nixpkgs, sops-nix, ... }:
    {
      nixosConfigurations.default = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          ./configuration.nix
          ./hardware-configuration.nix
          sops-nix.nixosModules.sops
        ];
      };
    };
}
