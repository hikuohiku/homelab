# 到達性マトリクス baseline (P-9034)

計器 `ops/tools/reachability_probe.py` の最初の実測。このファイルは **今日の応答マップ** を
記録する (dod 3)。次回以降の実行で応答マップが変わったら、このファイルも更新すること。

## 実行記録

- 日時: 2026-08-24T23:34:27Z (JST 2026-08-25 08:34)
- 環境: クラスタ内 (autopilot namespace の runner Pod `runner-p-9034-a1-8mq68`、node01 / k3s)
- tailnet ドメイン: `tailae6c2.ts.net`
- 実行: `python3 ops/tools/reachability_probe.py` (タイムアウト 3.0s)
- 結果: ok=7 / fail=4 / unknown=6 / total=17 (rc=1 — fail を検知したため)
- 実測 JSON の生データ: `ops/health/reachability.json` (git 管理外の生成物)

## 応答マップ (2026-08-24 時点)

state の意味: `ok` = 到達 / `fail` = 名前解決は通ったが probe 失敗 (死を確認) /
`unknown` = 名前解決自体が失敗 (この実行コンテキストからは観測不能)。

| アプリ | route | port | kind | state | 実測 detail |
|--------|-------|------|------|-------|-------------|
| ops-dashboard | clusterip | 80 | http | ok | HTTP 200 |
| coder | clusterip | 80 | http | ok | HTTP 200 |
| coder-postgres | clusterip | 5432 | tcp | ok | TCP 接続成功 |
| nats | clusterip | 4222 | tcp | ok | TCP 接続成功 |
| vaultwarden | clusterip | 80 | http | ok | HTTP 200 |
| syncthing | clusterip | 8384 | http | ok | HTTP 200 |
| immich-postgres | clusterip | 5432 | tcp | ok | TCP 接続成功 |
| autopilot-heart | clusterip | 8099 | http | **fail** | Connection refused |
| adguard | clusterip | 53 | dns-tcp | **fail** | Connection refused |
| adguard | clusterip | 53 | dns-udp | **fail** | DNS 応答タイムアウト (3.0s) |
| adguard | clusterip | 3000 | http | **fail** | Connection refused |
| adguard | tailnet | 53 | dns-tcp | unknown | 名前解決失敗 (NXDOMAIN) |
| adguard | tailnet | 53 | dns-udp | unknown | 名前解決失敗 (NXDOMAIN) |
| adguard | tailnet | 3000 | http | unknown | 名前解決失敗 (NXDOMAIN) |
| syncthing-sync | tailnet | 22000 | tcp | unknown | 名前解決失敗 (NXDOMAIN) |
| syncthing-sync | tailnet | 22000 | udp | unknown | 名前解決失敗 (NXDOMAIN) |
| syncthing-sync | tailnet | 21027 | udp | unknown | 名前解決失敗 (NXDOMAIN) |

## 解釈

**adguard は実測時点で死んでいた** (clusterIP 53/3000 が refused/timeout = 本プロジェクトが
対象とした失敗モードそのもの。この計器は 2026-08-24 当日のインシデントを 1 回の実行で捕まえた)。
tailnet 側は in-cluster から MagicDNS 名が NXDOMAIN になり unknown (下記のコンテキスト依存)。

- **adguard (fail, 確認された死)**: ClusterIP 経由で 53/tcp (refused)・53/udp (timeout)・
  3000 (refused)。backend 無しの Service を指す典型的な「Pod 死」。
- **autopilot-heart (fail)**: 8099 が refused。dashboard `/api/snapshot` の
  `heart.stale: true` (2026-08-24T23:34 実測) と整合する — 心臓が応答していない。
  ただしこの Service は NetworkPolicy で送信元を `app=autopilot-core` に限定しており
  (apps/autopilot/heart-service.yaml)、runner 等の他 Pod からは正常時でも refused になりうる
  (k3s の netpol は REJECT を使うため refused でも矛盾しない)。単発の refused と
  stale の併存で「心臓死」を疑うのが妥当。
- **tailnet 4 対象 (unknown)**: `adguard.*` / `syncthing-sync.*` の MagicDNS 名が in-cluster の
  resolver (CoreDNS → ts.net stub → ts-nameserver-fixed) で NXDOMAIN。
  **健康な syncthing-sync の MagicDNS 名も NXDOMAIN になることを実測した** (clusterIP
  22000/tcp は TCP 接続成功 = アプリは生きている)。つまり in-cluster の runner からは
  tailnet 経路は「観測不能」であり、解決失敗だけでは死を断定できない。**tailnet 経路の
  本実測は tailnet メンバー (node01 や人間の端末) から実行する必要がある** (未実施のまま残す)。

## 未解決 / 次のセッションへの引き継ぎ

1. **tailnet 経路の実測が in-cluster からはできない**。node01 (tailnet メンバー) から
   同じコマンドを実行して MagicDNS 名が解決できることを確認し、baseline を補完する。
   また `syncthing-sync` の MagicDNS 名が tailnet 側でも NXDOMAIN なのか (→ tailnet デバイス
   が登録されていない / 消えた) を確認する。これが健康な状態かは未確定。
2. **adguard の死は当日も継続中** (2026-08-24T23:34 実測)。修繕は本プロジェクトの外
   (1 PR 1 論点)。この計器が鳴っている事実を人間に届ける配線も外 (アラート配線は本プロジェクト外)。
3. **autopilot-heart の refused が「心臓死」か「自分が宛先外なだけ」か** はこの計器だけでは
   区別できない。dashboard の `heart.stale` と突き合わせて判断する。NetworkPolicy に引っかかる
   実行コンテキストでは心臓の単独観測はできない、という既知の死角 (PROJECT.md / ツール docstring 参照)。
4. 計器の常設 (CI ジョブ追加や定期実行) は worker 判断で今回は見送った。fixture + unittest +
   `--selftest` が判定ロジックを CI (ops job) で守り、実機実行はクラスタ内セッションからいつでも
   可能。自動実行に載せる場合は `ops/health/reachability.json` の読み書き経路と
   アラート配線 (外部) の設計が要る。