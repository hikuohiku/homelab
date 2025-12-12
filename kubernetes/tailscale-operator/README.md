# Tailscale Kubernetes Operator セットアップ

Tailscale Kubernetes Operator を使用して、k8s サービスを Tailnet 内に公開します。

## 前提条件

- k3s が稼働中
- ArgoCD がインストール済み
- Tailscale アカウント

## Phase 1: OAuth クライアントの作成

1. [Tailscale Admin Console](https://login.tailscale.com/admin/settings/oauth) にアクセス
2. "Generate OAuth client" をクリック
3. 以下のスコープを選択:
   - `devices:read`
   - `devices:write`
   - `auth_keys`
4. "Generate" をクリック
5. Client ID と Client Secret を保存（Secret は一度しか表示されません）

## Phase 2: Secret の作成

```bash
# tailscale namespace を作成
kubectl create namespace tailscale

# OAuth credentials を Secret として作成
kubectl create secret generic operator-oauth \
  --namespace tailscale \
  --from-literal=client_id=<your-client-id> \
  --from-literal=client_secret=<your-client-secret>
```

## Phase 3: Tailscale Operator のデプロイ

### 方法 A: ArgoCD を使用

```bash
# ArgoCD Application を適用
kubectl apply -f kubernetes/tailscale-operator/application.yaml
```

### 方法 B: Helm を直接使用

```bash
# Helm リポジトリを追加
helm repo add tailscale https://pkgs.tailscale.com/helmcharts
helm repo update

# Tailscale Operator をインストール
helm install tailscale-operator tailscale/tailscale-operator \
  --namespace tailscale \
  --create-namespace \
  --set oauth.clientId=<your-client-id> \
  --set oauth.clientSecret=<your-client-secret>
```

## Phase 4: 動作確認

```bash
# Operator の Pod を確認
kubectl get pods -n tailscale

# Operator のログを確認
kubectl logs -n tailscale -l app.kubernetes.io/name=tailscale-operator
```

## 使用方法

### LoadBalancer Service を Tailnet に公開

Service の `spec.loadBalancerClass` を `tailscale` に設定すると、
そのサービスが Tailnet 内に公開されます。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
spec:
  type: LoadBalancer
  loadBalancerClass: tailscale  # これを追加
  ports:
    - port: 80
      targetPort: 8080
```

### ArgoCD の例

`kubernetes/argocd/values.yaml` で設定済み:

```yaml
server:
  service:
    type: LoadBalancer
    loadBalancerClass: tailscale
```

これにより、ArgoCD Server が Tailnet 内の `argocd-server` としてアクセス可能になります。

## Tailnet でのアクセス

```bash
# Tailscale が作成したホスト名を確認
kubectl get svc -n argocd argocd-server -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

# または Tailscale Admin Console で確認
# https://login.tailscale.com/admin/machines
```

ArgoCD には `http://<tailscale-hostname>` でアクセスできます。

## トラブルシューティング

### Operator が起動しない

```bash
# Secret が正しく作成されているか確認
kubectl get secret -n tailscale operator-oauth

# Operator のログを確認
kubectl logs -n tailscale -l app.kubernetes.io/name=tailscale-operator
```

### Service に IP が割り当てられない

```bash
# Service の状態を確認
kubectl describe svc -n <namespace> <service-name>

# Tailscale Operator のログでエラーを確認
kubectl logs -n tailscale -l app.kubernetes.io/name=tailscale-operator
```

## 参考リンク

- [Tailscale Kubernetes Operator ドキュメント](https://tailscale.com/kb/1236/kubernetes-operator)
- [Tailscale Helm Charts](https://github.com/tailscale/tailscale/tree/main/cmd/k8s-operator)
