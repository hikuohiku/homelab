preview 中のアプリを HEAD に戻し、ArgoCD の auto-sync を復元する。

## 引数

- `$ARGUMENTS` — `<app>` 形式（例: `immich`）。省略時は `just preview-status` で状態を表示

## 手順

1. 引数がない場合: `just preview-status` を実行して終了
2. 引数がある場合: `just preview-reset <app>` を実行
3. ArgoCD MCP (`mcp__argocd__get_application`) で対象アプリの targetRevision が HEAD に戻ったことを確認
4. ルート `apps` Application の syncPolicy に automated が復元されていることを確認
