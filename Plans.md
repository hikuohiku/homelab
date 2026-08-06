# GitHub Copilot 日本語レビュー設定 Plans.md

作成日: 2026-05-21

関連: #42 (GitHub Copilot PR レビューを日本語で投稿するよう設定)

---

## 概要

GitHub Copilot の PR レビューコメントが英語で投稿されるため、日本語で投稿するようカスタム指示を追加する。

---

## Phase 1: Copilot カスタム指示

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | `.github/instructions/copilot.instructions.md` を作成: 日本語レビュー指示 | ファイルが存在し、日本語でレビューする指示が含まれる | - | cc:完了 |
| 1.2 | PR 作成・マージ | PR がマージされている | 1.1 | cc:完了 [PR #43] |

---

# Proxmox 棚卸し / pbs 管理方針 Plans.md

作成日: 2026-06-21

## 概要

Proxmox 上の停止中 VM/LXC を棚卸しで削除し、Terraform 管理外でドリフトしていた
pbs (qemu/112) の扱いを決定する。

## Phase 1: 棚卸し・pbs 管理方針

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| A | 停止中 VM/LXC の棚卸し・削除（9台） | 現存は vaultwarden(100)/syncthing(101)/docker(106)/pbs(112)/node01(113) のみ | - | 完了 [PR #45] |
| B | pbs を手動管理（Terraform 管理対象外）として明示 | pbs.tf.ignore に注記追記 / README・CLAUDE.md に明記 / Proxmox の `terraform` タグ削除 | A | 完了 [PR #45] |
| C | IaC 管理外の空 namespace 削除（ente / nextcloud / vaultwarden） | 3 namespace を削除（nextcloud は 50Gi データ含め破棄合意済み）/ repo の nextcloud 定義（kustomization 行・apps/nextcloud/・dex OIDC client）を整理 | - | 完了 [PR #46] |

---

# LXC → k8s 移行 Plans.md

作成日: 2026-06-21

## 概要

Proxmox LXC で手動運用中のサービスを k8s (ArgoCD) へ移行する。manifest は helm chart を使わずゼロスクラッチで手書きする方針。immich が先行移行済み。

## Phase 1: 移行バックログ

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| M0 | docker VM(106) 削除（未使用） | qemu/106 削除 / Tailscale `hikuo-homedocker` 削除 | - | 完了（qemu/106 削除済み・検証済み。Tailscale デバイス削除はユーザー手動） |
| M1 | vaultwarden を k8s 移行（手書き manifest） | apps/vaultwarden/ 作成・登録 / Doppler `VAULTWARDEN_ADMIN_TOKEN` 登録 / 旧 LXC tailscale 退避 / /data 移行 / 検証 | - | 完了 [PR #47]（ciphers 781 件移行・ログイン確認済み・旧 LXC 100 破棄済み） |
| M2 | syncthing を k8s 移行 | manifest 作成（T-0138, PR #328）・tailnet 公開（T-0139）完了。旧 LXC 101 からの実データ移行（T-0140）待ち | - | 実装進行中 |

> 補足: k8s への書き込み操作（移行時の scale/cp/exec 等）は kubectl CLI で行う方針（CLAUDE.md 反映済み）。
> 当初は `.claude/settings.json` の `permissions.ask` で毎回人間の承認を求めていたが、autopilot の
> ヘッドレス実行では誰も承認できず起動が丸ごと無駄になるため、`ask` ルールは全廃した（`ops/CHARTER.md`
> §5.1）。現在の歯止めは「変更は Git → CI → ArgoCD を通す」という経路そのもの。

> M2 の技術的根拠（T-0137, 2026-08-06 調査）: P2P 特性で懸念していた 2 点はいずれも解消できる。
> (1) device ID/証明書（`cert.pem`/`key.pem`）は syncthing の config ディレクトリに保存され、
> これが失われると別デバイスとして再認識される（Syncthing 公式ドキュメント）。永続化には PVC が要るが、
> node01 は単一ノード構成（`kubectl get nodes` 実測）のため `local-path`（node ローカル）PVC の
> ノード固定制約は実質問題にならない。(2) sync プロトコル（TCP 22000, QUIC 対応）は HTTP ではないため、
> immich/vaultwarden/coder 等が使っている Tailscale operator の L7 Ingress（`ingressClassName: tailscale`）
> では転送できない。Tailscale Kubernetes operator は Service に `tailscale.com/expose: "true"`
> （または `type: LoadBalancer` + `loadBalancerClass: tailscale`）を付ける L3 ingress を提供しており、
> これは iptables/nftables の DNAT で Service の全ポート・全プロトコル（TCP/UDP）を転送する
> （Tailscale 公式ドキュメント kubernetes-operator/cluster-egress 系）。この repo では今のところどのアプリも
> L7 Ingress のみで、L3 Service 公開は前例が無い。global discovery/relay はいずれも outbound 接続のみで
> 動作するため、この cluster の既存 egress で足りる。local discovery（UDP 21027, マルチキャスト）は
> Pod のネットワーク namespace 内では機能しないが、既存の LXC 版もピア側は Tailscale 経由で到達している
> 前提のため実質的な後退ではない（ここは実機の現行構成確認までは至っていない）。issue #31/#38 に結論を返信済み。
>
> T-0139（2026-08-06）で実装: sync/discovery ポート（TCP/UDP 22000, UDP 21027）は
> `apps/syncthing/service-tailnet.yaml`（`type: LoadBalancer` + `loadBalancerClass: tailscale`、
> `tailscale.com/hostname: syncthing-sync`）で公開。GUI(8384) は他アプリ（immich/vaultwarden）と
> 同じ L7 Ingress パターン（`apps/syncthing/ingress.yaml`）で `syncthing` として公開する判断とした
> （人間が sync フォルダ・デバイス承認を GUI から操作する必要があるため）。実機での到達性確認は
> このクラウド/クラスタ内サンドボックスからは検証できず未確認（`kustomize build` による構文検証のみ）。
> 残課題: LXC 101 の `/var/lib/syncthing`（想定パス、要確認）から新 PVC への config/データ移行手順
> （cert.pem/key.pem を含めて移すことで既存ピアとの再認証を避ける、T-0140 で対応）。
