{ pkgs, ... }:

{
  # システムのステートバージョン (変更しないこと)
  system.stateVersion = "24.11";

  # ホスト名
  networking.hostName = "node01";

  # ネットワーク設定
  networking = {
    useDHCP = false;
    interfaces.eth0.useDHCP = true;

    # ファイアウォール設定
    firewall = {
      enable = true;
      allowedTCPPorts = [
        22   # SSH
        6443 # k3s: API server (pods からアクセス必須)
      ];
    };
  };

  # SSH 設定 (リモートアクセス必須)
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password"; # パスワード禁止、鍵のみ
      PasswordAuthentication = false;
    };
  };

  # SSH 公開鍵 (デプロイ用)
  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL4S2N2p2y0UzkeNl83VOIzvtAnIzvhIatnbnMWy2BOL"
  ];

  # k3s サービス設定
  services.k3s.enable = true;
  services.k3s.role = "server";
  services.k3s.extraFlags = toString [
    "--disable traefik" # Traefik を無効化（ArgoCD の LoadBalancer で使用するため）
    # "--debug" # オプション: デバッグモード有効化
  ];

  # システムパッケージ
  environment.systemPackages = with pkgs; [
    vim
    git
    wget
    curl
    htop
    kubectl
    kubernetes-helm
  ];

  # kubectl 用環境変数
  environment.sessionVariables = {
    KUBECONFIG = "/etc/rancher/k3s/k3s.yaml";
  };

  # タイムゾーン
  time.timeZone = "Asia/Tokyo";

  # ロケール設定
  i18n.defaultLocale = "en_US.UTF-8";

  # Nix 設定
  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];

    # バイナリキャッシュ（Cachix）
    substituters = [
      "https://cache.nixos.org"
      "https://hikuohiku.cachix.org"
    ];
    trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "hikuohiku.cachix.org-1:jle9MrU7hoFm0IJrdEFuBCsVnHaZfyLsJ+rpLuMfOLM="
    ];
  };
}
