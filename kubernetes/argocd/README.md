# ArgoCD セットアップ

GitOps ツール ArgoCD のセットアップ手順

## 前提条件

- k3s が稼働中
- kubectl と helm がインストール済み（NixOS configuration で設定済み）
- KUBECONFIG 環境変数が設定済み（自動設定）
- Tailscale Operator がインストール済み（Tailnet 公開を使用する場合）
  - セットアップ手順: [kubernetes/tailscale-operator/README.md](../tailscale-operator/README.md)

## Phase 1: KUBECONFIG の取得

**ローカル（Mac）で実行:**

```bash
# node01 から KUBECONFIG を取得
scp root@192.168.0.129:/etc/rancher/k3s/k3s.yaml ~/.kube/node01-config

# サーバーアドレスを node01 の IP に変更
sed -i '' 's|https://127.0.0.1:6443|https://192.168.0.129:6443|g' ~/.kube/node01-config

# KUBECONFIG 環境変数を設定（チルダを展開）
export KUBECONFIG=$HOME/.kube/node01-config

# 接続確認
kubectl get nodes
```

## Phase 2: 初回インストール（Helm）

**ローカル（Mac）で実行:**

```bash
# homelab リポジトリのディレクトリに移動
cd ~/ghq/github.com/hikuohiku/homelab/kubernetes/argocd

# Helm リポジトリを追加
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

# ArgoCD をインストール（values.yaml を適用）
helm install argocd argo/argo-cd \
  --namespace argocd \
  --create-namespace \
  --version 9.1.6 \
  --values values.yaml

# インストール確認（Pod が起動するまで数分かかります）
kubectl get pods -n argocd
kubectl get svc -n argocd
```

## Phase 3: 初期パスワード取得

```bash
# admin パスワードを取得
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
echo
```

## Phase 4: ArgoCD UI アクセス

### Tailscale 経由（推奨）

Tailscale Operator が設定済みの場合、ArgoCD は Tailnet 内に公開されます。

```bash
# Tailscale が割り当てたホスト名を確認
kubectl get svc -n argocd argocd-server -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

# または Tailscale Admin Console で確認
# https://login.tailscale.com/admin/machines
```

ブラウザで `http://<tailscale-hostname>` にアクセス

### ローカルネットワーク経由（Tailscale 未設定時）

```bash
# LoadBalancer の IP を確認
kubectl get svc -n argocd argocd-server

# ブラウザで http://<EXTERNAL-IP> にアクセス
```

**ログイン情報:**
- ユーザー名: admin
- パスワード: (Phase 3 で取得したパスワード)

## Phase 5: 自己管理への移行

GitOps で ArgoCD 自身を管理するように設定します。

### 1. このリポジトリを GitHub にプッシュ

**ローカル（Mac）で実行:**

```bash
cd ~/ghq/github.com/hikuohiku/homelab
git add kubernetes/argocd/ nix/hosts/node01/
git commit -m "Add ArgoCD configuration and update NixOS config"
git push origin main
```

### 2. ArgoCD UI で Application を作成

1. **ArgoCD UI にアクセス**（Phase 4 で取得した LoadBalancer IP を使用）

2. **ログイン**
   - Username: `admin`
   - Password: Phase 3 で取得したパスワード

3. **新しい Application を作成**
   - "+ NEW APP" をクリック
   - 以下を入力：

   **GENERAL**
   - Application Name: `argocd`
   - Project Name: `default`
   - Sync Policy: `Automatic`
     - ☑ Prune Resources
     - ☑ Self Heal

   **SOURCE**
   - Repository URL: `https://github.com/hikuohiku/homelab`
   - Revision: `HEAD` または `main`
   - Path: `kubernetes/argocd`

   **DESTINATION**
   - Cluster URL: `https://kubernetes.default.svc`
   - Namespace: `argocd`

   **HELM**
   - Values Files: `values.yaml`

4. **CREATE** をクリック

5. **SYNC** をクリックして初回同期

### 3. 動作確認

**ローカル（Mac）で実行:**

```bash
# ArgoCD Application の状態を確認
kubectl get application -n argocd

# 詳細を確認
kubectl describe application argocd -n argocd

# Helm リリースを確認
helm list -n argocd
```

**期待される出力:**
```
NAME     NAMESPACE  STATUS  HEALTH   SYNCPOLICY
argocd   argocd     Synced  Healthy  Auto
```

これで ArgoCD が Git リポジトリから自己管理されるようになります！

### 今後の変更方法

1. `kubernetes/argocd/values.yaml` を編集
2. Git にコミット & プッシュ
3. ArgoCD が自動的に変更を検出して適用（Auto-Sync 有効時）

## トラブルシューティング

### Pod が起動しない

```bash
kubectl describe pod -n argocd <pod-name>
kubectl logs -n argocd <pod-name>
```

### パスワードを忘れた

```bash
# パスワードをリセット
kubectl -n argocd patch secret argocd-secret -p '{"stringData": {"admin.password": "'$(htpasswd -nbBC 10 "" <new-password> | tr -d ':\n' | sed 's/$2y/$2a/')'"}}'
```

## 参考リンク

- [ArgoCD 公式ドキュメント](https://argo-cd.readthedocs.io/)
- [ArgoCD Helm Chart](https://github.com/argoproj/argo-helm/tree/main/charts/argo-cd)
