{ config, pkgs, ... }:

{
  # システムのステートバージョン (変更しないこと)
  system.stateVersion = "24.11";

  # ホスト名
  networking.hostName = "node01";

  # ネットワーク設定 (DHCP)
  networking = {
    useDHCP = false;
    interfaces.eth0.useDHCP = true;  # cloud image のデフォルトインターフェース

    # ファイアウォール設定
    firewall = {
      enable = true;
      allowedTCPPorts = [ 22 ];  # SSH
    };
  };

  # SSH 設定 (リモートアクセス必須)
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";  # パスワード禁止、鍵のみ
      PasswordAuthentication = false;
    };
  };

  # SSH 公開鍵 (デプロイ用)
  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL4S2N2p2y0UzkeNl83VOIzvtAnIzvhIatnbnMWy2BOL"
  ];

  # システムパッケージ
  environment.systemPackages = with pkgs; [
    vim
    git
    wget
    curl
    htop
  ];

  # タイムゾーン
  time.timeZone = "Asia/Tokyo";

  # ロケール設定
  i18n.defaultLocale = "en_US.UTF-8";

  # Nix flakes を有効化
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
}
