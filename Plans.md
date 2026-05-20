# Immich LXC → K8s 移行 Plans.md

作成日: 2026-05-20

関連: #38 (LXC → K8s サービス移行)

---

## 概要

Proxmox LXC コンテナ (vmid 111) で運用中の Immich を Kubernetes クラスタへ移行する。
既存ブランチ `claude/create-immich-manifest-FpCGe` の途中実装をベースに、
最新チャート (v0.12.0 / app v2.6.3) へ更新・品質改善した上でデプロイする。

### 設計方針

- **途中実装活用**: 新ブランチを main から作成し、途中実装の内容をレビュー・修正して取り込む
- **バージョン更新**: Helm chart v0.10.3 → v0.12.0, Immich v2.4.0 → v2.6.3
- **既存パターン準拠**: Nextcloud/Dex 等の既存アプリと同じ構成パターンに合わせる
- **データ安全**: LXC からのデータ移行は段階的に行い、旧環境は検証完了まで保持

### アーキテクチャ

```
apps/immich/
├── application.yaml              # ArgoCD Application
├── kustomization.yaml            # Kustomize + Helm chart v0.12.0
├── values.yaml                   # Helm values override
├── ingress.yaml                  # Tailscale Ingress
├── library-pvc.yaml              # 写真ライブラリ用 PVC (50Gi)
├── postgres.yaml                 # PostgreSQL + vectorchord (Deployment, PVC, Service)
└── postgres-external-secret.yaml # Doppler → DB credentials
```

---

## Phase 1: マニフェスト作成・更新

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | ブランチ作成: main から `feat/immich-k8s-migration` を作成 | ブランチが存在し、main と同期している | - | cc:完了 |
| 1.2 | `apps/immich/kustomization.yaml` 作成: Helm chart v0.12.0 への更新、リソース一覧の定義 | chart version が 0.12.0、リソース一覧が正確 | 1.1 | cc:完了 [1950fdf] |
| 1.3 | `apps/immich/values.yaml` 作成: v0.12.0 の構造に合わせた values（DB接続、Valkey、ML、永続化） | `helm template` 相当で有効な YAML が生成される構造 | 1.2 | cc:完了 [1950fdf, 7f495a6] |
| 1.4 | `apps/immich/postgres.yaml` 作成: PostgreSQL + vectorchord Deployment/PVC/Service | vectorchord 16 系イメージ、PVC 10Gi、liveness/readiness probe 設定済み | 1.2 | cc:完了 [1950fdf] |
| 1.5 | `apps/immich/postgres-external-secret.yaml` 作成: Doppler 連携の ExternalSecret | ClusterSecretStore `doppler` 参照、`IMMICH_DB_PASSWORD` キー | 1.2 | cc:完了 [1950fdf] |
| 1.6 | `apps/immich/library-pvc.yaml` 作成: 写真ライブラリ用 PVC | 50Gi, local-path, ReadWriteOnce | 1.2 | cc:完了 [1950fdf] |
| 1.7 | `apps/immich/ingress.yaml` 作成: Tailscale Ingress | ingressClassName: tailscale, host: immich | 1.2 | cc:完了 [1950fdf] |
| 1.8 | `apps/immich/application.yaml` 作成: ArgoCD Application | automated sync, CreateNamespace=true, namespace: immich | 1.2 | cc:完了 [1950fdf] |
| 1.9 | `apps/kustomization.yaml` 更新: immich/application.yaml を追加 | immich が App of Apps に含まれる | 1.8 | cc:完了 [1950fdf] |
| 1.10 | マニフェスト全体レビュー: セキュリティ・可読性・既存スタイルとの整合性を最終確認 | コメント過剰なし、namespace 一貫、secret 参照正確 | 1.2-1.9 | cc:完了 |

## Phase 2: Doppler シークレット設定 (手動)

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 2.1 | Doppler に `IMMICH_DB_PASSWORD` を登録 (homelab/prd) | `doppler secrets get IMMICH_DB_PASSWORD --project homelab --config prd` で値が取得可能 | - | cc:完了 |

## Phase 3: デプロイ・新規動作確認

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 3.1 | ブランチを push し、ArgoCD で Immich アプリが Synced/Healthy になることを確認 | ArgoCD で immich が Synced かつ全 Pod が Running | Phase 1, 2.1 | cc:完了 |
| 3.2 | Tailscale 経由で Immich Web UI にアクセス可能か確認 (新規 DB で初期セットアップ画面) | `https://immich.<tailnet>` で初期セットアップ画面が表示される | 3.1 | cc:完了 |

## Phase 4: データ移行 (手動)

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 4.1 | K8s Immich の新規動作確認後、LXC の Immich を停止し PostgreSQL ダンプを取得 | `pg_dump` で完全なダンプファイルが生成される。LXC 停止前に K8s 側の正常動作を確認済み | 3.2 | cc:完了 |
| 4.2 | K8s の PostgreSQL を初期化し直し、ダンプをリストア | `psql` でリストア完了、`immich-admin schema-check` がパスする | 4.1 | cc:完了 |
| 4.3 | LXC の写真ライブラリを K8s の PVC にコピー | ファイル数・サイズが一致、Immich UI で写真が表示される | 4.1 | cc:完了 |
| 4.4 | 移行後の動作検証: 写真閲覧・アップロード・ML処理が正常動作 | 既存写真の表示、新規アップロード、顔認識が動作する | 4.2, 4.3 | cc:完了 |

## Phase 5: PR・マージ

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 5.1 | PR 作成・レビュー・main マージ | PR がマージされ、ArgoCD が main から Immich をデプロイ | Phase 3 | cc:完了 [PR #39] |

---

## 備考

- **LXC 保持**: データ移行完了・検証完了まで LXC コンテナ (vmid 111) は停止状態で保持する
- **ロールバック**: 問題発生時は ArgoCD で Immich Application を削除し、LXC を再起動して復旧
- **途中実装からの変更点**:
  - Helm chart v0.10.3 → v0.12.0
  - Immich v2.4.0 → v2.6.3
  - Valkey 9.0-alpine → 9.1-alpine (digest pin)
  - values.yaml を v0.12.0 の構造に合わせて再構成
  - 冗長なコメントを削除し、既存アプリのスタイルに統一
