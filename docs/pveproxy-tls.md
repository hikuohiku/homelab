# pveproxy TLS 解消台本 — tailscale cert と custom 証明書アップロード (2026-08-22)

P-0103 の産出物。backlog T-0107 / feedback F-20260806T134136Z-c5205413264 (issue #56) の
実測に基づく。**この台本の実行 (証明書の差し替え) は別途予告窓で人間が行う。**
ここには手順と確認方法だけを置く。

## 現状と真因 (なぜ terraform apply が止まっているか)

- pveproxy の証明書は PVE クラスタ CA の自己署名で、その CA を信頼するクライアントが無い。
  bpg provider からの接続は `signed by unknown authority` で失敗する。**SAN 不一致が
  真因ではない** (SAN を直しても発行者が信頼されない限り解消しない)
- provider はこの TLS 失敗を warning に握り潰し、datastore に実在する
  `nixos-proxmox-cloud-v0.2.5.qcow2` を「存在しない」と誤判定して
  `proxmox_download_file.nixos_image` に `1 to add` を出す。
  `terraform/proxmox/vm-nixos.tf` はこれを `replace_triggered_by` に持つため、
  誤った判断での apply は node01 再作成に繋がりうる
- 以上より 2026-08-03 以降、terraform apply は禁止中

## 解法の形

正解は tailscale cert — Let's Encrypt 由来 (公開 CA / SAN 一致 / tailnet 内なら自動信頼)。
発行コマンドだけが Proxmox ホスト OS 上での実行を要求する (人間専有はこれのみ)。
生成物を pveproxy へ反映する API `POST /nodes/{node}/certificates/custom` は
`PROXMOX_API_TOKEN` から実行できる (`/nodes` に対する `Sys.Modify` を実測済み)。

> 補正: T-0107 / F-20260806 の記録では動詞が PUT と書かれていたが、PVE API 定義上は
> **POST** である (pve-docs api-viewer 参照)。この台本では POST を使う。

## 手順

### 0. 適用前の状態を記録する

リポジトリルートから:

```bash
ops/tools/check_pve_tls.sh; echo "exit=$?"
```

現在は `FAIL ... TLS 証明書を検証できない` と表示され **exit 1** になるのが正。
(終了コードの一覧は `ops/tools/check_pve_tls.sh -h`。1 は「TLS が壊れている」、
2 は「接続できない等で判定不能」。)

### 1. 人間がホスト上で証明書を発行する (1 コマンド)

Proxmox ホストの OS シェル (物理/管理コンソールまたはホストへの SSH) で:

```bash
cd /root && tailscale cert hikuo-homeserver.tailae6c2.ts.net
```

- カレントディレクトリに `hikuo-homeserver.tailae6c2.ts.net.crt` と同名 `.key` (PEM) が
  落ちる。`--cert-file` / `--key-file` での明示指定も可能
- 前提: ホスト上の tailscaled が稼働中であること、tailnet の MagicDNS + HTTPS が有効なこと
- 有効期間は 90 日 (Let's Encrypt)。更新は下記「更新」節

### 2. 生成物を pveproxy へ反映する (API アップロード)

作業 PC 等から。トークンは Doppler homelab/prd の `PROXMOX_API_TOKEN`
(書式 `root@pam!<tokenid>=<secret>`。terraform が使っているものと同一):

```bash
export PROXMOX_API_TOKEN='root@pam!terraform=...'   # Doppler homelab/prd の値
FQDN=hikuo-homeserver.tailae6c2.ts.net
NODE=<ノード名>   # 不明なら次のコマンドで一覧:
                  # curl -fsS -H "Authorization: PVEAPIToken=${PROXMOX_API_TOKEN}" \
                  #        https://${FQDN}:8006/api2/json/nodes | python3 -m json.tool

curl -fSs -X POST "https://${FQDN}:8006/api2/json/nodes/${NODE}/certificates/custom" \
  -H "Authorization: PVEAPIToken=${PROXMOX_API_TOKEN}" \
  --data-urlencode "certificates@${FQDN}.crt" \
  --data-urlencode "key@${FQDN}.key" \
  --data-urlencode "force=1" \
  --data-urlencode "restart=1"
```

- `--data-urlencode name@file` が PEM 全文を URL エンードして載せる
  (このエンドポイントは JSON ボディや multipart を受け付けない)
- `restart=1` で pveproxy が即時再起動し、新しい証明書で応答する
  (Web UI / API が数秒断ける。だから予告窓)
- 同等の手作業としてホスト上の `pvenode cert set <crt> <key> -force --restart` もあるが、
  将来の自動更新は API 経路 (上記 curl) を前提にする

### 3. 適用後の確認

1. `ops/tools/check_pve_tls.sh` → **exit 0** になり subject / issuer / notAfter が表示される
2. ブラウザで `https://hikuo-homeserver.tailae6c2.ts.net:8006` が警告なしで開く
3. `terraform plan` から pveproxy TLS 由来の warning が消え、
   `proxmox_download_file.nixos_image` の diff (`1 to add`) も消える
   (ファイルは実在するので、TLS が通れば存在確認に成功する)

## apply 禁止解除の条件 (warning 消失条件)

次の **3 つすべて**が揃ったときに限り、apply 禁止の解除判断をしてよい:

- `ops/tools/check_pve_tls.sh` が exit 0
- `terraform plan` に pveproxy TLS 由来の warning が出ない
- 同じ plan で `proxmox_download_file.nixos_image` の diff が消えている

解除の判断・実行そのものは本プロジェクト (P-0103) の範囲外。この台本は判断材料を
機械的に揃えるところまでが責務。

## 更新

tailscale cert の有効期限は 90 日。ホスト側での自動更新 (systemd timer 等) は未整備であり
本プロジェクトの範囲外。期限切れ前に「手順 1〜3」を再実行すること。
`check_pve_tls.sh` が notAfter を表示するので、期限監視の出発点になる。
