# vaultwarden 1.37.1-alpine → 1.37.2-alpine リリースノート読了の証跡

- 読了日: 2026-08-23 (P-0145 worker セッション)
- 現在版: 1.37.1-alpine (`apps/vaultwarden/deployment.yaml`)
- 目標版: 1.37.2-alpine
- 情報源: GitHub Releases API (`https://api.github.com/repos/dani-garcia/vaultwarden/releases/tags/1.37.2`, HTTP 200, 当日実測)
- リリース URL: https://github.com/dani-garcia/vaultwarden/releases/tag/1.37.2
- published_at: 2026-08-22T12:29:13Z / prerelease: false

現在版 (1.37.1, 2026-07-29) から目標版 (1.37.2) までの間にリリースは 1 個のみ
(ホップ数 1。`releases` 一覧で 1.37.2 の次が 1.37.1 であることを確認済み)。以下、原文引用。

## 原文 (release body 全文)

> ## Note
>
> This update is required for support with clients with version 2026.8.0+, please update before reporting any issues with them.
>
> ## What's Changed
> * Fix Debian cross-linking with xx-cargo by @alexliluz in https://github.com/dani-garcia/vaultwarden/pull/7524
> * Fix playwright test by @Timshel in https://github.com/dani-garcia/vaultwarden/pull/7548
> * Misc fixes and updates by @BlackDex in https://github.com/dani-garcia/vaultwarden/pull/7558
> * Include user email in successful login logs by @lmogthb in https://github.com/dani-garcia/vaultwarden/pull/7496
> * Fix sendmail executable permission check by @p-boenisch in https://github.com/dani-garcia/vaultwarden/pull/7483
> * add dummy revisionDate by @stefan0xC in https://github.com/dani-garcia/vaultwarden/pull/7608
>
> **Full Changelog**: https://github.com/dani-garcia/vaultwarden/compare/1.37.1...1.37.2

## 判定材料になった箇所

- **Note 冒頭「This update is required for support with clients with version 2026.8.0+」** —
  Bitwarden クライアント 2026.8.0+ のサポートには本更新が必須。#49 は「vaultwarden の放置で
  クライアント同期が全停止」した前科であり、クライアント互換が切れる方向の更新は滞留させない
  根拠になる。更新しない場合の失敗方向は「新しいクライアントでの同期不具合を報告されても対応外」。
- breaking change 宣言なし。変更内容はビルド系修正 (Debian cross-linking)、テスト整備
  (playwright)、ログ改善 (login log への email 追加)、SMTP sendmail の permission check 修正、
  revisionDate のダミー付与。この環境 (alpine イメージ・SQLite・Tailscale Ingress・SIGNUPS_ALLOWED=false)
  に対する挙動変化は「成功ログインに email が載るようになる」程度で、設定済み env
  (IP_HEADER / IP_HEADER_TRUSTED_PROXIES / ROCKET_PORT / DOMAIN 等) に触る変更は無い。
- **DB migration: 無し** — git partial clone で tag 間 diff を実測:
  `git diff --name-status 1.37.1 1.37.2 -- migrations/` = 空 (0 ファイル)。
  SQLite は起動時 migration のため、schema 不変なら rollback も自明。
- **alpine タグの実在** — Docker Hub tags API
  (`https://hub.docker.com/v2/repositories/vaultwarden/server/tags/1.37.2-alpine`, HTTP 200):
  last_pushed 2026-08-22T12:18:50Z、amd64 digest
  `sha256:2e635df29ecd942515f79d26932b879a723764981376572cac2cc2bb5e7604bc`。
  inventory note が名指しする「1.37.0 の alpine ビルド破損」型の事故 (タグだけあってイメージが
  動かない) を避けるため、イメージの push 実績まで確認した。1.37.0 の破損原因は OpenSSL ビルド
  不具合で、1.37.1 で修正済み (deployment.yaml の既存コメントどおり)。
