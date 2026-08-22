# P-0103 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## セッション記録

<!-- 1 セッション 1 ブロック。何をやったか / 分かったこと / 次への一言 を書く -->

### initializer (2026-08-22)

PROJECT.md を起票した。受入 3 項目は全て failing を実測済み (詳細は PROJECT.md)。
実装には着手していない。

### worker #1 (2026-08-22)

やったこと:

- 受入 3 項目すべてを実装し、verify を自分で回して全 green を実測した
  - `ops/tools/check_pve_tls.sh` (新規): bash + python3 ssl のみの検証器 (openssl バイナリ不要)。
    終了コード: 0=検証成功 / 1=TLS 検証失敗 (unknown authority ほか) / 2=接続不能で判定不能
  - `docs/pveproxy-tls.md` (新規): 台本。人間 1 コマンド (`tailscale cert`) の例、API アップロード
    curl 例 (`--data-urlencode certificates@file`)、適用前後の確認手順、apply 解禁条件 3 点
    (checker exit 0 / plan warning 消失 / proxmox_download_file diff 消滅) を明記
  - `ops/tests/test_pve_tls_docs.py` (新規): 6 テスト。SAN=127.0.0.1 一致の埋め込み自己署名
    証明書でローカル HTTPS サーバを立て、unknown authority→exit 1 / CA 信頼→exit 0 /
    接続不能→exit 2 の反転を実サーバ相手に固定
- `python3 -m unittest discover -s ops/tests -t .` (74 テスト) と `check_doc_commands.py` も
  green を確認

分かったこと / 罠:

- **PVE API の動詞は POST。spec / T-0107 の記録にある PUT は誤り** (pve-docs api-viewer 定義 +
  フォーラム実例)。JSON/multipart ボディは受け付けないので `--data-urlencode name@file` で
  PEM を載せる。台本に補正注記を書いた (レビュー時に spec との食い違いと見える点の説明)
- openssl バイナリは Job イメージにもこの開発環境にも無い。テスト fixture の証明書は
  node22 + npm selfsigned で事前生成して PEM 文字列として埋め込んだ。テスト自体は
  標準ライブラリのみで動く
- checker の既定 HOST は `hikuo-homeserver.tailae6c2.ts.net` をハードコートしている
  (tailnet 名が変わったら script + doc を同時更新すること)
- 実ホストへの smoke は tailnet 外の環境からは DNS 不解決で exit 2 — これは正しい挙動。
  in-cluster での受入実測では現状 exit 1 になるはず

次への一言:

- verify 全項目 green 済みなので、wrapper の実測確認のうえレビューへ。差し替えの実行
  (予告窓)・CronJob 常駐化・tailscale cert 自動更新 timer は PROJECT.md「やらないこと」のまま
