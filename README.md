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

### 2. シークレットの設定 (Doppler)

本プロジェクトでは、Terraformの変数をDopplerで管理しています。
`homelab` プロジェクトを作成し、`prd` 環境に以下のSecretを登録してください：

| Key | Description | Example |
|-----|-------------|---------|
| `PROXMOX_ENDPOINT` | Proxmox VE API Endpoint | `https://192.168.0.100:8006` |
| `PROXMOX_API_TOKEN` | Proxmox API Token | `root@pam!terraform=xxxxxxxx` |
| `PROXMOX_SSH_USERNAME` | SSH User | `root` |
| `PROXMOX_NODE` | Proxmox Node Name | `pve` |
| `SSH_PUBLIC_KEY` | VM SSH Public Key | `ssh-ed25519 ...` |
| `AGE_PRIVATE_KEY` | sops-nix Age Private Key | `AGE-SECRET-KEY-...` |
| `GITHUB_REPO` | NixOS Flake Repo | `username/repo` |

### 3. Terraformの実行

`just` コマンドを使用します。Dopplerから自動的に変数が注入されます。

```bash
# 初期化
just plan

# 適用
just apply
```

## ファイル構成

```
.
├── README.md     # このファイル
├── providers.tf  # Terraformプロバイダー設定
├── variables.tf  # 共通変数定義
├── main.tf       # カスタムVM構成ファイル
├── pbs.tf        # PBS VM構成ファイル
└── .gitignore    # Git除外設定
```

## よく使うコマンド

```bash
# 設定の検証
terraform validate

# フォーマット
terraform fmt

# 現在の状態確認（Terraform Cloud上の状態を表示）
terraform show

# リソースの削除
terraform destroy

# 特定のリソースのみ作成
terraform apply -target=proxmox_virtual_environment_vm.pbs

# Terraform Cloud WebUIで状態確認
# https://app.terraform.io/app/your-org-name/workspaces/homelab
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

## Terraform Cloud のメリット

- **状態ファイルの安全な管理**: ローカルに保存せず、暗号化して保存
- **チーム共有**: 複数人で同じインフラを管理可能
- **ステートロック**: 同時実行を自動で防止
- **バージョン履歴**: 過去の状態に戻すことが可能
- **変数管理**: センシティブな変数を安全に保存
- **実行履歴**: いつ誰が何を変更したか記録

## トラブルシューティング

### SSL証明書エラー

ProxmoxがデフォルトでHTTPSを使用しています。自己署名証明書を使用している場合、証明書エラーが発生する可能性があります。

**推奨対応**:
- Proxmoxに有効なSSL証明書をインストール（Let's Encryptなど）

**自己署名証明書を許可する場合**:
- `providers.tf`の`insecure = false`を`insecure = true`に変更

### 認証エラー

- APIトークンの形式を確認: `username@realm!tokenid=secret`
- Proxmox WebUIでトークンが有効か確認
- Privilege Separationが無効化されているか確認（全権限が必要）
- トークンシークレットが正しいか確認（コピペミスに注意）

### Tailscale接続エラー

- Tailscaleが起動していることを確認
- ProxmoxホストがTailscaleネットワークに接続されていることを確認
- エンドポイントURLが正しいことを確認（例: `https://proxmox:8006`）

### Terraform Cloudエラー

**「No valid credential sources found」**
```bash
terraform login
```
を実行してAPIトークンを取得

**「organization not found」**
- `providers.tf`のorganization名が正しいか確認
- Terraform Cloud WebUIで組織が存在するか確認

**「workspace not found」**
- 初回`terraform init`時に自動作成されます
- 手動作成する場合: Terraform Cloud WebUI → New Workspace

## 参考リンク

- [Terraform Cloud Documentation](https://developer.hashicorp.com/terraform/cloud-docs)
- [bpg/proxmox Provider Documentation](https://registry.terraform.io/providers/bpg/proxmox/latest/docs)
- [Terraform Documentation](https://www.terraform.io/docs)
- [Proxmox VE Documentation](https://pve.proxmox.com/wiki/Main_Page)
