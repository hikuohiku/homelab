# ArgoCD App of Apps セットアップ

このディレクトリは App of Apps パターンで Kubernetes アプリケーションを管理します。

## 前提条件

- Tailscale アカウント（Tailnet公開用）
- Doppler アカウント（Secret管理用）

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────┐
│ NixOS (k3s-manifests.nix)                                       │
│ ┌─────────────┐ ┌─────────────┐ ┌─────────────────────────────┐ │
│ │ Namespaces  │ │ HelmCharts  │ │ ClusterSecretStore/         │ │
│ │             │ │ (ArgoCD,    │ │ ExternalSecret/App of Apps  │ │
│ │             │ │ ESO, TS-OP) │ │                             │ │
│ └─────────────┘ └─────────────┘ └─────────────────────────────┘ │
└────────────────────────────────┬────────────────────────────────┘
                                 │ 自動適用
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ K3s クラスタ                                                     │
│ ┌─────────────┐ ┌─────────────┐ ┌────────────────────┐          │
│ │ ArgoCD      │ │ ESO         │ │ Tailscale Operator │          │
│ │             │ │             │ │                    │          │
│ │ App of Apps ├─┤ Doppler連携 ├─┤ operator-oauth     │          │
│ └─────────────┘ └─────────────┘ └────────────────────┘          │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼ GitOps
┌─────────────────────────────────────────────────────────────────┐
│ GitHub (apps/)                                                  │
│ 以降のアプリ追加・変更は ArgoCD 経由で自動適用                     │
└─────────────────────────────────────────────────────────────────┘
```

## 初回セットアップ (2ステップ)

NixOS の `k3s-manifests.nix` により、以下のコンポーネントは VM 起動時に自動デプロイされます:
- Namespace (argocd, external-secrets, tailscale)
- ArgoCD (Helm Chart)
- External Secrets Operator (Helm Chart)
- Tailscale Operator (Helm Chart)
- ClusterSecretStore (Doppler)
- ExternalSecret (Tailscale OAuth)
- App of Apps

### Step 1: Doppler トークンの登録

SSH 経由で `doppler-token` Secret を作成します:

```bash
ssh -J root@hikuo-homeserver root@192.168.0.129 \
  "kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f - && \
   kubectl create secret generic doppler-token \
     --namespace external-secrets \
     --from-literal=token=dp.st.xxxx \
     --dry-run=client -o yaml | kubectl apply -f -"
```

### Step 2: Kubeconfig のセットアップ

Tailscale K8s Operator 経由で kubeconfig を取得します:

```bash
tailscale configure kubeconfig <operator-hostname>
kubectl get nodes
```

※ `<operator-hostname>` は Tailscale Admin Console または `tailscale status` で確認できます。

## セットアップ確認

```bash
kubectl get pods -A
kubectl get application -n argocd
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
