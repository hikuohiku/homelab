{ lib, ... }:

{
  imports = [ ];

  # ブートローダー設定 (既存の GRUB を維持)
  boot.loader.grub = {
    enable = true;
    device = "/dev/vda"; # MBR パーティションテーブル
  };

  # ファイルシステム設定
  fileSystems."/" = {
    device = "/dev/vda1";
    fsType = "ext4";
  };

  # ネットワークインターフェース
  boot.initrd.availableKernelModules = [
    "virtio_pci"
    "virtio_scsi"
    "virtio_blk"
    "ahci"
    "sd_mod"
  ];
  boot.initrd.kernelModules = [ ];
  boot.kernelModules = [ ];
  boot.extraModulePackages = [ ];

  # Proxmox VM 用の設定
  services.qemuGuest.enable = true;

  # スワップ無効
  swapDevices = [ ];

  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
