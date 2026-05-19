# Credential Separation Plans.md

作成日: 2026-05-16

---

## 概要

エージェント用の read-only credential とデプロイ用の write credential を分離する。
現在は Doppler (`homelab/prd`) の既存トークンを共用しているが、
エージェントには最小権限の専用 credential を発行し、誤操作リスクを排除する。

### 設計方針

- **最小権限**: 各レイヤーでエージェント専用の read-only credential を発行
- **Doppler 命名規約**: エージェント用は `*_AGENT_*` プレフィックスで区別
- **既存トークン温存**: デプロイ用の既存 credential は変更しない
- **.envrc 分岐**: エージェント用 credential を優先的に MCP サーバーへ注入

---

## Phase 1: Proxmox credential 分離

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | Proxmox で PVEAuditor ロールの専用ユーザー・トークン作成: `agent@pve` ユーザーを作成し、PVEAuditor ロールを `/` パスに割り当て、privilege separated な API トークンを発行 | Proxmox API で `agent@pve!readonly` トークンを使い `/api2/json/nodes` が取得でき、`/api2/json/nodes/{node}/qemu/{vmid}/config` への PUT が 403 になる | - | cc:完了 |
| 1.2 | Doppler にエージェント用 Proxmox トークン登録: `PROXMOX_AGENT_TOKEN` として格納 | `doppler secrets get PROXMOX_AGENT_TOKEN --project homelab --config prd` で値が取得可能 | 1.1 | cc:完了 |
| 1.3 | `.envrc` 更新: MCP サーバー向けに `PROXMOX_AGENT_TOKEN` を `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` に分離して注入 | `direnv allow .` 後に `PROXMOX_TOKEN_ID` が `agent@pve!readonly` の形式になっている | 1.2 | cc:完了 |

## Phase 2: Kubernetes credential 分離

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 2.1 | `agent-reader` ServiceAccount + ClusterRoleBinding マニフェスト作成: `apps/agent-rbac/` に ServiceAccount, Secret (token), ClusterRoleBinding (→ `view` ClusterRole) を定義 | `kubectl auth can-i get pods --as=system:serviceaccount:default:agent-reader` が yes、`kubectl auth can-i delete pods --as=system:serviceaccount:default:agent-reader` が no | - | cc:完了 |
| 2.2 | ArgoCD Application 追加: `apps/apps.yaml` に `agent-rbac` を追加し、ArgoCD で自動同期 | ArgoCD で `agent-rbac` が Synced / Healthy | 2.1 | cc:TODO |
| 2.3 | エージェント用 kubeconfig 生成 & 配置: `agent-reader` の token を使った kubeconfig を生成し、`~/.kube/agent-config` に配置。`.mcp.json` の kubectl を `KUBECONFIG=~/.kube/agent-config` で起動するよう更新 | kubectl MCP サーバーが `agent-reader` として接続し、`get_pods` が成功、write 系ツールがエラーになる | 2.2 | cc:TODO |

## Phase 3: Tailscale credential 分離

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 3.1 | Tailscale Admin Console で専用 OAuth クライアント作成: `devices:core:read` スコープのみで発行 | Tailscale Admin Console でクライアント ID/Secret が表示される | - | cc:完了 |
| 3.2 | Doppler にエージェント用 Tailscale credential 登録: `TAILSCALE_AGENT_CLIENT_ID` / `TAILSCALE_AGENT_CLIENT_SECRET` として格納 | `doppler secrets get TAILSCALE_AGENT_CLIENT_ID --project homelab --config prd` で値が取得可能 | 3.1 | cc:完了 |
| 3.3 | `.envrc` / `.mcp.json` 更新: Tailscale MCP サーバーがエージェント専用 OAuth credential を使うよう変更 | Tailscale MCP ツール `tailscale_list_devices` が正常に値を返す | 3.2 | cc:完了 |

## Phase 4: 統合テスト

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 4.1 | 全レイヤー read 権限テスト: 各 MCP サーバー経由で読み取り操作が成功することを確認 | Proxmox ノード一覧、K8s Pod 一覧、Tailscale デバイス一覧、ArgoCD アプリ一覧がすべて取得可能 | 1.3, 2.3, 3.3 | cc:TODO |
| 4.2 | 全レイヤー write 拒否テスト: 各レイヤーで write 操作が拒否されることを確認 | Proxmox VM 設定変更が 403、K8s リソース作成/削除が forbidden、Tailscale デバイス操作が 403 | 4.1 | cc:TODO |
| 4.3 | `just preflight` 更新: エージェント専用 credential での接続チェックに更新 | `just preflight` がエージェント credential で全レイヤー到達を確認 | 4.2 | cc:TODO |

## Phase 5: アクセスフロードキュメント

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 5.1 | CLAUDE.md にエージェントアクセスフローセクション追加: credential の流れ (Doppler → .envrc → MCP サーバー)、各レイヤーの権限範囲、トラブルシューティングを更新 | CLAUDE.md に credential 分離後のアクセスフローが記載され、新規セッションから参照可能 | Phase 4 | cc:TODO |
