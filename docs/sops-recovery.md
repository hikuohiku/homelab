# SOPS age 鍵の所在と復元手順

`nix/images/proxmox-cloud/secrets.yaml`（SOPS/age 暗号化）は node01 の cloud-init
シークレットの唯一の暗号化源。中身は `doppler-token` 1 キーで、sops-nix が node01 上で複号し、
k3s bootstrap manifest の Secret を生成する（`nix/images/proxmox-cloud/k3s-manifests.nix`）。
age 秘密鍵を失ったまま node01 を全損させると、repo と B2 バックアップからすべて戻しても
このファイルだけ開かない。依存の全体像（暗号化ファイル・recipient・消費者・鍵の所在・
実行環境での復号可否）は `python3 ops/tools/sops_dependency_map.py` が毎回再構築する
`ops/sops-dependency-map.json` を機械可読な正とし、この文書は人間用の読み取り版。

## 鍵の所在確認

秘密鍵の値は repo に存在しない（`.sops.yaml` が持つのは公開鍵 = recipient のみ）。実体は次の 2 箇所。

| # | 場所 | 性格 | 確認方法 |
|---|------|------|---------|
| 1 | Doppler プロジェクト `homelab` / config `prd` の `AGE_PRIVATE_KEY` | **一次情報源** | `doppler secrets --project homelab --config prd` で名前を確認し、必要になった時点だけ値を取得 |
| 2 | node01 の `/var/lib/sops-nix/key.txt` | プロビジョニング時に cloud-init が書く実体コピー（0600） | node01（静的 IP `192.168.0.129`, `terraform/proxmox/vm-nixos.tf` の locals）に SSH して `test -f /var/lib/sops-nix/key.txt && echo exists` |

流れは逆方向にも効く: `just plan` / `just apply` は `doppler run --project homelab --config prd
--name-transformer tf-var` 経由で Terraform 変数 `age_private_key`
（`terraform/proxmox/variables.tf`, sensitive）に鍵を渡し、cloud-init snippet
（`terraform/proxmox/vm-nixos.tf`）が `/var/lib/sops-nix/key.txt` へ注入する。
つまり node01 が生きているうちは 2 が実体、node01 全損時は 1 から復元する。
**1 と 2 が同時に消えたら鍵は失われる**（後述「鍵を失った場合」）。

手元の鍵候補が `.sops.yaml` の recipient と対になるかは公開鍵の導出で確認できる:

```
age-keygen -y keys.txt
# → age1u55u5prakalcplze25mvkr98ura4r4paduqx52xed0c8gh69j5psfp9tek なら一致
```

## 復元手順（復号 dry-run）

前提: `sops` と `age` コマンド（`nix shell nixpkgs#sops nixpkgs#age` 等で導入）、
「鍵の所在確認」で確保した秘密鍵ファイル。**平文はファイルへ残さない**
（復号出力は捨てて rc だけを見る。証跡に中身を貼らない）。

1. 鍵を確保する。「鍵の所在確認」の 1 → 取れなければ 2 の順。
2. **リポジトリルートで実行する。** sops はカレントディレクトリの `.sops.yaml` を読む
   （別の場所で実行するときは `SOPS_CONFIG` でパスを明示する）。
3. 鍵を環境変数で渡して復号し、出力は捨てる:

   ```
   SOPS_AGE_KEY_FILE=/path/to/keys.txt \
     sops -d nix/images/proxmox-cloud/secrets.yaml > /dev/null && echo OK
   ```

4. rc=0 なら鍵と暗号文の整合を確認できた。rc!=0 は鍵違いか recipient 不一致なので、
   手順 2 の前に行う `age-keygen -y` の対合検証に戻る。

node01 全損からの本復元は、この dry-run で鍵を確認できた上で `just apply` を再実行すればよい
（Terraform が cloud-init 経由で鍵を再注入し、NixOS 起動時に sops-nix が複号するまで待つ）。

## 鍵を失った場合

Doppler（上表 1）と node01（同 2）の両方から鍵が消えた場合、`secrets.yaml` は復号不能になる。
ただし被弾範囲は `doppler-token` 1 キーのみで、平文は Doppler 側で再発行できるトークンなので、
環境ごと作り直せる: 新しい age キーペアを生成して Doppler に保存し直し、`.sops.yaml` の
recipient を差し替え、`secrets.yaml` を新 recipient で再暗号化する（初回セットアップの手順は
[apps/README.md](../apps/README.md)）。逆に言えば、この 1 ファイルのためだけに鍵管理の
単一障害点が存在している。実行環境が今すぐ復号できるかは
`ops/sops-dependency-map.json` の `agent_environment.can_decrypt_now` でも観測できる。
