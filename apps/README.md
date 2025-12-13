# ArgoCD App of Apps セットアップ

このディレクトリは App of Apps パターンで Kubernetes アプリケーションを管理します。

## 前提条件

- Tailscale アカウント（Tailnet公開用）
- Doppler アカウント（Secret管理用）
- sops + Age（Secret暗号化用）

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────┐
│ NixOS (k3s-manifests.nix) - Bootstrap Layer                     │
│ ┌─────────────┐ ┌───────────────────┐ ┌───────────────────────┐ │
│ │ Namespace   │ │ ArgoCD HelmChart  │ │ App of Apps           │ │
│ │ (argocd,    │ │ (最低限の設定)     │ │ (apps/ を参照)        │ │
│ │ ext-secrets)│ │                   │ │                       │ │
│ └─────────────┘ └───────────────────┘ └───────────────────────┘ │
│                 ┌───────────────────┐                           │
│                 │ doppler-token     │ ← sops-nix で自動生成    │
│                 │ Secret            │                           │
│                 └───────────────────┘                           │
└────────────────────────────────┬────────────────────────────────┘
                                 │ 自動適用
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ ArgoCD App of Apps - GitOps Layer                               │
│ ┌─────────────┐ ┌─────────────┐ ┌────────────────────┐          │
│ │ ArgoCD      │ │ ESO         │ │ Tailscale Operator │          │
│ │ (values上書)│ │ + Doppler   │ │                    │          │
│ └─────────────┘ └─────────────┘ └────────────────────┘          │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼ GitHub (apps/)
```

## 初回セットアップ

NixOS の `k3s-manifests.nix` がすべてを自動でブートストラップします：

1. **Namespace**: argocd, external-secrets
2. **doppler-token Secret**: sops-nix で暗号化された `secrets.yaml` から自動生成
3. **ArgoCD** (Helm Chart)
4. **App of Apps**: 残りのコンポーネントを自動デプロイ



### 必要な作業（Terraform 実行前）

1. Age キーペアを生成して Doppler に保存 (`AGE_PRIVATE_KEY`)
2. `secrets.yaml` を暗号化して Git にコミット
3. Doppler に必要な変数を設定 (`PROXMOX_*`, `GITHUB_REPO` 等)

詳細は [Verification Plan](#verification-plan) を参照。

## セットアップ確認

Tailscale API Proxy 経由で kubectl アクセス:

```bash
kubectl --context=tailscale-operator-context get pods -A
kubectl --context=tailscale-operator-context get application -n argocd
```

**期待される出力:**
```
NAME                 SYNC STATUS   HEALTH STATUS
apps                 Synced        Healthy
argocd               Synced        Healthy
external-secrets     Synced        Healthy

tailscale-operator   Synced        Healthy
```

## ArgoCD UI アクセス

```bash
# Tailscale ホスト名を確認
kubectl get svc -n argocd argocd-server -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

# ブラウザで http://<tailscale-hostname> にアクセス
```

初期パスワード:
```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
echo
```

---

## 事前設定 (初回のみ)

### K3s シークレットの事前生成

事前に K3s の CA と Token を生成し、Doppler に保存します:

```bash
# 一時ディレクトリで作業
mkdir -p /tmp/k3s-scrt && cd /tmp/k3s-scrt

# CA 生成
openssl genrsa -out server-ca.key 2048
openssl req -x509 -new -nodes -key server-ca.key -sha256 -days 3650 -out server-ca.crt -subj "/CN=k3s"

# Token 生成
K3S_TOKEN=$(openssl rand -hex 32)

# Doppler に保存
doppler secrets set K3S_TOKEN="$K3S_TOKEN" \
  K3S_CA_CERT="$(cat server-ca.crt)" \
  K3S_CA_KEY="$(cat server-ca.key)"

# クリーンアップ
cd - && rm -rf /tmp/k3s-scrt
```

### Doppler プロジェクト設定

1. [Doppler](https://doppler.com) でアカウント作成
2. Project を作成 (例: `homelab`)
3. 以下の Secret を追加:
   - `TAILSCALE_CLIENT_ID`
   - `TAILSCALE_CLIENT_SECRET`
4. Service Token を生成 (Access → Generate Service Token)

### VM 再構築後

```bash
just inject-secrets
```

---

## ディレクトリ構造

```
apps/
├── apps.yaml              # App of Apps ルート
├── kustomization.yaml     # Application 一覧
├── argocd/
├── external-secrets/
└── tailscale-operator/
```

各アプリは以下の構造:
```
apps/<app-name>/
├── application.yaml    # ArgoCD Application (path参照のみ)
├── kustomization.yaml  # helmCharts + resources
└── (その他リソース)
```

## 新しいアプリケーションの追加

1. `apps/<app-name>/` ディレクトリ作成
2. `application.yaml` と `kustomization.yaml` を作成
3. `apps/kustomization.yaml` に追記
4. Git push → ArgoCD が自動適用
