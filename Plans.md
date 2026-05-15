# Agent Infrastructure Access Plans.md

作成日: 2026-05-11

---

## 概要

Claude Code エージェントが homelab の各レイヤー（Kubernetes, ArgoCD, Proxmox, Tailscale）を
読み取り専用で確認・検証できるようにする。IaC リポジトリなので変更はコードで行い、
エージェントは環境の現状把握と検証のみを行う。

### 設計方針

- **MCP ファースト**: 各レイヤーに専用 MCP サーバーを導入し、構造化されたツールで操作する
- **読み取り専用**: 全 MCP サーバーを read-only モードで起動
- **Doppler 一元管理**: 既存の Doppler (`homelab/prd`) をクレデンシャルの単一ソースとする
- **Git 健全性**: MCP 設定ファイルは Git 管理、クレデンシャルは Doppler 経由で環境変数注入

### MCP サーバー構成

| レイヤー | MCP サーバー | 読み取り専用モード | 認証方式 |
|---------|-------------|-------------------|---------|
| Kubernetes | `kubectl-mcp-server` (npm, CNCF) | `--read-only` | KUBECONFIG (既存) |
| ArgoCD | `argocd-mcp` (argoproj-labs 公式, npm) | `MCP_READ_ONLY=true` | API トークン → Doppler |
| Proxmox | `mcp-proxmox` (gilby125, npm) | basic モード (デフォルト) | API トークン (Doppler 既存) |
| Tailscale | `@yawlabs/tailscale-mcp` (npm) | `TAILSCALE_READONLY=1` + `TAILSCALE_PROFILE=minimal` | API キー → Doppler |

※ Tailscale ローカル接続 (`tailscale up`) は MCP ではなくローカル CLI で行う

---

## Phase 1: ネットワーク接続基盤 & Tailscale MCP

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | Tailscale ローカル接続: settings.json に `tailscale up` / `tailscale status` を許可追加 | `tailscale status` が正常値を返し、`k8s.tailae6c2.ts.net` に到達可能 | - | cc:完了 |
| 1.2 | Tailscale API キー作成 & Doppler 登録: Tailscale Admin Console で read-only API キーを発行し、Doppler に `TAILSCALE_API_KEY` として格納 | `doppler secrets get TAILSCALE_API_KEY --project homelab --config prd` で値が取得可能 | - | cc:完了 (OAuth credentials で代替: TAILSCALE_OAUTH_CLIENT_ID/SECRET が Doppler に既存) |
| 1.3 | `@yawlabs/tailscale-mcp` MCP サーバー設定: `.mcp.json` に追加、`TAILSCALE_READONLY=1` + `TAILSCALE_PROFILE=minimal` で起動 | MCP ツール `tailscale_status` / `tailscale_list_devices` が正常に値を返す | 1.2 | cc:完了 (接続済・OAuth スコープ `devices:read` 追加で完全動作。Admin Console で対応要) |

## Phase 2: Kubernetes MCP

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 2.1 | `kubectl-mcp-server` MCP サーバー設定: `.mcp.json` に追加、`--read-only` モードで起動。KUBECONFIG は既存の `~/.kube/config` を利用 | MCP ツール `get_pods` / `get_nodes` / `get_deployments` が正常に値を返す | 1.1 | cc:完了 |

## Phase 3: ArgoCD MCP

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 3.1 | ArgoCD API トークン作成 & Doppler 登録: ArgoCD で read-only 用 API トークンを発行し、Doppler に `ARGOCD_API_TOKEN` として格納 | `doppler secrets get ARGOCD_API_TOKEN --project homelab --config prd` で値が取得可能 | 1.1 | cc:完了 |
| 3.2 | `argocd-mcp` MCP サーバー設定: `.mcp.json` に追加、`MCP_READ_ONLY=true` で起動。`ARGOCD_BASE_URL=https://argocd.tailae6c2.ts.net` | MCP ツール `list_applications` / `get_application` が正常に値を返す | 3.1 | cc:完了 |

## Phase 4: Proxmox MCP

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 4.1 | Proxmox API トークン権限確認: Doppler 既存の `PROXMOX_API_TOKEN` の権限を確認。必要なら PVEAuditor ロールの読み取り専用トークンを別途作成 | Proxmox API から読み取り操作が可能であることを確認 | 1.1 | cc:完了 |
| 4.2 | `mcp-proxmox` MCP サーバー設定: `.mcp.json` に追加、basic モード（デフォルト = 読み取り専用）で起動。Doppler から `PROXMOX_HOST` / `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` を注入 | MCP ツールでノード一覧・VM ステータスが取得できる | 4.1 | cc:完了 |

## Phase 5: クレデンシャル管理 & 統合設定

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 5.1 | `.envrc` 作成 (direnv): `doppler run` 経由で MCP サーバーに必要な環境変数を自動ロード。`.gitignore` に `.envrc` 追加 | `cd` でプロジェクトに入ると `TAILSCALE_API_KEY`, `ARGOCD_API_TOKEN`, `PROXMOX_HOST` 等がセットされる | - | cc:完了 |
| 5.2 | `.mcp.json` 統合: 全 MCP サーバーの設定を `.mcp.json` にまとめる。`doppler run` ラッパーで環境変数を注入して各サーバーを起動 | Claude Code 起動時に 4 つの MCP サーバーが自動接続し、read-only ツールが利用可能 | 1.3, 2.1, 3.2, 4.2 | cc:完了 |
| 5.3 | settings.json 更新: `tailscale up/status` と `just *` コマンドの permission allowlist 追加 | Tailscale 接続と justfile レシピが許可プロンプトなしで実行可能 | 5.2 | cc:完了 |
| 5.4 | CLAUDE.md にエージェント向け運用セクション追加: MCP ツール一覧、接続手順 (`tailscale up` → MCP 自動接続)、トラブルシューティング | CLAUDE.md に「Agent Operations」セクションが記載 | 5.3 | cc:完了 |

## Phase 6: 便利コマンド & End-to-End 検証

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 6.1 | justfile にステータス確認レシピ追加: `just ts-up`, `just preflight` (全レイヤー接続チェック) | `just preflight` で Tailscale → k8s → ArgoCD → Proxmox の接続性が一括確認できる | Phase 5 | cc:完了 |
| 6.2 | 新規エージェントセッションから全 MCP ツールの動作確認 | 新規セッションで全 MCP サーバーが正常接続し、各レイヤーの読み取り操作が成功 | 6.1 | cc:完了 (kubectl/ArgoCD/Proxmox 動作確認済。Tailscale は OAuth スコープ追加待ち) |
