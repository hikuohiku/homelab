# P-0142 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### 2026-08-23 セッション1 — initializer (PROJECT.md 作成)

**やったこと**: PROJECT.md を作成し、受入 verify 3 項目すべてが現時点で failing であることを
実測した (rc=1 / rc=1 / rc=2、いずれも対象ファイル未存在)。commit 済み。push は wrapper が行う。

**次のセッションへの一言**: 最初にやることは Proxmox MCP が生きているかの確認。
`terraform/proxmox/pbs.tf.ignore` の「確認できていないこと」節が本プロジェクトで埋める穴。
取れない場合はその事実を pbs-inventory.json に記録して保守側の verdict を出すこと
(推測で埋めない)。

### 2026-08-23 セッション2 — pbs-inventory.json (受入2項目目 green)

**やったこと**: 受入 verify #2 (`pbs-inventory.json`) を作成し、自分で実行して green (rc=0)。
commit 済み。push は wrapper が行う。

**実測したこと (Proxmox MCP の生存確認 = initializer の指定どおり最初に実施)**:

- 本 worker セッションのツールセットに `mcp__proxmox__*` が存在しない。`.mcp.json` は
  Claude Code 起動時に読まれる構成で、この実行環境では MCP サーバーが起動していない
- 環境変数に `PROXMOX_*` / `PVE_*` 系が無い (`env | grep -iE 'proxmox|pve'` rc=1)。
  pbs.tf.ignore 冒頭コメントの 2026-08-07 実測と同じ結果の再確認
- tailscale CLI も無く、node01 管理面へのネットワーク経路が無い。
  `proxmox_endpoint` の実値は TFC workspace 側の変数注入でリポジトリ内に無く、
  到達試行自体も不可能

→ PBS VM の稼働状況・vzdump backup job・PBS 内部 job は **1 つも観測できなかった**
(「無かった」ではなく「見えなかった」)。inventory は PROJECT.md 規定どおり
`source: unavailable` + `jobs: []` + 失敗の記録 + 推測値ゼロで作成。

**分かったこと / 判断**:

- verdict は **`keep`** (退役保留)。ただしこれは「PBS 固有の価値を実測して見つけた」からでは
  なく「実測不能ゆえの保守側デフォルト」(CHARTER §4「確認していないものを無いとみなさない」)。
  inventory の `reason` に再判断条件を明記済み
- dod (2) の突き合わせ表は `restic_targets[].pbs_protects` 列として JSON 内に実装したが、
  全行 `unobserved` — 「PBS にしか無いもの」の有無自体が未確定。docs/backup.md 側の
  restic 対象 6 行・保持世代・復元試験実績はすべて引用して埋め込み済みなので、
  PBS 側のデータが取れたセッションは列を置き換えるだけで表が完成する
- **verdict = keep のため docs/pbs-retirement.md は書かない** (PROJECT.md「決めてあること」の
  規定どおり)。verify #1・#3 は green にならないまま完結しうる — spec dod (2)
  「その結論も成果」のケース。wrapper が verify 全 green を要求して詰まるようなら、
  この PROGRESS の記述が判断根拠

## 発見 (スコープ外。curriculum が拾うこと)

- **worker 実行環境に `.mcp.json` の MCP サーバーが一切接続されていない**。CLAUDE.md の
  「インフラ参照は MCP 経由」というルールが、この種の worker セッションではそもそも成立しない。
  本プロジェクト (P-0142) の主題である「誰も PBS の中を読んだ者がいない」は、
  credential の欠如に加えて**実行環境側の制約**でもある。infra read を要する project を
  採択するときは、curriculum は「その cell がこの実行環境から観測可能か」を先に見るべき
  （PVE/PBS 層は不可、k8s/ArgoCD 層も同様に mcp__kubectl__ / mcp__argocd__ が無いので不可のはず。
  未実測だがツールセットの構造上ほぼ確実）

**次のセッションへの一言**: 残る作業は dod (4) のみ — `terraform/proxmox/pbs.tf.ignore` の
「確認できていないこと」節を本実測 (2026-08-23 再確認、MCP/credential/tailscale すべて不達)
で置き換えること。resource 本体と経緯節は触らない。`.ignore` のまま維持。
手順書 (docs/pbs-retirement.md) は書かないこと (verdict=keep。上記参照)。
