# P-0144 — PROGRESS

## セッション記録

### worker #6 (2026-08-23) — 返信なしの 6 セッション目。「依頼以降の全コメント走査」を再適用、人間の不動も p-0143 のブランチ進行から裏取り。待ち以外なし

**やったこと**:

- **依頼コメント 5384140771 以降のコメントを全ページ走査** (`?per_page=100&page=1..2`、
  空バッチで終端): 取得できたのは **179 件**、依頼以降は **1 件のみ = P-0143 worker の収集依頼
  自己投稿 (5384240492)** でセッション4〜5 実測と同一。本プロジェクトへの返信はゼロ。
  返信判定材料 (投稿者 hikuohiku + 04:09:12Z 以降 + 表 / keyExpiryDisabled / expires) に
  合致するコメントは無しを実測
- 依頼コメント本体を再確認: **残置・未編集** (`created_at == updated_at` 実測、
  fetch_devices.py 手順と curl フォールバック両方が本文に残存) → CHARTER §6 の確認事項 OK
- 別経路の再実測: `git fetch --prune` + `git ls-remote` で `project/p-0144` は 23761774
  (= 本セッション checkout と同一)、main は分岐点 (8c5cbd7d) のまま。open PR は #512 (P-0118) のみ。
  **p-0143 ブランチが動いたが commit 者は homelab-autopilot 自身 (セッション6〜8 の記録) で、
  人間の動きではないことを commit author で裏取り** — 実測データがどの経路でも届いていない
- サンドボックス credential 不在を 6 セッション目として再実測: env の TAILSCALE_* は 0 件、
  tailscale / doppler / direnv / sops / just バイナリ無し、checkout に `.envrc` 無し
  (kubectl バイナリは存在するが `~/.kube` 無しのため使えない)。自力実測の道は引き続き閉じている
- unit test 12 件を再実測 → **全 green** (カレントは ops/projects/logs/P-0144/)。
  受入 verify を自分でも再実行: #1 rc=1 / #2 rc=0 / #3 rc=2 — **変化なし**

**verify 現状 (自分で実測)**: #1 failing (FileNotFoundError、実データ待ち) / **#2 GREEN (rc=0)** /
#3 failing (同上)。変化なし。

**分かったこと / 罠**:

- 「他ブランチが動いた = 人間が来た」とは限らない: 今回 p-0143 が 4 コミット進んでいたが
  すべて autopilot 自身の待ち記録だった。別経路確認では **commit author の確認まで入れると
  誤判定しない** (返信判定と同様に「誰が書いたか」を見るのが安全)
- 待ち状態の経過観察は本セッションでも 10 分足らず。走査方式 (page 1..N、空バッチ break) の
  安定性が 2 セッション連続で確認できた

**次への引き継ぎ (次のセッションのあなたはここから)**:

1. やることは変更なし: ① 依頼コメント 5384140771 以降の全コメント走査 → 返信があれば
   mktemp 保存 → `--from-md` / `--from-json` (+ `--fetched-at` あれば) → 復元 →
   verify#1/#3 実行。② 復元後は devices.md の印を目視確認し、node01 名義デバイスの有無で
   docs/tailscale-recovery.md ケース3を確定させる。③ 返っていなければ「依頼残置確認 +
   credential 不在再実測 + unit test 再実行 + PROGRESS 記録」で十分
2. 待ちが続く場合でも捏造・先走りの green 化はしない (P-0143 セッション5 との共通方針)。
   復元手順の詳細はセッション3 の往復リハーサル記録とセッション2 の引き継ぎ参照

**発見 (スコープ外。curriculum が拾う用)**:

