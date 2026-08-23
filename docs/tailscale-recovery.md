# Tailscale identity の失効と再認証台本

この homelab の外部到達性 (ts.net の各 Service、kube-apiserver proxy、tailscale MCP、人間の
アクセスすべて) は tailnet 一点依存。identity と資格情報には寿命があるが、期限を確認する
経路が今まで存在しなかった。失効すれば heart は生き続けるのに外部から何も届かなくなり、
watchdog (GitHub Actions) だけが鳴る — 沈黙事故の別形が起きうる。
P-0144 で作成 (2026-08-23)。公式ドキュメントの出典は末尾。

## 実測の現状とこの文書の限界

tailnet 内デバイスごとの実測 (`ops/projects/logs/P-0144/devices.json`) は **2026-08-23 時点で未取得**。
worker サンドボックスには tailscale バイナリ・OAuth credential・MCP が無いことを実測済み
(P-0107 の 2026-08-22 実測を P-0144 worker が同日再実測。`api.tailscale.com` への到達性のみ確認 = 認証なしからの 401)。
したがって本文の数値は**既定値であり実デバイスの期限ではない**。devices.json を取得したら
「期限の棚卸し」表を実測値で更新すること。取得手順は末尾「実測の取り方」。

## tailnet identity の実トポロジ (repo 実測)

node01 自身は tailscaled を走らせていない (`nix/images/proxmox-cloud/configuration.nix` に
tailscale の設定がない。手動で入れた履歴の有無は repo からは分からないので、devices.json に
node01 名義のデバイスが現れるかで確定させる)。tailnet につながっている identity は 2 系統:

| 系統 | 実体 | 鍵失効 | 備考 |
|---|---|---|---|
| cluster 側 | tailscale-operator 本体 + ts.net proxy 群 (argocd / dex / coder / vaultwarden / immich / ops-dashboard / syncthing-sync / kube-apiserver proxy)。proxy の hostname 接頭辞は `k8s-` (`apps/tailscale-operator/kustomization.yaml` の `operatorConfig.hostname`) | operator が付ける tag (`tag:k8s` 等) のついた tagged デバイスは**鍵失効が既定で無効** | proxy は起動のたび operator が OAuth client で auth key を発行して自動認証する。人がブラウザで認証する箇所は存在しない |
| 人間側 | dev machine / コンテナ (`just ts-up` = `tailscale up`)、tailscale MCP (エージェント用 read-only OAuth client) | 非 tagged なら**既定 180 日で失効** | 失効すると人間のアクセスと MCP が死ぬ。heart (cluster 内) は生き続ける |

寿命を持つ資格情報の全体像:

| 資格情報 | 寿命 | 消費者 | 失効時の症状 |
|---|---|---|---|
| ノードキー (非 tagged) | 既定 180 日 (1–180 日で変更可。変更は次回ログインから効く) | 人間側マシン | そのデバイスの tailnet 接続が切れる |
| ノードキー (tagged) | 既定で無効 | operator と proxy 群 | (失効しない) |
| OAuth client (operator 用) | **原理的に失効しない** (revoke まで有効) | `Secret/tailscale/operator-oauth` ← ExternalSecret ← Doppler `TAILSCALE_CLIENT_ID/SECRET` | 新規の proxy 認証と API 呼び出しが全滅。既存接続は restart まで生きる (「静かな死」。後述) |
| OAuth client (エージェント用 read-only, scope `devices:core:read`) | 同上 | `.envrc` の `TAILSCALE_AGENT_CLIENT_ID/SECRET` → tailscale MCP、fetch_devices.py | `mcp__tailscale__*` が全滅。クラスタ本体には影響しない |
| API access token (OAuth client が発行するもの) | **1 時間固定** (変更不可) | 上記両方の内部 | 自動で取り直すので運用上は無関係 |
| auth key | 最大 90 日 | (現状どこでも未使用) | 新規デバイス登録に使えなくなるだけ |

## (a) 再認証台本

### ケース 1: 人間側デバイスの鍵が切れそう/切れた (接続はまだある)

```bash
sudo tailscale up --force-reauth
```

