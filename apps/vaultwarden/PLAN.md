# Vaultwarden Server 追加計画

## 概要

自己ホスト型パスワードマネージャー [Vaultwarden](https://github.com/dani-garcia/vaultwarden) を既存のhomelabインフラに追加する。
Bitwarden互換のサーバーで、公式Bitwardenクライアント（ブラウザ拡張、デスクトップ、モバイル）から利用可能。

## 使用するHelm Chart

- **Repository**: https://guerzon.github.io/vaultwarden
- **Chart名**: vaultwarden
- **バージョン**: 0.34.4 (最新)
- **参考**: [guerzon/vaultwarden GitHub](https://github.com/guerzon/vaultwarden)

## 作成するファイル構造

```
apps/vaultwarden/
├── application.yaml              # ArgoCD Application マニフェスト
├── kustomization.yaml            # Kustomize構成（Helm + リソース）
├── values.yaml                   # Helm chart values
├── ingress.yaml                  # Tailscale Ingress
└── vaultwarden-admin-secret.yaml # ExternalSecret（管理者トークン）
```

## 実装詳細

### 1. application.yaml

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: vaultwarden
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: https://github.com/hikuohiku/homelab.git
    targetRevision: HEAD
    path: apps/vaultwarden
  destination:
    server: https://kubernetes.default.svc
    namespace: vaultwarden
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

### 2. kustomization.yaml

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

helmCharts:
  - name: vaultwarden
    repo: https://guerzon.github.io/vaultwarden
    version: 0.34.4
    releaseName: vaultwarden
    namespace: vaultwarden
    valuesFile: values.yaml

resources:
  - ingress.yaml
  - vaultwarden-admin-secret.yaml
```

### 3. values.yaml

```yaml
# Domain設定（Tailscale経由）
domain: "https://vaultwarden.tailae6c2.ts.net"

# 管理者トークン（ExternalSecretから取得）
adminToken:
  existingSecret: vaultwarden-admin
  existingSecretKey: admin-token

# サインアップ設定
signupsAllowed: false           # 新規登録を無効化（招待制）
invitationsAllowed: true        # 組織管理者からの招待は許可

# データベース（SQLite - シンプル構成）
database:
  type: default

# 永続化ストレージ
storage:
  data:
    enabled: true
    size: 1Gi
    class: local-path
    accessMode: ReadWriteOnce

# Ingressは別途Tailscale Ingressを使用
ingress:
  enabled: false

# リソース制限
resources:
  limits:
    cpu: 300m
    memory: 256Mi
  requests:
    cpu: 50m
    memory: 64Mi

# ログ設定
logging:
  level: info

# WebSocket有効化（リアルタイム同期）
websocket:
  enabled: true

# SMTP設定（オプション - 将来的に有効化可能）
smtp:
  host: ""
  from: ""
```

### 4. ingress.yaml

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: vaultwarden
  namespace: vaultwarden
  annotations:
    tailscale.com/experimental-forward-cluster-traffic-via-ingress: "true"
spec:
  ingressClassName: tailscale
  defaultBackend:
    service:
      name: vaultwarden
      port:
        number: 80
  tls:
    - hosts:
        - vaultwarden
```

### 5. vaultwarden-admin-secret.yaml

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: vaultwarden-admin
  namespace: vaultwarden
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler
  target:
    name: vaultwarden-admin
    creationPolicy: Owner
  data:
    - secretKey: admin-token
      remoteRef:
        key: VAULTWARDEN_ADMIN_TOKEN
```

### 6. apps/kustomization.yaml への追加

```yaml
resources:
  - argocd/application.yaml
  - dex/application.yaml
  - external-secrets/application.yaml
  - tailscale-operator/application.yaml
  - vaultwarden/application.yaml  # 追加
```

## 事前準備（Doppler）

以下のシークレットをDopplerに追加する必要があります：

| キー | 説明 | 推奨値 |
|------|------|--------|
| `VAULTWARDEN_ADMIN_TOKEN` | 管理画面アクセス用トークン | Argon2ハッシュ化されたトークン推奨 |

**トークン生成例:**
```bash
# Argon2ハッシュ生成（推奨）
echo -n "your-secret-password" | argon2 "$(openssl rand -base64 32)" -e -id -k 65540 -t 3 -p 4

# または、プレーンテキストでも可（非推奨）
openssl rand -base64 48
```

## デプロイ後のアクセス

- **Web Vault**: https://vaultwarden.tailae6c2.ts.net
- **管理画面**: https://vaultwarden.tailae6c2.ts.net/admin
- **API**: https://vaultwarden.tailae6c2.ts.net/api

## 将来の拡張オプション

1. **SMTP設定**: メール通知・2FA回復用
2. **外部データベース**: PostgreSQL/MySQLへの移行（大規模運用時）
3. **バックアップ**: cronジョブによる定期バックアップ
4. **OIDC統合**: Dexとの連携（Vaultwarden Enterprise機能が必要）

## 実装手順

1. [ ] Dopplerに `VAULTWARDEN_ADMIN_TOKEN` を追加
2. [ ] `apps/vaultwarden/` ディレクトリを作成
3. [ ] 上記ファイルを作成
4. [ ] `apps/kustomization.yaml` を更新
5. [ ] コミット & プッシュ
6. [ ] ArgoCD同期を確認
7. [ ] Tailscale経由でアクセス確認
