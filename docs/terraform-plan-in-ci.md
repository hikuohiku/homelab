# terraform plan を CI で検証できるか (2026-08-05)

`ops/backlog.json` T-0073 の調査記録。issue #56 (2026-08-05 04:45:21) の指摘「実インフラの
変更が誰にも事前確認されないまま main に入り続けるのは、CI で塞げていない最後の大きな穴」
を受けた調査。**設定変更は行っていない。** subagent に一次ソース（HashiCorp 公式ドキュメント、
`tailscale/github-action` の README、`bpg/terraform-provider-proxmox` の実ソース）を確認させた。

## 現状

`terraform/proxmox/providers.tf` は HCP Terraform (Terraform Cloud) の `cloud` ブロックで
state を管理している（organization `hikuohiku`、workspace `homelab`）。CI (`ci.yml`) は
`terraform validate` のみで、`plan` は実行していない（Proxmox は Tailscale 経由でしか
到達できない private LAN 上にあり、GitHub-hosted runner からは届かないため）。

## わかったこと

### 1. HCP Terraform の実行モードは workspace 設定であり、`providers.tf` からは変えられない

`cloud` ブロックを使う workspace の execution mode は3種類（HashiCorp 公式ドキュメント）:

- **Remote（既定）**: `terraform plan`/`apply` は HCP Terraform 自身の使い捨て VM 上で実行される。
  呼び出し元（人間の手元・CI どちらでも）ではない。このリポジトリの workspace は明示的に
  execution mode を指定していないため、現状はこれ（Remote）のはず
- **Local**: `plan`/`apply` は CLI を呼んだ場所（手元のマシンや CI runner 自身）で実行され、
  HCP Terraform は state 保存とロックのみを担う
- **Agent**: HCP Terraform が軽量エージェントプロセスと通信し、閉域網内で `plan`/`apply` を実行する

**execution mode は HCP Terraform の workspace 設定（UI/API）であり、リポジトリのファイルを
編集しても変えられない。**

### 2. HCP Terraform Agent を使えば、k3s クラスタ内に配置したエージェントが plan/apply を実行できる

`hashicorp/tfc-agent`（または `hcp-terraform-operator`）を homelab の tailnet 上（k3s 内等）で
動かせば、そのエージェントが Proxmox に到達しつつ plan/apply を実行し、state/lock は HCP
Terraform 側に置いたままにできる。必要なのは新規の **Agent Pool** と **Agent Token**（HCP
Terraform org の owner 権限で発行）、およびワークスペースの execution mode を Agent に切り替える
こと。エージェント自体は outbound のみで動くため（`app.terraform.io` への HTTPS + `agents.terraform.io`
への TCP/7146）、ファイアウォール的には軽い

### 3. `tailscale/github-action` で GitHub-hosted runner を一時的に tailnet に参加させられる

公式 Action（`tailscale/github-action`）は ephemeral node として GitHub-hosted runner を
tailnet に参加させ、ジョブ終了時に自動ログアウト・削除される。**新規の Tailscale OAuth
client（`writable auth_keys` スコープ、タグ必須）**か、事前署名した reusable+ephemeral+tagged
auth key が要る。ACL 側でそのタグから Proxmox ホストの 8006 番への到達を許可する設定も必要。
execution mode を Local にすれば、この経路で GitHub Actions の job 自身が plan を実行できる

### 4. `bpg/proxmox` provider の Read（plan 相当）は監査権限で足りそうだが、公式に文書化されてはいない

provider の実ソース（`proxmoxtf/resource/vm/vm.go`, `fwprovider/nodes/vm/model.go`,
`fwprovider/nodes/resource_download_file.go`）を確認すると、`Read`/`plan` 相当の経路は
`GET /nodes/{node}/qemu/{vmid}/config` や `GET /nodes/{node}/storage/{storage}/content` のような
GET/list のみで、Proxmox の `*.Audit` 権限（`VM.Audit`, `Sys.Audit`, `Datastore.Audit` 等 =
組み込み `PVEAuditor` ロール）で足りるはず。ただし bpg 自身のドキュメントに plan/apply の
権限区分表は無く、これは実ソースからの推論であって公式の保証ではない。既存の
`PROXMOX_AGENT_TOKEN`（MCP 用、PVEAuditor スコープ）とは別に、CI 専用の token を発行するのが
筋（既存 token の用途混在を避ける、CHARTER のクレデンシャル分離方針に沿う）

## 結論・推奨

到達性の問題（GitHub Actions runner または HCP Terraform 自身の実行環境が Proxmox に届かない）
を解決する2つの現実的な方式は、どちらも **このリポジトリの外側（Tailscale 管理コンソール /
HCP Terraform org 設定）で人間が新しい credential・設定変更を行わないと成立しない**:

- **方式A（HCP Terraform Agent）**: k3s 内にエージェントを配置。恒久的なワークロードが増える
  代わりに GitHub Actions 自体は Proxmox に触れない（変更範囲が repo 外の Agent Pool/Token
  発行と workspace 設定のみ）
- **方式B（Tailscale-in-CI + Local 実行）**: 恒久ワークロードは増えないが、ジョブのたびに
  ephemeral な tailnet ノードが生える。新規 Tailscale OAuth client（専用タグ + ACL）、
  execution mode の Local への切り替え、CI 専用の PVEAuditor スコープ Proxmox token が要る

このリポジトリの既存方針（read-only は MCP 経由、CI は現状 Proxmox に一切到達しない）を踏まえると
方式B の方が影響範囲が小さい。ただし**どちらも autopilot 単独では実装できない**
（`ops/backlog.json` T-0074, needs-human）。

## 参考

- [HCP Terraform Workspace 設定 (execution mode)](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/settings)
- [HCP Terraform Agents](https://developer.hashicorp.com/terraform/cloud-docs/agents)
- [tailscale/github-action](https://github.com/tailscale/github-action)
- [bpg/terraform-provider-proxmox](https://github.com/bpg/terraform-provider-proxmox)
