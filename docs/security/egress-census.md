# クラスタ外向き通信 (egress) の全数台帳 — P-0203

> 生成: `python3 ops/security/egress_census.py` / 差分検査: `--check`。
> 静的台帳であり**実クラスタへの通信試験は一切していない**。「manifest とコード上、どこへ出ていくはずか」の帳簿。到達性の実測は次のプロジェクト。

## サマリ

| namespace | endpoint 数 | 既定拒否で開けるべき穴 |
|---|---|---|
| external-secrets | 1 | 1 |
| argocd | 6 | 6 |
| dex | 1 | 1 |
| tailscale | 1 | 1 |
| immich | 2 | 2 |
| vaultwarden | 2 | 2 |
| coder | 4 | 4 |
| syncthing | 2 | 2 |
| version-watcher | 3 | 3 |
| ops-health-reporter | 1 | 1 |
| autopilot | 10 | 9 |
| (node) | 5 | 0 |

## namespace 別表

### external-secrets

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `external-secrets` | `api.doppler.com:443` | ClusterSecretStore doppler (provider: doppler) — ESO が homelab/prd の secret を同期しに行く先。host は ESO doppler プロバイダの定数で manifest に直書きされない [source_evidence: apps/external-secrets/cluster-secret-store.yaml] | **開ける** | 全 namespace (12 箇所) の ExternalSecret 同期が停止する。既存 Secret は残るが refresh 失敗が積み重なり、回転後の新 credential が届かない |

### argocd

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `argocd-repo-server` | `argoproj.github.io` | argo-cd chart 自身の取得元 (Helm inflation) | **開ける** | ArgoCD 自身の再デプロイ・upgrade ができない |
| `argocd-repo-server` | `charts.dexidp.io` | dex chart の取得元。ArgoCD repo-server が Helm chart の取得先として使う(kustomization.yaml の helmCharts.repo) | **開ける** | dex のデプロイ・upgrade ができない |
| `argocd-repo-server` | `charts.external-secrets.io` | external-secrets chart の取得元。ArgoCD repo-server が Helm chart の取得先として使う(kustomization.yaml の helmCharts.repo) | **開ける** | ESO のデプロイ・upgrade ができない |
| `argocd-repo-server` | `ghcr.io` | oci://ghcr.io/immich-app/immich-charts からの OCI Helm chart 取得 | **開ける** | immich のデプロイ・upgrade ができない |
| `argocd-repo-server` | `github.com` | Application の repoURL https://github.com/hikuohiku/homelab.git を argocd-repo-server が git fetch する (App of Apps の全 Application) | **開ける** | 全 ArgoCD Application の sync が停止し、クラスタの宣言的運用が止まる |
| `argocd-repo-server` | `pkgs.tailscale.com` | tailscale-operator chart の取得元。ArgoCD repo-server が Helm chart の取得先として使う(kustomization.yaml の helmCharts.repo) | **開ける** | tailscale-operator のデプロイ・upgrade ができない |

### dex

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `dex` | `accounts.google.com` | Google OIDC connector の issuer (config.issuers.connectors[].config.issuer)。discovery/JWKS/token を dex server が上流取得する | **開ける** | Google アカウントでの SSO 全不能 (ArgoCD ログインを含む) |

### tailscale

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `tailscale-operator (+proxy pods)` | `controlplane.tailscale.com:443` | Tailscale client の coordination server。host は Tailscale client のprovider 定数で manifest に直書きされない [source_evidence: apps/tailscale-operator/kustomization.yaml (chart 導入 + apiServerProxyConfig)] | **開ける** | operator/proxy が tailnet から離脱し、*.ts.net ingress と K8s API proxy 認証が全滅する |

### immich

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `immich-restic-backup` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |
| `immich-restic-retention` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |

### vaultwarden

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `vaultwarden-restic-backup` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |
| `vaultwarden-restic-retention` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |

### coder

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `coder-restic-backup` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |
| `coder-restic-retention` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |
| `coder-workspace-home-backup` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |
| `coder-workspace-home-backup-retention` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |

