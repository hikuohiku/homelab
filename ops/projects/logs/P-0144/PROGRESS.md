# P-0144 — PROGRESS

## セッション記録

### worker #2 (2026-08-23) — 人間の返信はまだ無し。返信到着後の復元作業を機械化する復元モード (--from-md / --from-json) を fetch_devices.py に新設 (unit test 12 件全 green)

**やったこと**:

- issue #56 を確認: 依頼コメント (5384140771、2026-08-23T04:09:12Z) が **最終コメントのまま = 人間の返信はまだ無い**。
  オープン PR も P-0118 の 1 本のみで、実測データが別経路 (PR/ブランチ) で届いていないことも確認
  (`git fetch origin --prune` 済み、project/p-0144 に動き無し)。CHARTER §6 の確認事項
  「依頼が issue 上に残っているか」は OK。verify#1/#3 は実データ待ちのまま変化なし (**捏造しない**)
- フレッシュ起動で環境を再実測: TAILSCALE_* env 無し / doppler・direnv・sops バイナリ無し。
  セッション1 実測と変わらず、worker サンドボックスから tailnet の直接実測は不可能
- **fetch_devices.py に復元モードを新設**: `--from-md TABLE.md` (render_table 形式の表から復元) /
  `--from-json DATA.json` (API 生応答または devices.json 全体から復元) / `--fetched-at TS`
  (人間が実行時刻を報告してきたときに記録)。復元モードは一切通信しない。
  次セッションの復元作業が「保存 → コマンド 1 発 → verify 実行」になる
- 復元モードの unit test 7 件追加 (合計 12 件全 green 実測)。機械検査している内容:
  - render_table 出力からの往復性 (parse → render で同一表)
  - **(不明) セルはキーごと省略 = 捏造しない** (mystery デバイスが `name` + `keyExpiryDisabled` しか持たないこと)
  - code fence で囲まれた貼り付けでも読める
  - 復元モード中は API_BASE を接続必失敗ポートに向けても死なない = **通信しない**
  - `--from-json` で既存 envelope を渡したとき schema/fetched_at/tailnet/notes/devices を**上書きしない**
    (source への追記と transcription ノートの追加のみ)

**verify 現状 (自分で実測)**: #1 failing (FileNotFoundError、実データ待ち) / **#2 GREEN** /
#3 failing (同上)。変化なし。

**分かったこと / 罠**:

- **GitHub issues comments API は per_page=100 でも全件取れない**: #56 は 178 件あり、
  page パラメータでのページネーションが必須。依頼コメントは page 2 の末尾にあった。
  「返信が無い」と誤判定するリスクがあるので次回以降も全ページ取得すること
  (#56 過去ログの run #5/#8 コメント取りこぼしと同型の罠)
- tagged デバイスの expires はゼロ値 (`0001-01-01T00:00:00Z`) で返ることがある。
  sort_key は keyExpiryDisabled 優先なので表の順序は崩れないが、生 JSON を目で読むときに混乱しないこと
- user/tags セルの逆分割は "tag:" 接頭辞で判定 (user email にカンマは出ない前提。実測で壊れたら parse_markdown_table を直す)

**次への引き継ぎ (次のセッションのあなたはここから)**:

1. issue #56 のコメントを**全ページ取得** (`?per_page=100&page=1..N`) して依頼への返信を探す。
   返っていたら:
   - 表 (devices.md 形式) なら: mktemp ファイルに保存 →
     `python3 ops/projects/logs/P-0144/fetch_devices.py --from-md <file> -o ops/projects/logs/P-0144/devices.json`
     (人間が実行時刻を書いていたら `--fetched-at <時刻>` を足す)
   - JSON (curl 出力や devices.json 中身) なら: 同じく `--from-json <file>`
   - 生成物を確認して verify#1/#3 を自分で実行 → green 化して commit
2. devices.json 復元後は前セッションからの引き継ぎそのまま: node01 名義デバイスの有無で
   docs/tailscale-recovery.md ケース3の記述を確定させる
3. 返っていない場合: 待機でよいが、依頼が issue 上に残っているかだけ再確認
   (今回は 5384140771 が残置確認済み。消えていたら再投稿)

**発見 (スコープ外。curriculum が拾う用)**:

- (セッション1 からの繰り越し 2 件: headless worker に MCP 未接続 / tailnet 監視の常設化は別論点。
  新規の発見は無し)

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
