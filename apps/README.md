# ArgoCD App of Apps セットアップ

このディレクトリは App of Apps パターンで Kubernetes アプリケーションを管理します。

## 前提条件

- k3s が稼働中（Traefik 無効化済み）
- kubectl と helm がインストール済み
- KUBECONFIG が設定済み

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

## Phase 2: ArgoCD を Helm で初回インストール

**ローカル（Mac）で実行:**

```bash
# homelab リポジトリのディレクトリに移動
cd ~/ghq/github.com/hikuohiku/homelab/apps/argocd

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

```bash
# LoadBalancer の IP を確認
kubectl get svc -n argocd argocd-server

# ブラウザで http://<EXTERNAL-IP> にアクセス
# ユーザー名: admin
# パスワード: (Phase 3 で取得したパスワード)
```

## Phase 5: App of Apps パターンで自己管理へ移行

### 1. App of Apps を適用

**ローカル（Mac）で実行:**

```bash
# Apps Application を適用（これが apps/ ディレクトリ内の全ての Application を管理）
kubectl apply -f apps/apps.yaml

# Application の状態を確認
kubectl get application -n argocd

# apps Application が自動的に argocd などの子 Application をデプロイします
```

### 2. 動作確認

**ローカル（Mac）で実行:**

```bash
# Application の詳細を確認
kubectl get application -n argocd argocd -o yaml

# Helm リリースを確認
helm list -n argocd

# ArgoCD UI で確認
# - Applications タブに "argocd" Application が表示される
# - Status が "Synced" と "Healthy" になっていることを確認
```

**期待される出力:**
```
NAME     NAMESPACE  STATUS  HEALTH   SYNCPOLICY
apps     argocd     Synced  Healthy  Auto
argocd   argocd     Synced  Healthy  Auto
```

## App of Apps パターンの利点

1. **完全な IaC**: すべての Application リソースが Git で管理される
2. **スケーラビリティ**: 新しいアプリを `apps/<app-name>/application.yaml` として追加し、`kustomization.yaml` に追記するだけ
3. **自己管理**: ArgoCD 自身も Git リポジトリから管理される
4. **複数 sources**: Helm chart と values.yaml を別リポジトリから参照可能

## 新しいアプリケーションの追加方法

```bash
# 1. 新しいアプリ用のディレクトリを作成
mkdir -p apps/my-app

# 2. Application リソースを作成
cat > apps/my-app/application.yaml <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: my-app
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/hikuohiku/homelab.git
    targetRevision: HEAD
    path: apps/my-app
  destination:
    server: https://kubernetes.default.svc
    namespace: my-app
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
EOF

# 3. kustomization.yaml に追加
# apps/kustomization.yaml の resources セクションに追記:
# - my-app/application.yaml

# 4. Git にコミット & プッシュ
git add apps/
git commit -m "Add my-app application"
git push origin main

# ArgoCD が自動的に新しい Application を検出して適用します
```

これで ArgoCD が Git リポジトリから完全に自己管理されるようになります！
