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
- **Apps**: ArgoCD, Dex, External Secrets Operator, Tailscale Operator, Immich, Vaultwarden, Coder, ops-health-reporter

## Directory Structure

```
terraform/proxmox/   — VM provisioning (Proxmox VE)。node01 のみ管理。pbs(qemu/112) は手動管理（Terraform 管理対象外、記録は pbs.tf.ignore）
nix/images/          — NixOS image definitions
apps/                — Kubernetes manifests (ArgoCD applications)
  apps.yaml          — App of Apps root
  argocd/            — ArgoCD self-management
  agent-rbac/        — エージェント用 read-only ServiceAccount/RBAC
  dex/               — Dex OIDC provider
  external-secrets/  — External Secrets Operator
  tailscale-operator/ — Tailscale networking
  immich/            — Immich (写真管理)
  vaultwarden/       — Vaultwarden (パスワード管理)
  coder/             — Coder (開発環境)
  ops-health-reporter/ — autopilot 向け ArgoCD/k8s 健全性レポーター（CronJob）
.sops.yaml            — SOPS 設定（暗号鍵の対象範囲）
nix/images/proxmox-cloud/secrets.yaml — SOPS 暗号化ファイル（単一ファイル、トップレベルの secrets/ ディレクトリは存在しない）
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
```

## Maintenance

pin したバージョンは誰も上げなければ据え置かれる。2026-08-03、vaultwarden 1.36.0 の
放置でクライアント同期が全停止した（#49）。

週次点検の手順は `/weekly-maintenance`（`.claude/commands/weekly-maintenance.md`）、
実行記録は `Maintenance.md`。対象は vaultwarden のみで、手順は毎回の振り返りで更新する。

node01 の root disk は 256 GiB（2026-08-04 にオンライン拡張）。`local-path` PVC の容量は
実ディスクを予約しないため、実使用量は引き続き監視する。拡張手順は
[`docs/node01-storage.md`](docs/node01-storage.md)。

## Autopilot（自律運用エージェント）

`ops/` 配下に、homelab を人間の介入なしに保守し続けるエージェントの器がある。
クラウドの定期実行が独立セッションを起こし、backlog からタスクを取って PR を出す。実行間隔は
固定値をここに書かず `ops/state.json` の `routines` を単一の情報源とする（頻度は運用状況に応じて
変わりうる。2026-08-05 時点は毎時）。

| ファイル | 役割 |
|---------|------|
| `ops/VISION.md` | 何を目指しているか。**起動時に最初に読む** |
| `ops/CHARTER.md` | 行動規範。auto-merge の条件、必ず人間に渡すもの |
| `ops/backlog.json` | タスクキュー |
| `ops/inventory.json` | バージョン監視対象 |
| `ops/journal/` | 起動ごとの引き継ぎ記録 |
| `ops/validate.py` | 状態ファイルの不変条件検査（CI から実行） |
| `ops/dashboard/build.py` | 人間向けダッシュボードの生成 |

- フィードバック窓口: [#56](https://github.com/hikuohiku/homelab/issues/56)（この issue にコメントすると次の起動で読まれる）
- ダッシュボード: `ops/state.json` の `dashboard.artifact_url`
- 定期実行の設定: `ops/state.json` の `routines`

**人間がこのリポジトリを触るときの注意**: `ops/backlog.json` と `ops/state.json` は
autopilot が直接 `main` に push する。コンフリクトを避けるため、手で編集するときは
issue 経由で依頼するか、編集後すぐ push すること。

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
- **インフラの参照（read）は MCP を使う**: CLI は管理者権限の kubeconfig を使うため credential 分離が無効になる。参照は MCP サーバーが read-only 制約を担保しているので MCP 経由を原則とする
- **kubectl の書き込み（write）は `kubectl` CLI で行う**: MCP は read-only のため、デプロイ検証・データ移行など write が必要な操作は CLI を使う。
  以前は `.claude/settings.json` の `permissions.ask` で毎回人間の承認を求めていたが、**`ask` は全廃した**。
  ヘッドレスの定期実行（[`ops/CHARTER.md`](ops/CHARTER.md)）では誰も承認できず、プロンプトに当たった時点で
  その起動が丸ごと無駄になるため（2026-08-04, run #1 が実際にこれで消えた）。
  実質の歯止めは「変更は Git → CI → ArgoCD を通す」という経路そのものと、憲章 §5 が担う
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