- (新規なし。繰り越し: headless worker に MCP 未接続 / tailnet 監視の常設化は別論点 /
  #56 の横断依頼窓口化に伴う返信差分照合機構の欠如)

### worker #5 (2026-08-23) — 返信なしの 5 セッション目。「依頼以降の全コメント走査」を初めて実行し P-0143 依頼のみを再確認。別経路 (ブランチ / PR / main) も不動で、待ち以外なし

**やったこと**:

- **依頼コメント 5384140771 以降のコメントを全ページ走査して返信を探す** (セッション4 引き継ぎ
  の判定方法を初適用): `?per_page=100&page=1..2` で取得できたのは **179 件**、依頼以降は
  **1 件のみ = P-0143 worker の収集依頼自己投稿 (5384240492)** で変化なし。本プロジェクトへの
  返信はゼロ。返信判定材料 (投稿者 hikuohiku + 04:09:12Z 以降 + 表 / keyExpiryDisabled / expires)
  に合致するコメントは無しを実測
- 依頼コメント本体も再確認: **残置・未編集** (`created_at == updated_at` 実測、
  fetch_devices.py 手順と curl フォールバックの両方が本文に残存) → CHARTER §6 の確認事項 OK
- 別経路の再実測: `git ls-remote` で `project/p-0144` は 47151167 (= 本セッション checkout と同一 =
  外部からは不動)、main は分岐点 (8c5cbd7d) のまま。open PR は #512 (P-0118) のみ。
  実測データが PR / ブランチ / main 経由で届いていないことを確認
- サンドボックス credential 不在を 5 セッション目として再実測: env の TAILSCALE_* は 0 件、
  tailscale / doppler / direnv / sops / just バイナリ無し、checkout に `.envrc` 無し。
  自力実測の道は引き続き閉じている
- unit test 12 件を再実測 → **全 green** (`python3 -m unittest test_fetch_devices` は
  ops/projects/logs/P-0144/ をカレントにする必要あり。ルートからだと import error になるだけなので
  失敗と誤認しないこと)。受入 verify を自分でも再実行: #1 rc=1 / #2 rc=0 / #3 rc=2 — **変化なし**

**verify 現状 (自分で実測)**: #1 failing (FileNotFoundError、実データ待ち) / **#2 GREEN (rc=0)** /
#3 failing (同上)。変化なし。

**分かったこと / 罠**:

- GitHub API の comment JSON には `edited_at` キーが常在しない (未編集だと欠ける)。
  「編集されたか」の判定は `created_at == updated_at` で行うのが安全 (本セッションで KeyError 実測)
- 待ち状態の経過観察に必要な作業は本セッションで 10 分足らず。「依頼以降の走査」方式なら
  最終コメント比較より安全かつ同程度に安価なので、次セッション以降もこれでよい

**次への引き継ぎ (次のセッションのあなたはここから)**:

1. やることはセッション4 引き継ぎのまま変更なし:
   ① 依頼コメント 5384140771 以降の全コメント走査 (`?per_page=100&page=1..N`、空バッチで break) →
   返信があれば mktemp 保存 → `--from-md` / `--from-json` (+ `--fetched-at` あれば) → 復元 →
   verify#1/#3 実行。② 復元後は devices.md の印を目視確認し、node01 名義デバイスの有無で
   docs/tailscale-recovery.md ケース3を確定させる。③ 返っていなければ「依頼残置確認 +
   credential 不在再実測 + unit test 再実行 + PROGRESS 記録」で十分 (scope 拡大しない)
2. 復元時の注意 (セッション3 実測の繰り越し): コメント本文は fence 有無どちらでも
   `--from-md` にそのまま渡せる。devices.json への出力先指定を忘れないこと
   (`-o ops/projects/logs/P-0144/devices.json`)

**発見 (スコープ外。curriculum が拾う用)**:

- (新規なし。セッション1〜4 の繰り越し: headless worker に MCP 未接続 /
  tailnet 監視の常設化は別論点 / #56 が横断依頼窓口化しつつあり返信判定の差分照合機構が欲しい)

### worker #4 (2026-08-23) — 返信なしの 4 セッション目。依頼コメント以降に増えたのは P-0143 の依頼のみ = 「#56 の末尾に他プロジェクトの依頼が積まれる」状態を初実測。待ち以外にやることは無しを全経路で確認

**やったこと**:

- issue #56 を全ページ取得して再確認: 総コメント **179 件** (page=1..2)、依頼コメント
  (5384140771) 以降は **1 件のみ = P-0143 worker の収集依頼自己投稿 (5384240492、04:37:45Z)** で、
  本プロジェクトへの返信はゼロ。依頼本文も未編集 (`updated_at == created_at` 実測、
  fetch_devices.py / curl 手順は残存) → CHARTER §6 の確認事項 OK
- 別経路の実測: `git ls-remote` で `project/p-0144` は 5da2a3a0 のまま不動。
  動いたのは ops-state / p-0116 / p-0139 / p-0143 / p-0145 (+新設 p-0147) だが
  いずれも本件と無関係。open PR は #512 (P-0118) のみ。main は分岐点 (8c5cbd7d) から不動 =
  実測データが merge 経路で届いていないことも確認
- サンドボックスの credential 不在を 4 セッション目として再実測: チェックアウトに `.envrc` 無し、
  env は `AUTOPILOT_GITHUB_TOKEN` / `GITHUB_REPO` のみ (TAILSCALE_* / DOPPLER_* 無し)。
  自力実測の道は引き続き閉じている
- unit test 12 件を再実測 → 全 green (mktemp のみ使用、リポジトリへの混入なし)。
  受入 verify を自分でも再実行: #1 rc=1 / #2 rc=0 / #3 rc=2 — **変化なし**

**verify 現状 (自分で実測)**: #1 failing (FileNotFoundError、実データ待ち) / **#2 GREEN (rc=0)** /
#3 failing (同上)。変化なし。

**分かったこと / 罠**:

- **#56 の末尾は複数プロジェクトの依頼が積み重なる FIFO になる** (P-0143 の依頼が本件の直後に
  到着済み)。次セッションは「最終コメントが自分の依頼か」ではなく「**依頼コメントより後の全コメントを
  走査して返信を探す**」こと。返信の判定材料: 投稿者が hikuohiku かつ 04:09:12Z 以降、本文に
  表 (先頭セルが数字のパイプ区切り) または `keyExpiryDisabled` / `expires` を含む
- コメント総数の数え方が揺れている (セッション3 の記録は「全 196 コメント」とあるが本日実測は 179)。
  ページング終端の判定 (空バッチで break) は本日の方が素直なので、今後は「取得できた件数」と
  「依頼以降の件数」だけを記録し総数の断言をしないのが安全
- 待ちセッションの正当性 precedents: P-0143 セッション5 も同一構造 (credential 不在 + 返信待ち) で
  「待ちは正当な状態として記録だけ残す」を commit している。本件もこれに倣い、
  **捏造・先走りの green 化はしない**

**次への引き継ぎ (次のセッションのあなたはここから)**:

1. 依頼コメント 5384140771 より後のコメントを全ページ走査 (手順はセッション3 引き継ぎどおり:
   `?per_page=100&page=1..N`)。返信があれば mktemp 保存 → `--from-md` / `--from-json`
   (人間が実行時刻を書いていたら `--fetched-at`) → 復元 → verify#1/#3 実行。
   リハーサル済みなので迷いどころはない
2. 復元後: devices.md の印を目視確認し、node01 名義デバイスの有無で docs/tailscale-recovery.md
   ケース3を確定させる (セッション2 以降の継続事項)
3. 返っていない場合: 本セッションと同じく「依頼の残置確認 + credential 不在再実測 +
   unit test 再実行 + PROGRESS 記録」で十分。それ以上の工作は scope 拡大なのでしない

**発見 (スコープ外。curriculum が拾う用)**:

- (繰り越し分に加えて新規なし。強いて挙げれば #56 がプロジェクト横断の依頼窓口として
  機能し始めたことで、「返信の有無」判定が単純な最終コメント比較では不可能になりつつある。
  各プロジェクトの runner が依頼 ID を state に持って差分照合する仕組みがあると
  同型の待ちプロジェクトが量産されても耐えられる — 起票価値は curriculum 判断に委ねる)

### worker #3 (2026-08-23) — 人間の返信はまだ無し (再確認)。復元経路を実 CLI で往復リハーサルし、データ到着時に「コマンド 1 発 + verify 2 項目 PASS」まで済むことを事前実証

**やったこと**:

- issue #56 を再確認: 依頼コメント (5384140771、2026-08-23T04:09:12Z) が **全 196 コメント中の最終のまま = 返信はまだ無い**
  (GitHub API の全ページ取得で実測。CHARTER §6 の「依頼が issue 上に残っているか」も OK、本文は編集・最小化されていない)。
  オープン PR も P-0118 のみで、実測データが別経路で届いていないことを確認。verify#1/#3 変化なし (**捏造しない**)
- unit test 12 件をフレッシュ環境で再実測 → 全 green (セッション2 実測の再演)
- **復元経路の往復リハーサルを実施**: render_table() が作る形式のサンプル表を mktemp に作り
  (node01 名義 / k8s- tag 付き disabled / 期限近い非 tagged の 3 台)、実 CLI の
  `--from-md` で復元 → 復元ファイルに対して受入 verify#1/#3 と同一ロジックを実行し **両方 PASS を実測**。
  印 (node01? / cluster-proxy) とソート (期限昇順・disabled/不明は末尾) も目視確認。
  サンプルはリポジトリ外のみで消滅、**リポジトリへの捏造混入なし** (`git status` clean 実測)

**verify 現状 (自分で実測)**: #1 failing (FileNotFoundError、実データ待ち) / **#2 GREEN** /
#3 failing (同上)。変化なし。

**分かったこと / 罠**:

- **verify#3 の grep は envelope の `notes` に乗った語句で命中している**:
  FIELD_NOTES の「expiry カウントの対象外」という説明文の小文字 `expiry`。
  つまり devices.json の実データに `key_expiry` というキーは最初から存在しない (実キーは camelCase の `keyExpiryDisabled`)。
  将来 envelope の notes を削る・書き換えると、**実データがあっても verify#3 だけ落ちる**。
  復元経路 (--from-md/--from-json) はどちらも notes に FIELD_NOTES を足す実装なので今は安全 (リハーサルで実測済み)
- 表の貼り付けは code fence 無しでも復元できる: GitHub コメント本文はパイプ表をそのまま保持し、
  parse_markdown_table は行頭の空白を行ごとに strip してから判定するため。次セッションは
  コメント本文を保存してそのまま `--from-md` に渡せばよい (fence ありでも既存 unit test どおり読める)
- リハーサルで 1 台も `node01` 名義がない構成でも印ロジックは破綻しないことを目視確認
  (PROJECT.md 前提どおりなら node01 デバイスは実在しない可能性が高い。台本ケース3は現状の
  事実ベース記述のままで確定させず、実データ到着時に判断 = セッション2 引き継ぎのまま)

**次への引き継ぎ (次のセッションのあなたはここから)**:

1. 引き継ぎはセッション2 のものをそのまま引き継ぐ (変更なし):
   issue #56 の返信を確認 → 届いていればコメント本文 (表なら `--from-md`、JSON なら fence 内を
   抜いて `--from-json`) で復元 → verify#1/#3 を実測 green 化。
   **リハーサル済みなので迷う箇所はないはず。** fetched_at は人間の報告があれば `--fetched-at` へ
2. 復元後は DoD(2): devices.md の印を目視で妥当性確認し、node01 名義デバイスの有無で
   docs/tailscale-recovery.md ケース3の記述を確定させる (セッション2 引き継ぎ 2 のまま)
3. 返っていない場合は待機でよいが、依頼コメント 5384140771 が最終コメントであり続けているかだけ
   API で確認すること (本セッションの方法: `?per_page=100&page=2` で末尾ページ取得。page=1 は
   100 件超のため依頼コメントが載らない罠がある)

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
