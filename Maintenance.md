# Maintenance

`/weekly-maintenance` の実行記録。新しいエントリを先頭に追加する。
手順は `.claude/commands/weekly-maintenance.md`。

---

## 2026-08-04 — node01 ディスクをオンライン拡張

Coder 導入に伴い、node01 (qemu/113) の `virtio0` を 50 GiB から 256 GiB へ拡張した。
Terraform は in-place update のみで適用され、`/dev/vda3`（ext4）もオンライン拡張済み。
再起動なしで完了し、root filesystem は 252 GiB（空き 223 GiB）、全 Pod は Ready。
再実行可能な手順は [node01 storage](docs/node01-storage.md) を参照。

---

## 2026-08-03 — 初回（障害対応）

vaultwarden 1.36.0 の据え置きにより、web 以外の全クライアントが同期不能になった。
上流 1.37.0 が「クライアント 2026.7.0+ に必須」。1.37.1 へ更新（#49）。
マージ後、Version 1.37.1 での稼働とクライアント表示を確認。

同時にセキュリティ修正 8 件を取り込んだ。本構成に該当するのは SSRF (icon endpoint)、
未認証 WebSocket flooding、Send access-count bypass の 3 件。**残り 5 件は組織機能の
脆弱性で、単一ユーザー・組織なしのため該当しない**（再調査を防ぐため記録）。

### 持ち越し

- ノードのディスク空き 4.23GB / 20GB。3GB を切る前に対処。Plans.md M2 もこれで停止中
  → **解消（2026-08-04）**: node01 を 256 GiB へ拡張（上記エントリ参照）。M2 の保留理由は
    P2P 特性の移行可否検討のみに変更（Plans.md 反映済み）
- `ADMIN_TOKEN` が平文（起動ログに警告）。Argon2 PHC 化は今回見送り → `ops/backlog.json` T-0011（needs-human、Doppler への登録待ち）
- 1.37.0 でレート制限が追加された。全クライアントが Tailscale プロキシ経由で同一
  送信元 IP に見えるため、429 が出ないか要確認 → `ops/backlog.json` T-0032
- icon 取得のタイムアウトが多発。実害なし。`DISABLE_ICON_DOWNLOAD` で無効化可 → `ops/backlog.json` T-0031

### 振り返り

初回のため手順を新規作成（#50）。判明した落とし穴は手順書に反映済み。
手順が実際に回るかの検証はこれから。
