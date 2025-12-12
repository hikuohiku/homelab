# ArgoCD App of Apps セットアップ

このディレクトリは App of Apps パターンで Kubernetes アプリケーションを管理します。

## 前提条件

- k3s が稼働中
- kubectl と helm がインストール済み
- KUBECONFIG が設定済み
- Tailscale アカウント（Tailnet公開を使用する場合）

## Phase 1: KUBECONFIG の取得

**ローカル（Mac）で実行:**

```bash
# node01 から KUBECONFIG を取得
scp root@192.168.0.129:/etc/rancher/k3s/k3s.yaml ~/.kube/node01-config

# サーバーアドレスを node01 の IP に変更
sed -i '' 's|https://127.0.0.1:6443|https://192.168.0.129:6443|g' ~/.kube/node01-config

# KUBECONFIG 環境変数を設定
export KUBECONFIG=$HOME/.kube/node01-config

# 接続確認
kubectl get nodes
```

## Phase 2: Tailscale Operator の事前セットアップ

ArgoCD を Tailnet 内に公開するため、Tailscale Operator 用の OAuth シークレットを作成します。

### 2.1 OAuth クライアントの作成

1. [Tailscale Admin Console - OAuth](https://login.tailscale.com/admin/settings/oauth) にアクセス
2. "Generate OAuth client" をクリック
3. 以下のスコープを選択:
   - `devices:read`
   - `devices:write`
   - `auth_keys`
4. Client ID と Client Secret を保存

### 2.2 Secret の作成

```bash
# tailscale namespace を作成
kubectl create namespace tailscale

# OAuth credentials を Secret として作成
kubectl create secret generic operator-oauth \
  --namespace tailscale \
  --from-literal=client_id=<your-client-id> \
  --from-literal=client_secret=<your-client-secret>
```

## Phase 3: ArgoCD を Helm で初回インストール

**ローカル（Mac）で実行:**

```bash
# Helm リポジトリを追加
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

# ArgoCD をインストール（values.yaml を適用）
helm install argocd argo/argo-cd \
  --namespace argocd \
  --create-namespace \
  --version 9.1.6 \
  --values apps/argocd/values.yaml

# インストール確認（Pod が起動するまで数分かかります）
kubectl get pods -n argocd
kubectl get svc -n argocd
```

## Phase 4: 初期パスワード取得

```bash
# admin パスワードを取得
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
echo
```

## Phase 5: ArgoCD UI アクセス

### 初回（Tailscale Operator デプロイ前）

```bash
# port-forward でアクセス
kubectl port-forward svc/argocd-server -n argocd 8080:80

# ブラウザで http://localhost:8080 にアクセス
```

### Tailnet 経由（App of Apps デプロイ後）

```bash
# Tailscale が割り当てたホスト名を確認
kubectl get svc -n argocd argocd-server -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

# ブラウザで http://<tailscale-hostname> にアクセス
```

**ログイン情報:**
- ユーザー名: admin
- パスワード: (Phase 4 で取得したパスワード)

## Phase 6: App of Apps パターンで自己管理へ移行

```bash
# Apps Application を適用（apps/ ディレクトリ内の全ての Application を管理）
kubectl apply -f apps/apps.yaml

# Application の状態を確認
kubectl get application -n argocd
```

**期待される出力:**
```
NAME                 NAMESPACE  STATUS  HEALTH   SYNCPOLICY
apps                 argocd     Synced  Healthy  Auto
argocd               argocd     Synced  Healthy  Auto
tailscale-operator   argocd     Synced  Healthy  Auto
```

これで Tailscale Operator が自動デプロイされ、ArgoCD が Tailnet 内に公開されます。

## 新しいアプリケーションの追加方法

1. `apps/<app-name>/application.yaml` を作成
2. `apps/kustomization.yaml` の resources に追記
3. Git にコミット & プッシュ

ArgoCD が自動的に新しい Application を検出して適用します。
