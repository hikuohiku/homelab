# P-0105 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### セッション 1 (2026-08-22) — initializer (PROJECT.md 作成)

- 受入 verify 3 本を実測し、**全項目 failing を確認** (rc=2 / unittest errors=1 / rc=2)
- 鍵→暗号→消費者の連鎖を実読で確定: `variables.tf` (age 秘密鍵変数) → `vm-nixos.tf`
  cloud-init (`/var/lib/sops-nix/key.txt`) → `configuration.nix` (`sops.age.keyFile`)
  → `secrets.yaml` 復号 → `k3s-manifests.nix` (`sops.templates`)。CI からの参照はゼロ。
  詳細は PROJECT.md「前提」
- エージェント環境に鍵の気配なし (env / `~/.config/sops` 共に無し、sops バイナリも無し)。
  dry-run の帰趨 (復号成功 or incident 報告) は worker がその時点の環境で改めて実測して判定すること
