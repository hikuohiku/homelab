# P-0103 — VM の新築を 3 週間凍らせている鍵穴に合鍵の型を取る — pveproxy TLS 解消を「人間 1 コマンド + 検証器 + API アップロード台本」まで削り込む

## 目的

T-0107 (pveproxy 証明書 SAN 不一致) 以降 terraform apply は禁止のまま (2026-08-03〜) で、
VM プロビジョニング層全体が凍結している。解法自体は F-20260806 の人間実測で特定済み —
`PROXMOX_API_TOKEN` は `Sys.Modify` を持ち、差し替えは `PUT /nodes/{node}/certificates/custom`
で可能、正解は tailscale cert (公開 CA / SAN 一致 / 更新自動)、人間の作業はホスト上の
1 コマンドのみ。欠けているのは判断ではなく道具なので、精密な台本と、現状 fail・解消後に
反転する検証器を作る。VISION「判断を人間に投げない、決めるための情報を集めるプロジェクトを
立てる」の直接の適用先。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing** (2026-08-22、`project/p-0103`
の checkout、リポジトリルートから実行)。

- [ ] `test -x ops/tools/check_pve_tls.sh`
  — 検証器スクリプトが存在し実行可能であること。実測 rc=1 (`ops/tools/` ディレクトリごと未存在)。
- [ ] `grep -q 'tailscale cert' docs/pveproxy-tls.md && grep -q 'certificates/custom' docs/pveproxy-tls.md`
  — 台本に人間 1 コマンド (`tailscale cert`) と API アップロード経路 (`certificates/custom`)
    の両方が書いてあること。実測 rc=2 (`docs/pveproxy-tls.md` 未存在)。
- [ ] `python3 -m unittest ops.tests.test_pve_tls_docs`
  — 台本と検証器の約束 (特に「unknown authority を exit 1 で報告する」挙動) がテストで
    固定されていること。実測 FAILED (errors=1、モジュール未存在)。

## 設計方針

### 前提 (initializer が 2026-08-22 に読んだ事実)

- backlog T-0107 (構築セッション実測): 真因は SAN 不一致ではなく「発行者である PVE クラスタ
  CA を誰も信頼していない」(`signed by unknown authority`)。SAN への IP 追加だけでは直らない
- F-20260806T134136Z-c5205413264: 証明書差し替えは API から可能 (`Sys.Modify` 実測済み)。
  残る人間専有は `tailscale cert` が Proxmox ホスト OS 上での実行を要求することのみ
  (物理/管理コンソール操作相当)
- bpg provider は TLS 失敗を warning に握り潰し「ファイルが存在しない」と誤判定していた
  (T-0107/T-0074)。だから検証器が「TLS がまだ壊れている」と機械的に言えることが価値になる
- `ops/tools/` は新規ディレクトリ (ops/ 配下に .sh は現状無い)。docs/ には調査記録の前例あり
  (docs/terraform-plan-in-ci.md 等)
- テストは unittest。**pytest は Job イメージに無い** (substrate.md / P-0091)
- スクリプト実行環境にあるのは git, python3 (+py3-yaml), curl, bash 等。openssl 単体コマンドや
  terraform は無い前提で設計する (substrate.md「イメージに入っているもの」

### 方針

1. **検証器** `ops/tools/check_pve_tls.sh`: 対象ホスト:8006 の TLS を検証し、現在
   (unknown authority) は exit 1、解消後は exit 0 で反転する。検証は環境にある道具
   (curl / python3 ssl) で行う
2. **挙動の固定** `ops/tests/test_pve_tls_docs.py`: unittest で、スクリプトの存在・実行可能
   ビット・未知 CA 相手に exit 1 になることを固定する。実ホストに依存せずテスト内で
   ローカル TLS サーバ (自己署名) を立てて検査する
3. **台本** `docs/pveproxy-tls.md`: (1) 人間がホストで打つ `tailscale cert` の正確なコマンド例
   (2) 生成物を `PUT /nodes/{node}/certificates/custom` へアップロードする curl 例
   (**実行は別途予告窓で**。台本の用意までが本プロジェクト) (3) 適用前後の確認手順
   (check_pve_tls.sh の使い方を含む)
4. **warning 消失条件の明記**: 「証明書差し替え後、terraform plan から TLS warning が消え
   `proxmox_download_file.nixos_image` の diff が確定すること」が apply 禁止解除の条件である旨を
   台本に書く。解除の判断・実行そのものは範囲外

## やらないこと

- **証明書の差し替え実行** (spec dod 明記)。台本を実行するのは別途予告窓で人間が行う
- **terraform apply の再開・plan の定期実行化**。apply 禁止解除の判断と実行は本プロジェクト外
- **check_pve_tls.sh の CronJob / CI への常駐組み込み**。verify が求めるのはスクリプトと
  テストまで。運用配線は別論点 (1 PR 1 論点)
- **tailscale cert 自動更新のホスト側設定作業** (systemd timer 等の実施)。台本に記す程度にとどめ、
  ホスト上での作業は行わない
