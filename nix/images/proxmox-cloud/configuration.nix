# NixOS Proxmox Cloud image configuration for node01
#
# This configuration combines:
# - Proxmox image settings (cloud-init, qemu)
# - k3s server configuration
# - sops-nix for secret management
#
# Machine-specific settings (IP, SSH keys) are managed by Cloud-Init via Terraform
{
  pkgs,
  config,
  lib,
  ...
}:
{
  imports = [ ./k3s-manifests.nix ];

  # システムのステートバージョン
  system.stateVersion = "25.05";

  # =========================================
  # Proxmox / Cloud-Init 設定
  # =========================================
  # Proxmox イメージ固有の設定
  proxmox.cloudInit.enable = true;

  # Cloud-Init サービス (SSH キー注入、ネットワーク設定)
  services.cloud-init = {
    enable = true;
    network.enable = true;
  };

  # =========================================
  # ネットワーク設定
  # =========================================
  # ホスト名 (Cloud-Init から上書き可能)
  networking.hostName = lib.mkDefault "node01";

  # Cloud-Init がネットワークを設定するため DHCP は使用しない
  networking.useDHCP = lib.mkDefault false;

  # DNS サーバー
  networking.nameservers = [
    "8.8.8.8"
    "1.1.1.1"
  ];

  # カーネルパラメータ (インターフェース名を eth0 に固定)
  boot.kernelParams = [
    "net.ifnames=0"
    "biosdevname=0"
  ];

  # ファイアウォール設定
  networking.firewall = {
    enable = true;
    allowedTCPPorts = [
      22 # SSH
      6443 # k3s API server
    ];
  };

  # =========================================
  # SSH 設定
  # =========================================
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
  };

  # =========================================
  # sops-nix 設定
  # =========================================
  sops = {
    defaultSopsFile = ./secrets.yaml;
    age.keyFile = "/var/lib/sops-nix/key.txt"; # Cloud-Init で注入

    secrets.doppler-token = { };

    # テンプレート: Kubernetes Secret YAML を生成
    templates."doppler-token-k8s.yaml" = {
      content = ''
        apiVersion: v1
        kind: Secret
        metadata:
          name: doppler-token
          namespace: external-secrets
        stringData:
          token: ${config.sops.placeholder.doppler-token}
      '';
      path = "/var/lib/rancher/k3s/server/manifests/doppler-token.yaml";
    };
  };

  # =========================================
  # k3s サービス設定
  # =========================================
  services.k3s = {
    enable = true;
    role = "server";
    extraFlags = toString [ "--disable traefik" ];
  };

  # =========================================
  # システム設定
  # =========================================
  environment.systemPackages = with pkgs; [
    vim
    git
    wget
    curl
    htop
    kubectl
    kubernetes-helm
  ];

  environment.sessionVariables.KUBECONFIG = "/etc/rancher/k3s/k3s.yaml";

  time.timeZone = "Asia/Tokyo";
  i18n.defaultLocale = "en_US.UTF-8";

  # Nix 設定
  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    substituters = [
      "https://cache.nixos.org"
      "https://hikuohiku.cachix.org"
    ];
    trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "hikuohiku.cachix.org-1:AZwUw2nnqdfm6k5oLyczGRRHMBEQXz0Fo1HzI+RwApg="
    ];
  };
}
