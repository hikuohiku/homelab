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

# ArgoCD preview deploy (フィーチャーブランチのテスト)
just preview <app> <branch>     # アプリをブランチに切り替え
just preview-reset <app>        # HEAD に戻す
just preview-status             # preview 中のアプリ一覧

# ArgoCD bootstrap (ルート Application 再適用)
just argocd-bootstrap
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

### Credential 分離

エージェント用 read-only credential とデプロイ用 write credential は分離されている。

```
Doppler (homelab/prd)
  ├── PROXMOX_AGENT_TOKEN (PVEAuditor) → .envrc → PROXMOX_TOKEN_ID/SECRET → Proxmox MCP
  ├── TAILSCALE_AGENT_CLIENT_ID/SECRET (devices:core:read) → .envrc → TAILSCALE_OAUTH_CLIENT_ID/SECRET → Tailscale MCP
  ├── ARGOCD_API_TOKEN (agent account, get-only RBAC) → .envrc → ArgoCD MCP
  └── KUBECONFIG (既存, --read-only フラグで制限) → kubectl MCP
```

各レイヤーの権限範囲:

| レイヤー | credential | 権限 | 制限方式 |
|---------|-----------|------|---------|
| Proxmox | `PROXMOX_AGENT_TOKEN` | PVEAuditor (Audit系のみ) | API トークン権限分離 |
| K8s | 既存 kubeconfig | 全権限（Tailscaleプロキシ制約） | MCP `--read-only` フラグ |
| ArgoCD | `ARGOCD_API_TOKEN` | applications/projects/clusters の get のみ | RBAC policy |
| Tailscale | `TAILSCALE_AGENT_CLIENT_ID/SECRET` | devices:core:read | OAuth スコープ |

> **注意**: K8s は Tailscale API プロキシが Bearer トークンを無視するため、ServiceAccount ベースの分離ができない（#36）。MCP の `--read-only` フラグが実効的なセキュリティ境界。

### エージェント操作ルール

- **インフラ参照は MCP ツール経由で行う**: `mcp__kubectl__*`, `mcp__argocd__*`, `mcp__proxmox__*`, `mcp__tailscale__*` を使う
- **kubectl / curl 等の CLI を直接使わない**: CLI は管理者権限の kubeconfig を使うため credential 分離が無効になる。MCP サーバーが read-only 制約を担保している
- **例外**: `tailscale up` / `tailscale status` / `just *` は CLI 許可済み（MCP 非対応の操作）

### トラブルシューティング

- `just preflight` で全レイヤーの接続性を一括確認
- MCP サーバーが接続しない場合: `tailscale status` でネットワーク確認 → `direnv allow .` で環境変数確認
- Proxmox MCP がエラー → `PROXMOX_AGENT_TOKEN` が Doppler に登録されているか確認
- Tailscale MCP の `fetch failed`: OAuth クライアントに `devices:core:read` スコープが必要（Tailscale Admin Console）
- credential 変更後は Claude Code 再起動が必要（MCP サーバーが起動時に env を読み込むため）

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
