フィーチャーブランチを ArgoCD でテストデプロイする。

## 引数

- `$ARGUMENTS` — `<app> <branch>` 形式（例: `immich feat/my-branch`）

## 手順

1. 引数をパースして app と branch を取得
2. `just preview <app> <branch>` を実行
3. ArgoCD MCP (`mcp__argocd__get_application`) で対象アプリの targetRevision がブランチに変更されたことを確認
4. アプリの Sync 状態と Health 状態を報告

## 完了後の案内

- `just preview-status` で現在の preview 状態を確認できること
- `/preview-reset <app>` でリセットできること
