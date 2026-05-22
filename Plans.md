# 開発サーバー（Coder on K8s）Plans.md

作成日: 2026-05-22

---

## 概要

VSCode Remote SSH で接続できる開発サーバーを K8s アプリとして追加する。
Coder（v2.33.2）で開発ワークスペースを管理し、ArgoCD でデプロイする。
小さく始めて段階的に拡張する方針。

### 構成

- **VM**: node01 スケールアップ（2c/8GB → 4c/12GB）
- **開発環境**: Coder（Helm chart, ArgoCD 管理）
- **ワークスペース**: Ubuntu 26.04 LTS
- **アクセス**: Tailscale 経由で VSCode Remote SSH
- **ホスト制約**: Intel N100 (4c) / 15.4 GB RAM

---

## Phase 1: インフラ準備

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | node01 のディスク実サイズ確認。Terraform 上 20GB だが PVC 合計 130GB あり実態を把握する | 実ディスクサイズと使用量が判明 | - | cc:TODO |
| 1.2 | Terraform で node01 スケールアップ: CPU 2→4, RAM 8→12GB, ディスク拡張（1.1 の結果次第） | `terraform plan` で差分が想定通り | 1.1 | cc:TODO |

---

## Phase 2: Coder デプロイ

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 2.1 | `apps/coder/` に Kustomize + Helm マニフェスト作成（最新 Helm chart） | kustomize build が成功 | - | cc:TODO |
| 2.2 | `apps/kustomization.yaml` に Coder Application 追加 | ArgoCD が coder アプリを認識 | 2.1 | cc:TODO |
| 2.3 | Tailscale Ingress 設定（既存 tailscale-operator 活用） | Tailscale 経由で Coder UI にアクセス可能 | 2.1 | cc:TODO |

---

## Phase 3: ワークスペース運用設計

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 3.1 | ワークスペーステンプレート作成: Ubuntu 26.04 LTS, PVC 永続化, リソース制限付き | Coder UI からワークスペース起動・再起動してもデータが残る | Phase 2 | cc:TODO |
| 3.2 | Git 認証設定: SSH キーまたは credential をワークスペースに注入 | ワークスペース内から git push できる | 3.1 | cc:TODO |
| 3.3 | ワークスペース自動停止設定: 未使用時にリソース解放 | 一定時間操作なしで Pod が停止し、再起動で復帰できる | 3.1 | cc:TODO |
| 3.4 | VSCode Remote SSH 接続確認 | ローカル VSCode から `coder ssh` で接続・開発できる | 3.1 | cc:TODO |

---

## 設計メモ

### ストレージ

- StorageClass: `local-path`（ノードローカル、ReclaimPolicy: Delete）
- ワークスペースの PVC は Delete ポリシー → **ワークスペース削除でデータ消失**
- 対策: コードは常に git push 運用。ワークスペースはいつでも再作成可能にする
- ディスク容量: node01 の実サイズ確認後にワークスペース PVC サイズを決定

### リソース制限

- Coder namespace に ResourceQuota 設定（メモリ上限）
- ワークスペーステンプレートで requests/limits を明示
- 既存ワークロード（ArgoCD, immich 等）を圧迫しないようにする

### バックアップ

- PBS で VM レベルのバックアップは既にある
- ワークスペース内のコードは git push が一次バックアップ
- dotfiles は GitHub リポジトリで管理（Coder の dotfiles 機能）
