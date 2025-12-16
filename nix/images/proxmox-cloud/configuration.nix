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

  # Cloud-Init 完了後に sops シークレットを再セットアップ
  # (activation スクリプトは cloud-init より前に実行されるため)
  systemd.services.sops-install-secrets-after-cloud-init = {
    description = "Re-run sops-install-secrets after cloud-init";
    wantedBy = [ "multi-user.target" ];
    after = [ "cloud-final.service" ];
    wants = [ "cloud-final.service" ];
    before = [ "k3s.service" ]; # k3s より前に実行してシークレットを準備
    path = [ pkgs.gnupg ];
    environment.SOPS_GPG_EXEC = "${pkgs.gnupg}/bin/gpg";
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      # Age キーが存在する場合のみ実行
      if [ -f /var/lib/sops-nix/key.txt ]; then
        echo "Re-running sops-install-secrets..."
        # manifest.json のパスを取得して直接実行
        MANIFEST=$(find /nix/store -maxdepth 1 -name '*-manifest.json' -newer /run/current-system 2>/dev/null | head -1)
        if [ -z "$MANIFEST" ]; then
          MANIFEST=$(cat /run/current-system/activate | grep -o '/nix/store/[a-z0-9]*-manifest.json' | head -1)
        fi
        if [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ]; then
          /nix/store/*-sops-install-secrets-*/bin/sops-install-secrets "$MANIFEST" || true
        else
          echo "Could not find sops manifest"
        fi
      else
        echo "Age key not found at /var/lib/sops-nix/key.txt, skipping"
      fi
    '';
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
  # Tailscale 設定
  # =========================================
  # VM に Tailscale をインストールし、Pod から Tailscale ネットワークにアクセス可能に
  # これにより Pod 内から Tailscale ホスト名 (*.ts.net) の DNS 解決が可能
  services.tailscale = {
    enable = true;
    # Cloud-Init で注入される auth key file
    # Terraform の tailscale_tailnet_key で ephemeral/preauthorized/tags は設定済み
    authKeyFile = "/var/lib/tailscale/auth-key";
    # Note: authKeyParameters は使用しない（Terraform 側で設定済み）
    # Tailscale hostname は networking.hostName を自動使用
    # ファイアウォールでポートを開く
    openFirewall = true;
  };

  # tailscaled-autoconnect を cloud-init 完了後に実行
  # (auth-key は cloud-init で注入されるため)
  systemd.services.tailscaled-autoconnect = {
    after = [ "cloud-final.service" ];
    wants = [ "cloud-final.service" ];
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
