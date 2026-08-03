homelab の週次メンテナンス。バージョン据え置きとクラスタの異常を検知し、更新 PR を作成する。

## 背景

本リポジトリはイメージタグと helm chart のバージョンを明示的に pin している。
pin は再現性のために必要だが、**誰も上げなければ永久に据え置かれる**という副作用がある。

実際に 2026-08-03、vaultwarden が 1.36.0 のまま放置され、自動更新されたクライアント
(2026.7.0+) との API 不整合で全クライアントの同期が停止する障害が発生した
(PR #49)。このとき同時に取り込み損ねていた上流のセキュリティ修正が 8 件あった。

このコマンドはその再発防止のためのルーティンである。

## 引数

- `$ARGUMENTS` — 省略可
  - 引数なし: 全項目を点検してレポート
  - `check`: 点検のみ（PR を作らない）
  - `<app名>`: 指定アプリのバージョンだけ点検

## 前提

- `tailscale up` 済みであること（未接続なら `just ts-up`）
- 参照は MCP ツール経由で行う（`mcp__kubectl__*` 等）。CLI の kubectl は使わない
- write が必要になった場合は人間の承認（ask）を経ること

## ワークフロー

### 1. 接続確認

`just preflight` で Tailscale / k8s / ArgoCD / Proxmox の疎通を確認する。
到達できないレイヤーがあれば、その項目はスキップしてレポートに明記する
（黙って飛ばさない）。

### 2. バージョン点検

以下が pin されている全バージョンの棚卸し対象。**現在値はファイルから読むこと**
（このリストの値は執筆時点のもので、更新されている可能性がある）。

| 対象 | 定義ファイル | 上流 |
|------|------------|------|
| vaultwarden | `apps/vaultwarden/deployment.yaml` (`image:`) | https://github.com/dani-garcia/vaultwarden/releases |
| busybox (initContainer) | `apps/vaultwarden/deployment.yaml` | Docker Hub `busybox` |
| immich chart | `apps/immich/kustomization.yaml` (`helmCharts[].version`) | `oci://ghcr.io/immich-app/immich-charts` |
| argo-cd chart | `apps/argocd/kustomization.yaml` | https://argoproj.github.io/argo-helm |
| dex chart | `apps/dex/kustomization.yaml` | https://charts.dexidp.io |
| external-secrets chart | `apps/external-secrets/kustomization.yaml` | https://charts.external-secrets.io |
| tailscale-operator chart | `apps/tailscale-operator/kustomization.yaml` | https://pkgs.tailscale.com/helmcharts |

各対象について:

1. 現在 pin されているバージョンをファイルから読む
2. 上流の最新安定版を調べる（GitHub Releases は `mcp__github__get_release_by_tag` /
   `list_releases` を使うとリリース本文が正確に取れる。WebFetch の要約は情報が落ちる）
3. 差がある場合、**間にある全リリースのリリースノートを読む**。1 つ飛ばしの更新でも
   途中バージョンの breaking change は効いてくる
4. 以下を抽出する:
   - **セキュリティ修正**（advisory / CVE / GHSA）→ 最優先
   - **breaking change**（設定項目の削除・改名、デフォルト値の変更、必須化）
   - **クライアント/他コンポーネントとの互換性要件**
   - **アップグレード後に必要な管理作業**（マイグレーション等）

### 3. クラスタ健全性の点検

MCP で以下を確認する。

- **ArgoCD**: 全 Application が `Synced` / `Healthy` か
  (`mcp__kubectl__gitops_apps_list_tool`)
- **Pod**: 全 namespace で異常な状態のものがないか (`mcp__kubectl__get_pods`)
- **ノードのディスク空き**: `mcp__kubectl__node_stats_summary_tool`
  - **node01 のディスクは 20 GB しかなく、空きが逼迫している**（2026-08-03 時点で空き 4.2 GB）
  - **空きが 3 GB を切っていたらレポートの先頭に警告を出す**。DiskPressure に入ると
    ノード全体が不安定になる
  - `local-path` は容量を強制しないため、PVC の requests は実容量を意味しない
    （例: `immich-library` は 50Gi を要求しているが実ディスクは 20 GB）
- **preview の消し忘れ**: `just preview-status` で `HEAD` 以外を追跡中のアプリがないか

### 4. レポート

点検結果を以下の構成でまとめる。

- 冒頭に **要対応の有無**（セキュリティ修正の未取り込み、ディスク逼迫、Sync 異常）
- バージョン差分の一覧（現在 → 最新、リリース日、差の大きさ）
- 各更新の要約（セキュリティ修正 / breaking change / 互換性要件）
- クラスタの状態

**問題がなければ「異常なし」と明記して終わる。** 無理に作業を作らない。

### 5. 更新 PR の作成

更新すべきものがあれば PR を作る。ルール:

- **1 アプリ 1 PR**。複数アプリの更新をまとめない（レビュー粒度を保つため）
- PR 本文には必ず以下を書く:
  - なぜ上げるのか（セキュリティ修正 / 障害回避 / 定期更新）
  - **この構成に該当する変更と、該当しない変更の切り分け**
    （例: 組織機能の脆弱性は単一ユーザー構成では無害、など。該当しないものも
    「確認した上で該当しない」と書く。書かないと再調査が発生する）
  - breaking change の有無と、本構成への影響の確認結果
  - アップグレード後に確認すべき項目（チェックボックス）
- 事前検証として `kubectl kustomize apps/<app>/` がビルドできることを確認する
  （helm chart を使うアプリは `--enable-helm` が要る）
- **マージはしない**。エージェントによるマージはパーミッションで拒否される。
  PR を作るところまでが担当で、マージは人間が判断する

### 6. マージ後の検証

人間がマージしたら、MCP で以下を確認して報告する。

- ArgoCD の `sync_revision` がマージコミットに追随したか（ポーリング既定 3 分）
- Pod が新世代に入れ替わり Running か
- 起動ログにバージョンとエラーの有無

## 注意事項

- **上流の「最新」を鵜呑みにしない**。alpine variant などバリアントごとに事情が違う。
  実例: vaultwarden 1.37.0 は alpine イメージの OpenSSL ビルド不具合があり、
  alpine を使う本構成では 1.37.1 を選ぶ必要があった
- **メジャーバージョンの飛び越しは慎重に**。特に argocd は self-management で
  壊すと復旧が面倒なため、更新は単独 PR にして事前に breaking change を精査する
- helm chart を使うアプリは、chart のバージョンと中のアプリのバージョンが別物である
  ことに注意する。chart 更新がアプリ更新を伴わない場合もその逆もある
- Plans.md に別ストリームとして「helm 脱却」がある。helm chart を手書きリソースへ
  置き換えたアプリは、点検対象がイメージタグに変わるのでこの表を更新すること
