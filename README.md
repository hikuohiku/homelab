# Homelab Infrastructure as Code

Proxmox VE環境をTerraformで管理するためのIaCリポジトリです。

## 前提条件

- Terraform 1.14.0
- Proxmox VE 8.x以上
- Tailscale経由でProxmoxに接続

## セットアップ

### 1. リポジトリのクローン

```bash
git clone <repository-url>
cd homelab
```

### 2. Terraform設定ファイルの作成

```bash
cp terraform.tfvars.example terraform.tfvars
```

`terraform.tfvars`を編集して、Proxmox環境に合わせて設定を変更してください：

```hcl
proxmox_endpoint     = "https://your-proxmox-host:8006"
proxmox_username     = "root@pam"
proxmox_password     = "your-password"
proxmox_insecure     = true  # 自己署名証明書の場合はtrue
```

### 3. Terraformの初期化

```bash
terraform init
```

### 4. プランの確認

```bash
terraform plan
```

### 5. リソースの作成

```bash
terraform apply
```

## ファイル構成

```
.
├── README.md                    # このファイル
├── providers.tf                 # Terraformプロバイダー設定
├── variables.tf                 # 共通変数定義
├── terraform.tfvars.example     # 設定テンプレート
├── main.tf                      # サンプルVM構成ファイル
├── pbs.tf                       # PBS VM構成ファイル
└── .gitignore                   # Git除外設定
```

## よく使うコマンド

```bash
# 設定の検証
terraform validate

# フォーマット
terraform fmt

# 現在の状態確認
terraform show

# リソースの削除
terraform destroy

# 特定のリソースのみ作成
terraform apply -target=proxmox_virtual_environment_vm.pbs
```

## Proxmox Backup Server構築

Proxmox Backup Server (PBS)をISOイメージからインストールする手順です。

### 1. PBS ISOのアップロード

Proxmox WebUIからISOイメージをアップロード：

```bash
# Proxmoxホストで直接ダウンロードする場合
ssh root@proxmox-host
cd /var/lib/vz/template/iso/
wget https://enterprise.proxmox.com/iso/proxmox-backup-server_3.2-1.iso
```

または、Proxmox WebUI から：
1. Datacenter → ストレージ (local) を選択
2. ISO Images タブ
3. Upload または Download from URL

### 2. PBS設定

`pbs.tf`を直接編集して環境に合わせて設定を変更：

```hcl
# ISOファイル名を環境に合わせて変更
cdrom {
  file_id   = "local:iso/proxmox-backup-server_3.2-1.iso"
  interface = "ide2"
}

# 必要に応じてリソースを調整
cpu {
  cores = 4  # CPUコア数
}

memory {
  dedicated = 8192  # メモリ (MB)
}

disk {
  datastore_id = "local-lvm"  # ストレージ名
  size         = 64           # ディスクサイズ (GB)
}
```

### 3. PBS VMの作成

```bash
# PBS VMのみ作成する場合
terraform apply -target=proxmox_virtual_environment_vm.pbs

# または全リソースを作成
terraform apply
```

**注意**: VMは停止状態で作成されます（インストール作業のため）。

### 4. PBS インストール

1. Proxmox WebUIでPBS VMを選択
2. コンソールを開く
3. VMを起動（Start ボタン）
4. PBS インストーラーが起動
5. 画面の指示に従ってインストール:
   - タイムゾーン設定
   - 管理者パスワード設定
   - ネットワーク設定（DHCP）
   - ディスク選択
6. インストール完了後、VMを再起動

### 5. PBS初期セットアップ

インストール後、PBS WebUIにアクセス：

```
https://<pbs-ip>:8007
```

デフォルトログイン:
- ユーザー名: `root`
- パスワード: インストール時に設定したパスワード

### 6. S3データストアの設定

PBS WebUIで：

1. **Datastore** → **Add Datastore** → **S3**
2. 以下の情報を入力:
   - Name: データストア名（例: `s3-backup`）
   - Bucket: S3バケット名
   - Region: リージョン（例: `us-east-1`）
   - Endpoint: S3互換ストレージのエンドポイント
   - Access Key ID: アクセスキー
   - Secret Access Key: シークレットキー

または、CLIで設定：

```bash
# PBS ホストにSSH接続
ssh root@<pbs-ip>

# S3データストアの追加
proxmox-backup-manager datastore create s3-backup \
  --backing-type s3 \
  --bucket your-bucket-name \
  --endpoint https://s3.example.com \
  --access-key-id YOUR_ACCESS_KEY \
  --secret-access-key YOUR_SECRET_KEY
```

### 7. CDROMの削除（インストール完了後）

インストール完了後、CDROMが不要になります：

1. `pbs.tf`を編集してcdromブロックを削除またはコメントアウト
2. または、`file_id = "none"`に変更

```hcl
# cdrom {
#   file_id   = "local:iso/proxmox-backup-server_3.2-1.iso"
#   interface = "ide2"
# }

# または
cdrom {
  file_id   = "none"
  interface = "ide2"
}
```

3. 変更を適用:

```bash
terraform apply
```

### 8. PBS VMの管理

```bash
# PBS VMのみ削除
terraform destroy -target=proxmox_virtual_environment_vm.pbs

# PBS VM情報の確認
terraform state show proxmox_virtual_environment_vm.pbs
```

## トラブルシューティング

### SSL証明書エラー

Tailscaleは自己署名証明書を使用するため、`terraform.tfvars`で`proxmox_insecure = true`を設定してください。

### 認証エラー

- Proxmoxのユーザー名は`root@pam`または`root@pve`の形式で指定
- APIトークンを使用する場合は、プロバイダー設定を変更

### Tailscale接続エラー

- Tailscaleが起動していることを確認
- ProxmoxホストがTailscaleネットワークに接続されていることを確認
- エンドポイントURLが正しいことを確認（例: `https://proxmox:8006`）

## 参考リンク

- [bpg/proxmox Provider Documentation](https://registry.terraform.io/providers/bpg/proxmox/latest/docs)
- [Terraform Documentation](https://www.terraform.io/docs)
- [Proxmox VE Documentation](https://pve.proxmox.com/wiki/Main_Page)
