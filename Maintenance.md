# Maintenance

`/weekly-maintenance` の実行記録。**新しいエントリを先頭に追加する。**

手順そのものは `.claude/commands/weekly-maintenance.md` にある。
各回の振り返りで手順を更新していくため、記録と手順は対で運用する。

---

## 2026-08-03 — 初回（障害対応として実施）

定期実行ではなく、障害をきっかけに実施したもの。このルーティンを作る契機になった回。

### 点検結果

| 項目 | 結果 |
|------|------|
| vaultwarden バージョン | **1.36.0（2026-05-03）で据え置き。上流最新は 1.37.1（2026-07-29）** |
| セキュリティ | 1.37.0 に Medium 8 件の修正。うち 3 件が本構成に該当 |
| ArgoCD Sync/Health | Synced / Healthy |
| Pod | Running |
| ノードディスク | **空き 4.23 GB / 20 GB（逼迫）** |

### 障害の内容

Web vault は正常だが、モバイル / ブラウザ拡張 / デスクトップの各クライアントで
vault のレコードが表示されない。

原因は vaultwarden 1.37.0 のリリースノートに明記されていた:

> This update is required for support with clients with version 2026.7.0+

クライアントは自動更新で 2026.7 系に到達していたが、サーバーは 1.36.0 のまま据え置かれて
いたため API が不整合になった。web vault はサーバー同梱（1.36.0 は v2026.4.1 を同梱）で
バージョンが揃っているため影響を受けず、外部クライアントのみ症状が出た。

サーバーログ上も、クライアントは `POST /identity/connect/token => 200` と
`GET /api/accounts/revision-date => 200` までは到達しているが `/api/sync` が一度も
現れておらず、認証は通り同期段階で失敗していた。

### 対応

- **PR #49** — `vaultwarden/server` を 1.36.0-alpine → 1.37.1-alpine に更新
- マージ後の検証: ArgoCD が `fe31010` に追随、Pod が新世代に入れ替わり、
  起動ログで Version 1.37.1 を確認。クライアント側でもレコード表示を確認
- 同期成立の裏付けとして、修正後のログに vault 登録ホストへの icon 取得が多数出現
  （修正前のログには icon 取得が一切なかった）

### 同時に取り込んだセキュリティ修正

本構成（単一ユーザー・組織なし・SQLite・SSO 未使用）に**該当するもの**:

| 脆弱性 | CVSS |
|--------|------|
| SSRF via the icon endpoint | 5.8 / 6.3 |
| Unauthenticated WebSocket Flooding DDOS | 5.3 |
| Send Access-Count Bypass | 5.3 |

**該当しないもの**（すべて組織機能の脆弱性。単一ユーザー・組織なしのため実質無害。
次回以降この調査を繰り返さないために記録する）:
Cross-Organization Cipher Access / Organization Policy Bypass on Directory Import /
Cross-Organization Secret Sharing / Organization Import Authorization /
Organization Data Enumeration via the Manager role

### 持ち越し

- **ノードのディスク空きが 4.23 GB / 20 GB**。3 GB を切る前に手を打つ必要がある。
  syncthing の k8s 移行（Plans.md M2）もこの制約で実行不可能な状態
- **`ADMIN_TOKEN` が平文**。起動ログに `You are using a plain text ADMIN_TOKEN which
  is insecure` の警告。Argon2 PHC 文字列への差し替えを検討（今回は見送り）
- **未認証リクエストのレート制限が 1.37.0 で追加された**。本構成は全クライアントの
  トラフィックが Tailscale Ingress のプロキシ Pod 経由で届き、送信元 IP が一律同一
  （ログ上 `10.42.0.43`）。クライアントを増やした際に 429 が出ないか要確認
- **icon 取得のタイムアウトが多発**しログが賑やかになっている。実害はないが、
  気になるなら `DISABLE_ICON_DOWNLOAD` で無効化できる

### 振り返り

初回のため手順そのものを新規作成した（PR #50）。この回で判明した落とし穴を
手順書の「注意事項」に反映済み:

- 上流の「最新」を鵜呑みにしない（alpine variant の事情で 1.37.0 ではなく 1.37.1）
- リリースノートは要約ツールを通さず原文を読む（WebFetch がセキュリティ修正 8 件の
  内訳を圧縮してしまい該当有無を判断できなかった）
- 該当しない脆弱性も「確認した上で該当しない」と記録に残す

次回への申し送り: 手順が実際に回るかの検証はこれから。対象は vaultwarden のみに
絞っており、拡張は振り返りで「変更なし」が 3 回続いてから。
