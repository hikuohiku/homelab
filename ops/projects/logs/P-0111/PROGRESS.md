# P-0111 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### セッション 1 (2026-08-22) — initializer (PROJECT.md 作成)

- 受入 verify 2 本を実測し、**全項目 failing を確認**
  (#1 rc=1: root_cause.md 未存在 / #2 rc=1 AssertionError: coder=Degraded, immich=Degraded)
- latest.json 実測で **vaultwarden=Healthy** を確認。同型 ExternalSecret の 3 アプリのうち
 1 つだけ回復 — vaultwarden との差分比較が調査の出発点 (詳細は PROJECT.md「前提」)
- 診断対象の温床候補 `<app>-restic-backup-credentials` ExternalSecret と Doppler キー名
  (`B2_ACCOUNT_ID_APPEND_ONLY` / `B2_ACCOUNT_KEY_APPEND_ONLY`) を manifest 実読で確定