### syncthing

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `syncthing-restic-backup` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |
| `syncthing-restic-retention` | `api.backblazeb2.com:443` | RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す (restic b2 backend の provider 定数。manifest には host 名は出ない) | **開ける** | バックアップと retention (forget --prune) の両方が失敗し、災害時に復元点が無い。PVC 単独障害でデータロストに直結 |

### version-watcher

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `version-watcher` | `api.github.com` | inventory 対象の最新版確認 (GitHub releases / Docker Hub tags / npm registry) | **開ける** | バージョン監視が止まり、pin の放置による同期停止事故 (#49 型) の早期発見を失う |
| `version-watcher` | `hub.docker.com` | inventory 対象の最新版確認 (GitHub releases / Docker Hub tags / npm registry) | **開ける** | バージョン監視が止まり、pin の放置による同期停止事故 (#49 型) の早期発見を失う |
| `version-watcher` | `registry.npmjs.org` | inventory 対象の最新版確認 (GitHub releases / Docker Hub tags / npm registry) | **開ける** | バージョン監視が止まり、pin の放置による同期停止事故 (#49 型) の早期発見を失う |

### ops-health-reporter

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `ops-health-reporter` | `api.github.com` | K8s/ArgoCD 健全性の GitHub への報告と issue 参照 (GITHUB_HEALTH_REPORTER_TOKEN) | **開ける** | autopilot と人間への健全性報告が途絶える (soak 判定も不能化) |

### autopilot

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `autopilot` | `github.com` | git clone/fetch/push (main への帳簿 push と PR)。credential.helper に https://github.com を設定 | **開ける** | autopilot が repo を読み書きできず自律運用全体が停止する |
| `autopilot-core` | `api.github.com` | ops-feedback ブランチの読み取り (GITHUB_API 既定値) | **開ける** | コアがフィードバックを読めなくなる |
| `autopilot-heart` | `api.anthropic.com:443` | allowed_autopilot_doppler_keys の CLAUDE_CODE_OAUTH_TOKEN 由来。Claude Code OAuth の接続先 (provider 定数。repo 内に直書きは無い) [source_evidence: ops/rules.json allowed_autopilot_doppler_keys] | **開ける** | autopilot の思考エンジンが動かず、全プロジェクトが停止する |
| `autopilot-heart` | `api.telegram.org:443` | allowed_autopilot_doppler_keys の TELEGRAM_BOT_TOKEN 由来。Telegram Bot API。許可鍵リストに入っている以上、直接送信経路として存在しうる (主経路は telegram-adapter) [source_evidence: ops/rules.json allowed_autopilot_doppler_keys] | **開ける** | autopilot からの Telegram 通知が失われる可能性がある |
| `autopilot-heart` | `discord.com:443` | allowed_autopilot_doppler_keys の DISCORD_WEBHOOK_URL 由来。通知 webhook の POST 先 (ops/heart/notify.py の _post_discord)。URL 自体は Doppler から注入されるため repo に host 直書きは無い [source_evidence: ops/rules.json allowed_autopilot_doppler_keys] | **開ける** | 予告・納品・障害の即時通知が全て失われる (digest も届かない) |
| `autopilot-heart` | `github.com` | heart 起動時の repo clone/fetch (REPO_URL 既定値) | **開ける** | heart-and-projects の起動ができない |
| `ops-dashboard` | `api.github.com` | feedback 投稿時の GitHub API 呼び出し (GITHUB_API 既定値) | **開ける** | dashboard から ops-feedback への書き置きができなくなる |
| `ops-dashboard` | `github.com` | ISSUE_URL — フィードバック issue へのリンク文字列の生成のみ。サーバからの通信先ではなくブラウザが開く先なので、既定拒否で開ける穴には数えない | 不要 (管轄外) | 壊れない (リンク文字列)。リンク先自体は人間の閲覧経路 |
| `telegram-adapter` | `api.github.com` | Telegram Bot API の long polling (TELEGRAM_API 既定値) と、受信 DM を ops-feedback へ流す際の GitHub API (GITHUB_API 既定値) | **開ける** | 人間→autopilot の Telegram 受信窓が閉じる |
| `telegram-adapter` | `api.telegram.org` | Telegram Bot API の long polling (TELEGRAM_API 既定値) と、受信 DM を ops-feedback へ流す際の GitHub API (GITHUB_API 既定値) | **開ける** | 人間→autopilot の Telegram 受信窓が閉じる |

**autopilot namespace 対象外にする場合の例外理由文言:**

- `autopilot`: 暫定例外: lethal trifecta 分離 (stage3 seeds #11) 未実施のため、既定拒否 NetworkPolicy を autopilot namespace 対象外とする場合がある。その場合でも本 endpoint は器自身の生存に必須であり、分離適用時には workload 別の egress ルールとして復刻すること
- `autopilot-core`: 暫定例外: lethal trifecta 分離 (stage3 seeds #11) 未実施のため、既定拒否 NetworkPolicy を autopilot namespace 対象外とする場合がある。その場合でも本 endpoint は器自身の生存に必須であり、分離適用時には workload 別の egress ルールとして復刻すること
- `autopilot-heart`: 暫定例外: lethal trifecta 分離 (stage3 seeds #11) 未実施のため、既定拒否 NetworkPolicy を autopilot namespace 対象外とする場合がある。その場合でも本 endpoint は器自身の生存に必須であり、分離適用時には workload 別の egress ルールとして復刻すること。本行は credential allowlist 由来であり、鍵を rules.json から削除すれば台帳からも消れる
- `ops-dashboard`: 暫定例外: lethal trifecta 分離 (stage3 seeds #11) 未実施のため、既定拒否 NetworkPolicy を autopilot namespace 対象外とする場合がある。その場合でも本 endpoint は器自身の生存に必須であり、分離適用時には workload 別の egress ルールとして復刻すること
- `telegram-adapter`: 暫定例外: lethal trifecta 分離 (stage3 seeds #11) 未実施のため、既定拒否 NetworkPolicy を autopilot namespace 対象外とする場合がある。その場合でも本 endpoint は器自身の生存に必須であり、分離適用時には workload 別の egress ルールとして復刻すること


### (node)

| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |
|---|---|---|---|---|
| `node01/helm-controller (bootstrap)` | `argoproj.github.io` | 初回ブートストラップの HelmChart CR (argo-cd) の取得元。ArgoCD 起動後は apps/argocd/kustomization.yaml 側に主導権が移る | 不要 (管轄外) | 空クラスタからの ArgoCD ブートストラップができない |
| `node01/kubelet (image pull)` | `docker.io:443` | python/restic/busybox/postgres/vaultwarden/syncthing 等の Docker Hub イメージ。image pull は kubelet = ノード側 egress で Pod NetworkPolicy の対象外という重要な注意付きで載せる | 不要 (管轄外) | 新規 Pod の起動・再スケジュールができない (既存コンテナは影響なし) |
| `node01/kubelet (image pull)` | `ghcr.io:443` | 自前イメージ群 (homelab-*) と vectorchord/coder 等。image pull は kubelet = ノード側 egress で Pod NetworkPolicy の対象外という重要な注意付きで載せる | 不要 (管轄外) | 新規 Pod の起動・再スケジュールができない (既存コンテナは影響なし) |
| `node01/nix-daemon` | `cache.nixos.org` | nix.settings.substituters — ノード構築・再構成時の binary cache 取得 | 不要 (管轄外) | ノードの再構築・設定変更適用ができない (Pod ではないため NetworkPolicy 管轄外。node firewall レイヤーの統制対象) |
| `node01/nix-daemon` | `hikuohiku.cachix.org` | nix.settings.substituters — ノード構築・再構成時の binary cache 取得 | 不要 (管轄外) | ノードの再構築・設定変更適用ができない (Pod ではないため NetworkPolicy 管轄外。node firewall レイヤーの統制対象) |

## 横串: 主要依存が塞がれたときの被害一覧

- **Doppler** (`api.doppler.com`) — External Secrets Operator の同期経路。塞がると全 namespace の Secret 更新が止まり、credential 回転が効かなくなる
- **Backblaze B2** (`api.backblazeb2.com`) — restic backup 全 5 リポジトリの保存先。塞がるとバックアップも retention も失敗する
- **GitHub** (`github.com` / `api.github.com`) — ArgoCD の manifest 取得、autopilot の git push、version-watcher / health-reporter / dashboard / telegram-adapter の API 呼び出し。塞がると宣言的運用と自律運用の双方が停止する
- **Telegram** (`api.telegram.org`) — 人間からの指示窓口。塞がるとフィードバックが届かない
- **Google OIDC 上流** (`accounts.google.com`) — Dex 経由の SSO 全不能
- **Tailscale coordination** (`controlplane.tailscale.com`) — tailnet 参加資格そのもの。塞がると ts.net ingress 全滅
- **コンテナレジストリ系** (`ghcr.io` / `docker.io`) — ArgoCD の OCI chart 取得と kubelet の image pull。pull は node 側で NetworkPolicy 管轄外
- **Discord webhook** (`discord.com`) — autopilot の通知出口

## 台帳から除外したホスト

| host | 分類 | 理由 |
|---|---|---|
| `127.0.0.1:1` | cluster_local | クラスタ内アドレス (Service/API server) (apps/autopilot-core/app/main_test.go:181 apps/autopilot-core/app/mcp_test.go:106) |
| `127.0.0.1:4096` | cluster_local | クラスタ内アドレス (Service/API server) (apps/autopilot-core/app/main.go:68 apps/autopilot-core/deployment.yaml:141) |
| `kubernetes.default.svc` | cluster_local | クラスタ内アドレス (Service/API server) (apps/agent-rbac/application.yaml:17 apps/apps.yaml:17) |
| `ops-dashboard.autopilot.svc` | cluster_local | クラスタ内アドレス (Service/API server) (apps/autopilot-core/app/mcp.go:103) |
| `opencode.ai` | schema_reference | $schema 等のメタデータ参照。通信しない (apps/autopilot-core/config.yaml:13) |
| `argocd.tailae6c2.ts.net` | self_public_url | tailnet 上の自サービス公開 URL。ブラウザやクライアント側の接続先であり、workload からの egress 先ではない (apps/argocd/values.yaml:9 apps/dex/values.yaml:47) |
| `coder.tailae6c2.ts.net` | self_public_url | tailnet 上の自サービス公開 URL。ブラウザやクライアント側の接続先であり、workload からの egress 先ではない (apps/coder/deployment.yaml:39) |
| `dex.tailae6c2.ts.net` | self_public_url | tailnet 上の自サービス公開 URL。ブラウザやクライアント側の接続先であり、workload からの egress 先ではない (apps/argocd/values.yaml:15 apps/dex/values.yaml:10) |
| `vaultwarden.tailae6c2.ts.net` | self_public_url | tailnet 上の自サービス公開 URL。ブラウザやクライアント側の接続先であり、workload からの egress 先ではない (apps/vaultwarden/deployment.yaml:51) |

## 既知の盲点 (repo からは名前が取れない)

- **LLM API 実接続先の一部**: OPENCODE_API_KEY の向こう側の endpoint は repo 内に直書きが無い。api.anthropic.com は CLAUDE_CODE_OAUTH_TOKEN の provider 定数として載せたが、telemetry 系 (statsig 等) は確定できない
- **Syncthing の global discovery / relay**: discovery.syncthing.net 等の宛先は GUI (PVC 上の config) で決まり repo に現れない。tailnet 直接接続のみで成立しているかは実測が必要 (実測プローブは次のプロジェクト)
- **Vaultwarden の icon 取得**: icon cache が有効な場合、登録済みサイトの任意 host へ出ていく。repo には現れない
- **Tailscale DERP relay**: *.derp.tailscale.com (UDP/TCP 443) は NAT 越えが必要なときだけ使う。coordination 本体 (controlplane.tailscale.com) とは別に開ける判断が要る
- **coder の workspace agent 接続**: CODER_ACCESS_URL (https://coder.tailae6c2.ts.net) は公開 URL としてself_public_url に除外したが、deployment.yaml のコメント通り workspace agent の接続先にもなる。既定拒否適用時は agent → coder の実経路を確認すること (実測プローブは次のプロジェクト)
- **kubelet の image pull**: docker.io / ghcr.io からの pull は node 側 egress。既定拒否 NetworkPolicy では防げないので、統制は node firewall レイヤーで別途検討
