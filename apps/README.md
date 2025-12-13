# ArgoCD App of Apps セットアップ

このディレクトリは App of Apps パターンで Kubernetes アプリケーションを管理します。

## 前提条件

- k3s が稼働中
- kubectl がインストール済み
- Tailscale アカウント（Tailnet公開用）
- Doppler アカウント（Secret管理用）

## Phase 1: K3s シークレット設定 (初回のみ)

事前に K3s の CA と Token を生成し、Doppler に保存します。これにより、再構築後も同じ Kubeconfig が使用可能になります。

1. **CA と Token の生成・Doppler への保存**:
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

   ※ 既存のサーバーから抽出する場合は、Bastion経由で取得します:
   ```bash
   ssh -J root@hikuo-homeserver root@192.168.0.129 "cat /var/lib/rancher/k3s/server/token"
   ```

2. **シークレットの注入**:
   VM を再構築した直後に以下を実行します:
   ```bash
   just inject-secrets
   ```

3. **Kubeconfig の取得 (初回のみ)**:
   ```bash
   scp root@192.168.0.129:/etc/rancher/k3s/k3s.yaml ~/.kube/node01-config
   sed -i '' 's|https://127.0.0.1:6443|https://192.168.0.129:6443|g' ~/.kube/node01-config
   export KUBECONFIG=$HOME/.kube/node01-config
   kubectl get nodes
   ```
   ※ 次回以降は、`just inject-secrets` を実行すれば、既存の `~/.kube/node01-config` がそのまま使えます。

## Phase 2: Doppler セットアップ

External Secrets Operator が Secret を自動生成するために Doppler を使用します。

1. [Doppler](https://doppler.com) でアカウント作成
2. Projectを作成 (例: `homelab`)
3. 以下のSecretを追加:
   - `TAILSCALE_CLIENT_ID`
   - `TAILSCALE_CLIENT_SECRET`
4. Service Token を生成 (Access → Generate Service Token)
5. クラスタに Token を登録:

```bash
kubectl create namespace external-secrets
kubectl create secret generic doppler-token \
  --namespace external-secrets \
  --from-literal=token=dp.st.xxxx
```

## Phase 3: ArgoCD 初回インストール

```bash
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

helm install argocd argo/argo-cd \
  --namespace argocd \
  --create-namespace \
  --version 9.1.6 \
  --values apps/argocd/values.yaml

kubectl get pods -n argocd
```

## Phase 4: 初期パスワード取得

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
echo
```

## Phase 5: App of Apps 適用

```bash
kubectl apply -f apps/apps.yaml
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

## Phase 6: ArgoCD UI アクセス

```bash
# Tailscale ホスト名を確認
kubectl get svc -n argocd argocd-server -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

# ブラウザで http://<tailscale-hostname> にアクセス
```

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
