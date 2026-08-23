# P-0144 — PROGRESS

## セッション記録

### worker #1 (2026-08-23) — verify#2 (再認証台本) を green 化。実測 (verify#1/#3) はサンドボックスに credential が無く不能を実測 → 実測ツールと人間への依頼を用意

**やったこと**:

- サンドボックスの能力を本日付で再実測: tailscale/direnv/doppler/just/sops バイナリ無し、
  SA token 未マウント (`/var/run/secrets/kubernetes.io/serviceaccount` 不在)、`~/.kube` 無し、
  `~/.config/opencode/opencode.json` に MCP 未定義 (= **headless worker に
  `mcp__tailscale__*` は存在しない**)、env に TAILSCALE_* 無し。P-0107 の 2026-08-22 実測と同型。
  ただし `api.tailscale.com` への到達性はある (認証なしから HTTP 401)
- **PROJECT.md の前提を 1 件訂正**: node01 自身は tailscaled を走らせていない
  (`nix/images/proxmox-cloud/configuration.nix` に tailscale 設定なし)。tailnet identity は
  (1) operator + tagged proxy 群 (argocd / dex / coder / vaultwarden / immich /
  ops-dashboard / syncthing-sync / kube-apiserver proxy。hostname 接頭辞 `k8s-`)、
  (2) 人間側マシン + エージェント用 read-only OAuth client の 2 系統。
  台本はこの実トポロジで執筆した。「node01 名義のデバイスが実在するか」は devices.json 待ち
- **docs/tailscale-recovery.md 新設 → verify#2 green 実測 (rc=0)**。内容:
  資格情報ごとの寿命の棚卸し表 / ケース1 (`tailscale up --force-reauth` + ブラウザ。
  force-reauth は接断しうるのでリモート作業中は代替経路確保が前提) /
  ケース2 (Temporarily extend key は 30 分のみ。**先に Disable key expiry を押すと復旧不能に
  なりうる罠 = tailscale#19785**) / ケース3 (cluster 側は OAuth 自動再認証が基本なので pod 再作成から)/
  自動化の境界 (器=検知と briefing まで、人間=ブラウザ承認・extend・OAuth 発行・Doppler 更新)/
  OAuth client 失効の影響と復旧手順 (**ESO は Doppler 源が変わらない限り Synced のまま = 
  k8s 状態に故障が現れない盲点**) / リードタイム提案 (非 tagged 鍵は 30日前/14日前/3日前、
  OAuth client は日数カウント不能のため inventory 帳簿化+年 1 回ローテ判断の提案)
  数値はすべて公式ドキュメント参照 (文書末尾に出典日付き)。実デバイス期限が未実測であることを冒頭に明記
- **fetch_devices.py 新設**: 読み取り専用の実測ツール。POST `/oauth/token` +
  GET `/tailnet/-/devices` 以外通信しないことを unit test が機械検査。
  生応答を envelope ごと devices.json へ (verify#3 用に notes に field 語義を同梱)、
  整形表を devices.md へ (期限昇順、node01?/cluster-proxy/autopilot の印付き)。
  credential 変数は TAILSCALE_API_KEY / TAILSCALE_OAUTH_CLIENT_ID+_SECRET /
  TAILSCALE_AGENT_CLIENT_ID+_SECRET を受付
- unit test 5 件 (test_fetch_devices.py、実ローカル HTTP サーバ方式 P-0107 流儀) 全 green 実測
- issue #56 に実測依頼を投稿済み:
  https://github.com/hikuohiku/homelab/issues/56#issuecomment-5384140771
  (人間に fetch_devices.py の実行 + 表の貼り付けを依頼。curl だけの代替手順も添えた)

**verify 現状 (自分で実測)**: #1 failing (FileNotFoundError) / **#2 GREEN (rc=0)** /
#3 failing (ファイル未存在)。#1/#3 は実データが必要 — **捏造せず failing のまま残す**。

**分かったこと / 罠**:

- **Tailscale OAuth client は原理的に失効しない (revoke されるまで有効)。**
  日数カウントが必要なのは非 tagged ノードキー (既定 180 日) の方が正確だった。
  spec why の「OAuth client の寿命」は「revoke・秘密鍵喪失 (secret は再表示不可)・Doppler 消滅」
  という別種のリスクへ読み替えて台本に反映済み
- tagged デバイス (operator/proxy 群) は鍵失効既定無効 (2022-03 から) →
  cluster 側の単一障害点はノードキーではなく OAuth client。「静かな死」は
  「動いている proxy は restart まで生き続ける」性質そのもの
- operator 用 OAuth client の必須スコープは Services / Devices Core / Auth Keys の Read+Write
  + tag:k8s-operator (公式 install 手順)。差し替え後は rollout restart、だめなら chart 再 sync
  (= 公式の「完全再インストール」相当) まで昇格
- unittest の markdown 行パースで最初のデータ行を落とすバグを作った (ヘッダ除外の
  インデックスずれ)。テーブル行の選別は「第 1 セルが数字」で判定するのが安全
- `/tmp/opencode` は今回も書けない (P-0107 実測の再演)。mktemp 推奨どおり

**次への引き継ぎ (次のセッションのあなたはここから)**:

1. issue #56 の返信を拾う (#56#issuecomment-5384140771 への人間の返事)。
   返っていたら `devices.md` の表か JSON を原本として
   `ops/projects/logs/P-0144/devices.json` を復元し verify#1/#3 を green 化する。
   復元形式: `{schema:"p-0144.devices/1", fetched_at:<人間が実行した時刻。不明なら取得日+
   "transcribed from issue #56" と明記>, source, tailnet, notes, devices:[<生応答>]}`
   — fetch_devices.py の build_envelope() が作る形と同じ
2. devices.json 復元後、DoD(2) の表完成: fetch_devices.py の render_table 相当で
   devices.md を生成し、印 (node01?/cluster-proxy/autopilot) の妥当性を目視で確認。
   **node01 名義デバイスの有無で docs/tailscale-recovery.md ケース3の記述を確定させる**
   (現状は「repo 上は tailscaled 無し」という事実ベースの書き分けになっている)
3. 返っていない場合は「PR merge 待ち」として待機でもよいが、CHARTER §6 どおり
   依頼が実際に issue 上に残っているかだけ確認すること (投稿済みなので消れてなければ OK)

**発見 (スコープ外。curriculum が拾う用)**:

- headless worker セッションには `.mcp.json` の MCP サーバーが接続されない
  (opencode.json には未定義)。「MCP 前提の spec」は initializer の段階で headless 実行可否を
  確認しないと、毎回同じ壁に当たって初回セッションが環境調査で終わる
  (P-0011 の ArgoCD、本件の Tailscale と同型が継続)
- tailnet 到達性の常設監視をするなら「API read ができる場所での定期実行」が必要。
  その場所は現状人間マシンのみ。runner secret への read-only client 配線は credential 判断なので
  人間領域 (CHARTER)。devices.json の鮮度 (expires は再認証で伸びる) の問題もあり、
  監視常設化は別論点として起票価値あり
