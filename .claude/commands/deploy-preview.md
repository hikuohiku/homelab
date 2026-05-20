ArgoCD を使ったフィーチャーブランチのデプロイ検証フロー。

## 引数

- `$ARGUMENTS` — 操作とパラメータ（例: `start immich feat/my-branch`, `stop immich`, `status`）
- 引数なし: 現在の状態を表示してから次のアクションを提案

## ワークフロー

### 既存アプリの変更をテスト

1. `just preview <app> <branch>` でアプリの targetRevision をブランチに切り替え
2. ArgoCD MCP (`mcp__argocd__get_application`) で sync 状態と health を確認
3. テスト完了後、`just preview-reset <app>` で HEAD に戻す
4. ArgoCD MCP で復元を確認

### 新規アプリのデプロイテスト

新規アプリは main に存在しないため、ルート Application をブランチに向ける必要がある。

1. `just preview apps <branch>` でルートをブランチに切り替え（新 Application CR が作成される）
2. 新規アプリの targetRevision は HEAD のままなので、`just preview <new-app> <branch>` で切り替え
3. ArgoCD MCP で sync 状態と health を確認
4. テスト完了後、`just preview-reset <new-app>` → `just preview-reset apps` の順で復元

### 状態確認

- `just preview-status` で HEAD 以外を追跡中のアプリ一覧を表示
- `just preflight` にも preview 状態の警告が含まれる

## 注意事項

- preview 中はルート `apps` Application の auto-sync が無効になる（既存アプリの場合）
- テスト完了後は必ず `preview-reset` で復元すること
- `just argocd-bootstrap` はルート Application を main の状態で再適用する（AutoSync 有効化用）
