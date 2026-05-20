# K8s デプロイ検証フロー改善 Plans.md

作成日: 2026-05-20

関連: #40 (K8s デプロイ検証フロー改善)

---

## 概要

フィーチャーブランチのマニフェストを ArgoCD でテストデプロイするための `just` コマンドを追加する。
併せて PVC データ保護と ArgoCD AutoSync 有効化を行う。

### 設計方針

- **シンプルさ優先**: just コマンドのみ。GitHub Actions や ApplicationSet は使わない
- **データ安全**: 重要 PVC に resource-policy: keep を付与し、prune からの誤削除を防止
- **AutoSync 有効化**: ルート Application の AutoSync を有効にし、GUI 手動操作を不要にする
- **スキル化**: Claude Code のプロジェクトスキルとして再利用可能にする

---

## Phase 1: PVC データ保護

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | `apps/immich/postgres.yaml` の PVC に `helm.sh/resource-policy: keep` アノテーション追加 | PVC 定義にアノテーションが存在する | - | cc:完了 |
| 1.2 | `apps/immich/library-pvc.yaml` に `helm.sh/resource-policy: keep` アノテーション追加 | PVC 定義にアノテーションが存在する | - | cc:完了 |

## Phase 2: preview コマンド

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 2.1 | `just preview <app> <branch>` レシピ追加: ルート automated sync 一時停止 + targetRevision patch | `just preview immich main` が正常終了する | - | cc:完了 |
| 2.2 | `just preview-reset <app>` レシピ追加: HEAD に戻し automated sync 復元 | `just preview-reset immich` が正常終了する | 2.1 | cc:完了 |
| 2.3 | `just preview-status` レシピ追加: HEAD 以外のアプリ一覧表示 | `just preview-status` が正常終了し一覧表示される | 2.1 | cc:完了 |
| 2.4 | `just preflight` 拡張: preview 状態の警告を末尾に追加 | `just preflight` 実行時に preview 中のアプリがあれば警告表示 | 2.3 | cc:完了 |
| 2.5 | `just argocd-bootstrap` レシピ追加: `kubectl apply -f apps/apps.yaml` | コマンドが正常終了する | - | cc:完了 |

## Phase 3: スキル・ドキュメント

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 3.1 | `.claude/commands/preview.md` 作成: preview ワークフロースキル | `/preview` で呼び出し可能 | Phase 2 | cc:完了 |
| 3.2 | `.claude/commands/preview-reset.md` 作成: reset スキル | `/preview-reset` で呼び出し可能 | Phase 2 | cc:完了 |
| 3.3 | `CLAUDE.md` 更新: preview ワークフローと argocd-bootstrap を記載 | Development Commands セクションに記載がある | Phase 2 | cc:完了 |

## Phase 4: PR・デプロイ

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 4.1 | PR 作成・レビュー・main マージ | PR がマージされている | Phase 1-3 | cc:TODO |
| 4.2 | `just argocd-bootstrap` 実行で AutoSync 有効化 (手動) | ArgoCD MCP で全アプリが Synced/Healthy かつ AutoSync enabled | 4.1 | cc:TODO |
| 4.3 | PVC アノテーションが適用されていることを ArgoCD MCP で確認 | immich-postgres-data, immich-library に resource-policy: keep が付与 | 4.2 | cc:TODO |
