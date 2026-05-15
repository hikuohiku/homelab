# CLAUDE.md — homelab

## Project Overview

Proxmox VE 上の homelab インフラを管理する IaC リポジトリ。
Terraform で VM プロビジョニング、NixOS でOS構成、Kubernetes (ArgoCD) でアプリケーションデプロイを行う。

## Tech Stack

- **Provisioning**: Terraform (Proxmox provider)
- **OS**: NixOS (flake-based)
- **Kubernetes**: ArgoCD (App of Apps pattern)
- **Networking**: Tailscale
- **Secrets**: SOPS, External Secrets Operator
- **Auth**: Dex (Google OAuth)
- **Apps**: Nextcloud, ArgoCD, Tailscale Operator

## Directory Structure

```
terraform/proxmox/   — VM provisioning (Proxmox VE)
nix/images/          — NixOS image definitions
apps/                — Kubernetes manifests (ArgoCD applications)
  apps.yaml          — App of Apps root
  argocd/            — ArgoCD self-management
  nextcloud/         — Nextcloud deployment
  dex/               — Dex OIDC provider
  external-secrets/  — External Secrets Operator
  tailscale-operator/ — Tailscale networking
secrets/             — Encrypted secrets (SOPS)
```

## Development Commands

```bash
# Terraform
cd terraform/proxmox && terraform init
cd terraform/proxmox && terraform plan
cd terraform/proxmox && terraform apply

# NixOS image build
nix build .#nixosConfigurations.<name>.config.system.build.image
```

## Agent Operations

エージェントが homelab 環境を読み取り専用で参照するための MCP サーバー構成。

### 接続手順

1. `tailscale up` — Tailscale ネットワークに接続（`just ts-up`）
2. Claude Code 起動時に `.mcp.json` から 4 つの MCP サーバーが自動接続

### MCP サーバー一覧

| サーバー | ツール例 | 用途 |
|---------|---------|------|
| `kubectl` | `get_nodes`, `get_pods`, `get_deployments` | K8s リソース参照 |
| `argocd` | `list_applications`, `get_application` | ArgoCD アプリ状態確認 |
| `proxmox` | `proxmox_node_list`, `proxmox_vm_list` | VM/ノード状態確認 |
| `tailscale` | `tailscale_list_devices`, `tailscale_status` | ネットワークデバイス参照 |

### クレデンシャル

- `.envrc` (direnv) 経由で Doppler (`homelab/prd`) からシークレットを自動ロード
- `.mcp.json` の `env` フィールドで read-only フラグを設定

### トラブルシューティング

- `just preflight` で全レイヤーの接続性を一括確認
- MCP サーバーが接続しない場合: `tailscale status` でネットワーク確認 → `direnv allow .` で環境変数確認
- Tailscale MCP の `fetch failed`: OAuth クライアントに `devices:read` スコープが必要（Tailscale Admin Console）

## Conventions

- Kubernetes manifests は Kustomize で管理
- シークレットは SOPS で暗号化して Git 管理
- ArgoCD Application は apps/ 配下に個別ディレクトリ
- 言語: 日本語でコミュニケーション

## Git / .gitignore

以下は Git 追跡しない（.gitignore に含める）:

- `.claude/state/` — セッションごとの一時データ
- `.claude/sessions/` — セッションデータ
- `.claude-plugin/` — `harness sync` で再生成される自動生成物

以下は Git 管理する:

- `.claude/settings.json` — プロジェクト共有のパーミッション設定
- `harness.toml` — Harness 設定のソース
- `hooks/hooks.json` — フック定義のソース