表示される URL を、tailnet に属するアカウントでログイン済みのブラウザで開いて承認すると
再認証完了。鍵は既定寿命 (6 か月) で更新される。

> **警告** (公式ドキュメントそのまま): `--force-reauth` は tailnet 接続が一時的に落ちうる。
> **SSH over tailscale でリモート作業中のマシンに対しては、代替ログイン経路を確保してから行うこと。**

自動化の境界: 再認証の本体はブラウザでの OAuth 承認なので**人間の作業**。
器 (autopilot / worker セッション) ができるのは期限の検知と briefing への記載まで。
`TS_AUTHKEY` (auth key) を渡せば無人認証も技術的には可能だが、auth key の発行は
credential の発行判断を伴うので人間の領域 (CHARTER「人間に渡すもの」)。

### ケース 2: すでに失効して締め出された (remote で触れない)

管理コンソール [Machines](https://console.tailscale.com/admin/machines) → 該当デバイスの
メニュー → **Temporarily extend key** (30 分だけ延長される。失効済みデバイスにのみ出現) →
その窓の中でケース 1 を実行して再認証する。

> **罠**: 先に **Disable key expiry** を押すと復旧になるどころか Temporarily extend key の
> 選択肢が消えて詰む実例がある ([tailscale/tailscale#19785](https://github.com/tailscale/tailscale/issues/19785))。
> Disable は予防措置であって復旧手段ではない。締め出し済みデバイスでは extend を先にする。

### ケース 3: cluster 側 (operator / proxy) の identity

基本、起きない設計になっている (tagged なので鍵失効なし + OAuth による自動再認証)。
疑わしいときの切り分け順:

1. proxy pod を作り直す (`kubectl -n tailscale delete pod -l app=<proxy>` 等)。
   ArgoCD selfHeal が望む状態へ戻し、operator が OAuth で再登録する。ここで直れば鍵は無傷
2. 直らないなら operator のログを見る。OAuth client が死んでいる典型的な顔は
   `oauth2: cannot fetch token: 401 Unauthorized ... creating operator authkey`
   (tailscale/tailscale#17278 に実例) → 「(b) OAuth client」の手順へ
3. node01 名義のデバイスが devices.json に現れた場合 (= 手動で tailscaled が動いている)、
   それは非 tagged の可能性が高いのでケース 1/2 に従う

## (b) OAuth client の失効が MCP / ExternalSecret に与える影響

OAuth client は日数で失効しない (revoke まで有効)。よって cluster 側に「期限の日付カウント」は
存在せず、実際のトリガは次の 3 つ:

1. 人間が admin console で revoke / ローテーションした (**revoke すると発行済みの
   access token も即時無効化される**)
2. client secret の喪失 (secret は再表示できない。失くしたら作り直し)
3. Doppler 側の secret が消えた・壊れた (ExternalSecret の源泉)

消費者ごとの影響:

| 消費者 | 症状 | 復旧 |
|---|---|---|
| tailscale-operator (`apps/tailscale-operator/kustomization.yaml` で `oauth: {}` → 既存 Secret `operator-oauth` を使用) | **静かな死**: 動いている proxy の接続は restart まで生き続け、ArgoCD/k8s は Degraded にならない。restart や新規 Service 追加で初めて 401 で落ちる。外部からの到達性だけが消える | 下記の手順 |
| tailscale MCP (`.envrc` エージェント用 client) | `mcp__tailscale__*` 全滅。クラスタ本体への影響はない | admin console で新 client (scope `devices:core:read`) → Doppler 更新 → `direnv allow` → Claude Code 再起動 |
| fetch_devices.py (P-0144 の実測ツール) | token 取得が 401 | MCP と同じ client で可 |

operator 用 OAuth client の復旧手順 (人間の作業):

1. admin console [Trust credentials](https://console.tailscale.com/admin/settings/trust-credentials) で新 client を発行する。
   スコープは公式インストール手順どおり **Services / Devices Core / Auth Keys の Read+Write、
   tag に `tag:k8s-operator`** (troubleshooting ドキュメントは `devices:core` + `auth_keys` write を必須と明言)
2. Doppler の `TAILSCALE_CLIENT_ID` / `TAILSCALE_CLIENT_SECRET` を新しい値に更新する
3. ExternalSecret が `refreshInterval: 1h` で同期し、`Secret/tailscale/operator-oauth`
   (`client_id` / `client_secret`) が置き換わるのを待つ (`deletionPolicy: Retain` なので
   ExternalSecret を消しても Secret は残る)
4. operator を再起動する (`kubectl -n tailscale rollout restart deploy/tailscale-operator`)
   — 公式 troubleshooting は「新 client に差し替えたら operator を完全に再インストールせよ」
   というより強い指示もある。rollout restart で直らなければ ArgoCD 経由で chart の再 sync
   (= 再インストール相当) まで昇格させる
5. 既存 proxy のうち 401 で落ちているものを作り直す (ケース 3 の 1.)

> **観測の盲点**: ESO は Doppler の値が変わっただけでは Synced のまま (むしろ正しく同期している)。
> つまり「OAuth client が死んだ」ことは k8s 側のどのリソース状態にも現れない。
> 検知できるのは operator ログの 401 と「外から届かない」という症状だけである (#36 の
> Bearer 無視制約と同型の「identity 層の故障は k8s の健康状態に出ない」問題)。

## (c) briefing へ乗せるべきリードタイムの提案

- **expiry が有効なデバイス (非 tagged)**: 期限の **30日前 / 14日前 / 3日前** の 3 回を提案する。
  根拠: 再認証作業自体は数分だが着手は人間待ちであり、唯一の事後救済 (Temporarily extend key)
  が **30 分**しかないため「切れてから」では余裕がない。180 日の既定寿命に対し 30 日前なら
  余裕は十分 (週次点検 1〜2 回分の猶予)
- **expiry disabled のデバイス (tagged 含む)**: カウント不要。ただし「disabled にした理由と
  台帳」が残らないと #49 型の据え置き問題になるので、四半期ごとの棚卸し (devices.json 再取得)
  を提案する
- **OAuth client**: 日数カウント不能なので、代わりに (i) 作成日と用途を ops/inventory.json に
  記録する (ii) 意図的ローテーションの判断 (例: 年 1 回) を人間に briefing で問う (iii)
  revoke した場合は発行済み token も即死することを添える
- **実測の鮮度**: `expires` の値は再認証のたび伸びるため、devices.json は取得時点のスナップショット
  にすぎない。「あの時 90 日だった」は意味がなく、監視に乗せるなら毎回再取得が正
  (常設化するかどうかは PROJECT.md「やらないこと」のとおり次の論点)

## 実測の取り方

dev マシンなど credential (`TAILSCALE_AGENT_CLIENT_ID/SECRET` または `TAILSCALE_API_KEY`) の
ある環境で:

```bash
python3 ops/projects/logs/P-0144/fetch_devices.py
```

- 読み取り専用 (token 発行の POST とデバイス一覧の GET のみ。tailnet への変更は一切しない)
- `ops/projects/logs/P-0144/devices.json` に生応答、同ディレクトリの `devices.md` に
  期限昇順の整形表 (node01 / cluster proxy / autopilot 関連に印) を書く

## 出典 (すべて 2026-08-23 参照)

- Key expiry — https://tailscale.com/docs/features/access-control/key-expiry (validated Jan 5, 2026)
- OAuth clients — https://tailscale.com/docs/features/oauth-clients / 公式ブログ (2023-01-26,
  「OAuth clients … don't expire」)
- Tagged devices の鍵失効既定無効化 — https://tailscale.com/blog/tagged-key-expiry (2022-03-10)
- k8s operator の OAuth scopes — https://tailscale.com/docs/kubernetes-operator/install-operator /
  https://tailscale.com/docs/reference/troubleshooting/containers/kubernetes-operator
- 締め出しトラブルの実例 — https://github.com/tailscale/tailscale/issues/19785 ,
  operator 起動時 401 の実例 — https://github.com/tailscale/tailscale/issues/17278
