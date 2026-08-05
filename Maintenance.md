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
- `ADMIN_TOKEN` が平文（起動ログに警告）。Argon2 PHC 化は今回見送り → **解消（2026-08-05）**:
  人間が Doppler の `VAULTWARDEN_ADMIN_TOKEN` を Argon2 PHC 文字列に差し替えた。
  `apps/vaultwarden/admin-token-external-secret.yaml` は既にこのキーを参照しており、vaultwarden 側も
  PHC 形式を自動判別するため manifest の変更は不要だった（`ops/backlog.json` T-0011, done）。
  反映後に `/admin` へログインできるかの確認は人間側の作業として残る
- 1.37.0 でレート制限が追加された。全クライアントが Tailscale プロキシ経由で同一
  送信元 IP に見えるため、429 が出ないか要確認 → **解消（2026-08-05）**: 懸念どおり、
  Tailscale Ingress はリバースプロキシとして自 Pod から接続するため raw peer IP は全クライアントで
  同一になる。ただし `X-Forwarded-For` には実クライアントの tailnet peer アドレスが入っているため、
  `apps/vaultwarden/deployment.yaml` に `IP_HEADER=X-Forwarded-For` を設定して読ませるようにした
  （`ops/backlog.json` T-0032）。実際に 429 が発生していたかどうかのログ確認はできていない
  （クラスタ到達不可）が、原因側の設定不備は解消した。あわせて `IP_HEADER_TRUSTED_PROXIES` を既定の
  `local`（private アドレス全体を信頼）からこの k3s クラスタの pod CIDR `10.42.0.0/16` に絞り、
  クラスタ内部の別 Pod による `X-Forwarded-For` 詐称の余地を減らした（T-0038）
- icon 取得のタイムアウトが多発。実害なし。`DISABLE_ICON_DOWNLOAD` で無効化可 → **見送り（2026-08-05）**:
  一度 `DISABLE_ICON_DOWNLOAD=true` を設定したが、「実害なしと記録済みの事象のために利用者に見える
  機能（クライアントのサイトアイコン表示）を犠牲にしている」という指摘（issue #56）を受けて revert した。
  ログノイズを消す価値より favicon 表示を残す価値のほうが大きいと判断（`ops/backlog.json` T-0031、
  `dropped`）。実害が出るようになったら改めて起票する

### 振り返り

初回のため手順を新規作成（#50）。判明した落とし穴は手順書に反映済み。
手順が実際に回るかの検証はこれから。
