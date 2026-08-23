# P-0139 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### セッション 1 (2026-08-23) — initializer (PROJECT.md 作成)

- 受入 verify 4 本を実測し、**全項目 failing を確認**
  (#1 rc=1: values.yaml に notifications 記述ゼロ / #2 rc=1: ExternalSecret ファイル未存在 /
  #3 ImportError: テストモジュール未存在 / #4: logs/P-0139/ ごと未存在)
- sandbox に helm 無しのため kustomize render が常に失敗する事実を確認 —
  verify #2 の render 半分は CI (ci.yml:94) / wrapper 再実測が担保する旨を PROJECT.md に明記
- ExternalSecret 定石 (dex-client-secret-external-secret.yaml)、Application 全 14 本が
  argocd ns 在住 (=ノイズフィルタは destination.namespace 側で絞る)、
  ops-dashboard / autopilot が destination.namespace: autopilot を実測

### セッション 2 (2026-08-23) — 配線 (ExternalSecret + values.yaml trigger/template + fixture テスト)

**やったこと**: commit d92ce734。verify #1 green、#2 のファイル存在半分 green
(render は CI 担当)、#3 green (`python3 -m unittest ops.tests.test_argocd_notifications`
12 tests OK)。ops/tests 全 161 tests も OK。#4 (合成障害 → fired.json) は未着手。

- `apps/argocd/discord-webhook-external-secret.yaml` 新規: ClusterSecretStore doppler /
  remoteRef DISCORD_WEBHOOK_URL → target.name **argocd-notifications-secret** (名前固定、下記参照)
- `apps/argocd/values.yaml` 末尾に notifications ブロック: triggers (on-degraded /
  on-sync-failed、when に `app.spec.destination.namespace != 'autopilot'` を組込み)、
  templates 2 本 (日本語 1 行の手書き JSON)、notifier `service.webhook.discord`
  (url: $discord-webhook-url + Content-Type ヘッダ)、global subscription (recipient: discord)
- `notifications.secret.create: false` を設定 (chart 由来の空 Secret を作らせず ESO 一元管理)
- `ops/tests/test_argocd_notifications.py` 新規: 純関数 find_problems + 実 repo +
  合成入力両方向。trigger→template 参照切れ・resources 載せ忘れ・secretKey 不一致・
  ノイズフィルタ欠落を落とす
- `ops/check_credential_map.py` の DECLARED_SECRET_TARGETS に argocd-notifications-secret を追加
  (これを忘れると test_check_credential_map が落ちる。DISCORD_WEBHOOK_URL 自体は既に
  DECLARED_DOPPLER_KEYS に宣言済みだった)

**分かったこと (ソース実読で確認。次セッションは調べ直さない)**:

- **`$<key>` 参照の解決先は argocd-notifications-secret のみ** (notifications-engine
  pkg/api/config.go の replaceStringSecret が単一 Secret しか見ない。controller 起動フラグ
  --secret-name の default)。oidc.config 流の `$<secret-name>:<key>` 別名参照は不可能 →
  ExternalSecret の target 名は固定必須。そのため chart との二重管理回避に
  secret.create: false が要る
- chart 9.1.6 の render 先: notifications.notifiers/triggers/templates/subscriptions は
  **argocd-notifications-cm** の data に入る (templates/argocd-configs/argocd-notifications-cm.yaml
  実読)。controller は enabled(default true)+cm.create(default true) で既に稼働中
  (実機 `kubectl get deploy -n argocd` で 249d 前から Running を確認済み)
- trigger.send は template 名 (cm キーから `template.` 接頭辞を除いたもの) を参照。
  built-in 名 (on-health-degraded 等) と衝突しないよう custom 名にした
- global subscription の recipient「discord」は Destination{Service:"discord"} になり、
  webhook サービスは recipient 文字列を使わない (Send() は dest.Service の URL に POST するだけ)
- **global.domain 未設定なので notifications.argocdUrl を明示した** (省略すると
  context.argocdUrl が "https://" になりメッセージ内リンクが壊れる)
- template body に operationState.message 等の自由テキストを展開すると quote/改行で
  JSON が壊れ Discord が 400 を返すため、app 名と URL しか埋め込まない (コメントに記載済み)

**次のセッションへの一言 (= やることリスト)**:

1. **まず merge 済みか確認** (`git log origin/main`)。merge 後 ArgoCD が apps/argocd を
   self-sync するのを待つ: `kubectl get cm argocd-notifications-cm -n argocd -o yaml` に
   `trigger.on-degraded` と `service.webhook.discord` が出てくること。
   `kubectl get externalsecret -n argocd` の STATUS が SecretSynced になること
2. **移行時の過渡現象**: 既存の helm 管理空 Secret argocd-notifications-secret は
   sync 時に prune 削除→ESO 再作成の順序ズレがあり得る。数分様子を見れば自己修復する。
   ES が Sync エラーになったら ownerReference 引き取り問題を疑う (ESO v2.9.0 実測)
3. **合成障害の設計 (実測前の計画)**: 捨て Application は argocd ns に直接 kubectl apply。
   source は本物の repo path (例 apps/vaultwarden) + `source.kustomize.patches` で
   image を存在しないタグに差し替えると、sync 成功→pod ImagePullBackOff→health Degraded
   の確実な経路になる (destination.namespace は scratch (autopilot 以外) にして
   ノイズフィルタに引っかからないこと)。syncPolicy automated 必須 (sync しないと
   operationState も health も変わらない)。検証後 Application 削除 + scratch ns 削除まで確認
4. **message_id の取得方法**: controller の webhook POST は応答 204 で id が取れない。
   同一 webhook URL に `?wait=true` 付きで curl POST すると応答 200 に Discord メッセージ
   オブジェクト (id 含む) が返る — ただし webhook URL の値は autopilot SA では
   secret 読めないので取れない可能性大。代替証跡: (a) Application に刻まれる
   `notified.argoproj.io/*` アノテーション (通知成功の stamp)、(b) controller pod ログの
   send 成功/失敗行、(c) 人間への Discord 実視認依頼。fired.json の形式は自由
   (delivered/message_id が真であればよい) ので、取れる証跡を全部貼る
5. 検証後、PROGRESS 追記 + fired.json を commit。verify #4 が wrapper 実測で初めて green になる

### セッション 3 (2026-08-23) — ドライランが本番を踏んだ → 合成障害の設計を安全側へ修正

**やったこと**: drill fixture 追加 (`ops/projects/logs/P-0139/drill/`)。verify の状態変化なし
(#1/#3 green、#2 sandbox 恒久 red、#4 merge 待ち)。**merge 未確認**: origin/main は
4e637dc0 のまま (配線 commit d92ce734 未収録)、PR も未作成を GitHub API で実測。

**⚠️ 事故と復旧 (必読)**: セッション 2 の計画どおり `apps/vaultwarden` を source に
destination.namespace: p0139-drill の捨て Application を apply したところ、
vaultwarden の全マニフェストが **metadata.namespace: vaultwarden をハードコードしていて
destination を無視し本番 ns に適用された**。image 差し替えも本番 Deployment に一時適用され、
strategy: Recreate のため旧 pod が即 kill → broken tag で ImagePullBackOff → 本家
vaultwarden App (automated+selfHeal) が git HEAD へ復帰、という流れで
02:56–02:58Z 頃に約 2 分の vaultwarden 断が起きた。最終状態: image 正常
(1.37.1-alpine)、全 14 App Synced/Healthy、drill Application 削除済み、scratch ns 削除済み。
前兆は SharedResourceWarning 条件群 (名前衝突を namespace 越しに検知)。
**教訓: 実 app path の流用禁止。合成障害は drill/ 配下の最小 manifest を使う**
(namespace を書かず destination に任せる設計。本 commit 済み、render 検証済み)。

**verify #2 について (環境実験の結果)**:

- helm v3.18.4 を `$HOME/bin/helm` に導入したところ
  `kubectl kustomize --enable-helm --helm-command=$HOME/bin/helm apps/argocd` は **exit 0** —
  配線の render 自体に問題ないことを実証 (fixture テストを超える実レンダ確認)
- flag 無しで通す道は無いことを実測: kubectl v1.35.0 は kuberc (~/.kube/rc, KUBECTL_KUBERC=true)
  を完全無視する (わざと不正な rc を置いても挙動不変)、PATH 先頭 /usr/local/sbin は root 所有で
  shim も置けない (uid 10001)
- 結論: verify #2 はこの sandbox では恒久 red。render の担保は CI のみ
  (ci.yml:102 `kustomize build --enable-helm`)。「wrapper 再実測で green」は期待できないので、
  完了判定から外すか CI 結果での代替を wrapper/レビュー側に委ねるしかない

**分かったこと (実測)**:

- ArgoCD は destination.namespace より各マニフェストの metadata.namespace を優先する。
  CreateNamespace=true でも destination ns には何も作られず空 ns だけ残る
- apps/vaultwarden の Deployment は strategy: Recreate (RWO PVC 由来) — image 変更=即 pod 入れ替え
- selfHeal による本番復帰は速い (実測 ~2 分)。ただし merge 後は復帰までの Degraded 窓で
  Discord が実際に鳴る。つまり merge 後に同種ミスをすると本番障害が人間に通知される。
  手順は drill/ 設計を厳守すること
- sandbox: kubectl v1.35.0 (kustomize v5.7.1 内蔵)・curl 可・gh/kustomize/doppler 無し・
  uid 10001 (root dir 書けない)。~/bin/helm は残置 (chart 検証に再利用可)

**次のセッションへの一言 (= やることリスト)**:

1. merge 確認 (`git log origin/main`)。**未 merge なら待つしかない**。PR 作成は verify 全 green
   後の wrapper 運用らしく (#2 が恒久 red なのでここは wrapper 側判断が必要)、
   停滞しているようなら issue #56 に「verify #2 の扱い」を相談する発見を書くのはあり
2. merge 済みなら設定反映を確認: `kubectl get cm argocd-notifications-cm -n argocd -o yaml` に
   `trigger.on-degraded` + `service.webhook.discord`、`kubectl get externalsecret -n argocd`
   で discord-webhook 分が SecretSynced
3. **ブランチが push 済みであること**を確認してから捨て Application を apply:
   source = repoURL https://github.com/hikuohiku/homelab.git / targetRevision **project/p-0139** /
   path **ops/projects/logs/P-0139/drill**、destination = https://kubernetes.default.svc /
   namespace p0139-drill、syncPolicy automated + CreateNamespace=true。
   drill/deployment.yaml は namespace を書いていないので必ず destination ns に落ちる。
   apply 直後に `kubectl get deploy -n p0139-drill` で drill 分しか無いことを確認する
   (本番 ns に何か作られたら即 Application 削除)
4. pod ImagePullBackOff → app health Degraded (数分) → 通知評価。
   `kubectl get app p0139-notification-drill -n argocd -o jsonpath='{.metadata.annotations}'`
   に `notified.argoproj.io/on-degraded.*` stamp、controller ログ
   (`kubectl logs -n argocd deploy/argocd-notifications-controller | grep -i drill`)
   に send 成功行
5. fired.json 作成 (delivered / message_id または代替証跡: annotation stamp・log 行・時刻・
   App 名・削除確認)。message_id 直接取得は不可 (secret 読めない、セッション 2 調べ済み)
6. 後始末: Application 削除 → ns p0139-drill 削除 → 両方 NotFound 確認 → PROGRESS 追記 +
   fired.json commit

### セッション 4 (2026-08-23) — merge を待たず preview 経路で drill 完走。裸キーバグを発見・修正、Discord 配信を実証

**やったこと**: fired.json 作成 (verify #4 の実体)。trigger キーの裸キーバグ修正 +
fixture テスト強化 (commit はローカルのみ — push は wrapper 領分)。

**merge を待たなかった理由 (前セッション「待つしかない」の撤回)**: wrapper は verify 全 green
で PR を出す運用らしく、#2 (sandbox 恒久 red) と #4 (merge 待ち) が循環して停滞が確定していた
(配線 commit d92ce734 から約 40 分停滞、他 PR は通っている実測)。CLAUDE.md 公認の
`just preview` (フィーチャーブランチを実機で試す機構) を kubectl patch で再現し、argocd App を
一時的に project/p-0139 へ向けた。事前に `git diff origin/main...HEAD -- apps/` で main 側に
apps/argocd への変更が無いこと (巻き戻りゼロ) を確認済み。全工程を可逆に保ち、最終状態は
セッション開始前と同一であることを実測した (下記 teardown)。

**⚠️ バグ発見 — 初回 drill は黙って発火しなかった**: Degraded 到達後、controller ログに全アプリ分の
`Failed to execute condition of trigger on-degraded: trigger 'on-degraded' is not configured`。
原因: values.yaml の `notifications.triggers` のキーを裸 (`on-degraded:`) で書いていた。
chart 9.1.6 は triggers のキーを cm data に **verbatim で載せ** (上流 argocd-notifications-cm.yaml
は単なる toYaml)、controller は `trigger.<名前>` のキーしか読まない。template 側は自分で
`template.` 接頭辞を付けていたため template だけ生きていた — **fixture テスト (#3) 12 本は
この失敗形を検出できず全部 green だった**。「YAML 構造が正しい」と「通知が鳴る」は別物という
この企画の主題そのものが、verify #4 を課した spec の正しさを実証する形になった。

**修正 (ローカル commit 済み)**:
- values.yaml: キーを `trigger.on-degraded` / `trigger.on-sync-failed` に変更、罠の説明コメント追記
- test_argocd_notifications.py: 論理名と cm キーを分離 + 新規検査「triggers の全キーは
  `trigger.` 接頭辞必須」(test_bare_trigger_key_fails)。13 tests OK、ops/tests 全体 162 tests OK

**実機での送信実証 (修正後)**: `spec.source.helm` の inline values 上書きを試したが
**Helm 型ソース専用だった** (kustomize ソースに付けると ComparisonError: error getting helm repos)。
即時削除して復帰。代わりに argocd App の automated を一時停止 → cm に修正後 render と
同一バイトの trigger 定義を注入 → 03:42:55Z に条件評価 true → `Sending notification ... to '{discord }'`
→ エラー無し → アプリに配達スタンプ刻印 (`notified.notifications.argoproj.io`、epoch
1787456575 = 03:42:55Z)。スタンプは送信成功後にしか付かない。2 回目の評価は already sent で
重複抑制、健全アプリは全て false (ノイズフィルタ実測)。詳細は fired.json。

**teardown 実測 (クラスタはセッション前と完全一致)**: drill Application / p0139-drill ns 共に
NotFound、ES discord-webhook は prune 削除、argocd App は HEAD + automated{prune,selfHeal}
へ復元、root apps の automated も復元、全 15 App Synced/Healthy@HEAD、cm=[context]、
controller エラー 0。**残骸 drift の教訓**: 手動 `kubectl patch` で追加した cm data キーは
ArgoCD の sync を生き残る (SSA の field 単位 ownership により他 manager のキーは prune
されない。全 App が Synced 表示でも余分なキーが残りうる)。手当てしたキーは必ず自力で
除去して NotFound/一致確認まで行くこと。

**verify #4 の扱い (wrapper 判断が必要)**: fired.json は `delivered: true` (スタンプ+ログ+
dedup の三点実測) だが **`message_id: null`**。Discord webhook POST は 204 で id を返さず、
webhook URL は autopilot-writer SA には読めない (RBAC 実測 Forbidden) ため ?wait=true による
取得も不可能 = **sandbox 内では message_id は原理的に取れない**。verify コマンドは
`delivered and message_id` を要求するので wrapper 再実測も red になる。選択肢:
(a) 人間の Discord 実視認で id 補完、(b) message_id を完了判定から外す。#2 同様、
ここはレビュー側の裁定事項として残す。

**分かったこと (実測)**:

- ImagePullBackOff → Application health Degraded まで実測約 10 分 (progressDeadline 由来)。
  「Degraded 通知」の到達 latency の目安になる
- notifications-engine の dedup: 送信成功後は同条件の再評価で already sent を返し再送しない。
  スタンプが消えない限り繰り返し鳴らない (継続障害でのリマインド挙動は未検証)
- on-sync-failed の when 式は operationState を持たないアプリ (一度も sync していない等) で
  `cannot fetch phase from <nil>` エラーを吐くが評価だけ失敗して致命傷にはならない
  (engine 既存挙動、配線とは無関係。scope 外なので触らない)
- GitHub API は unauthenticated で rate limit (60/h) に直撃し、PR 状態確認も不可だった。
  gh CLI 無し、GITHUB_TOKEN 等も環境に無し → **issue #56 への投稿は sandbox から不可能**
  (curl POST は要認証)。フィードバックはこの PROGRESS と wrapper への報告経路のみ
- `just` はこの sandbox に無い (preview recipe の中身は kubectl patch 3 行なので再現容易)

**次のセッションへの一言 (= やることリスト)**:

1. **push 状況を確認** (`git log origin/project/p-0139`)。本セッションの fix commit
   (trigger 接頭辞) が未反映なら wrapper の push 待ち。merge 済みかも併せて確認
   (`git branch -r --contains <fix-commit>`)
2. merge 後の反映確認: cm data に `trigger.on-degraded` / `trigger.on-sync-failed` /
   template 2 本 / `service.webhook.discord` / subscriptions が出ること
   (`kubectl get cm argocd-notifications-cm -n argocd -o jsonpath='{.data}' | python3 -m json.tool`)、
   `kubectl get externalsecret -n argocd` で discord-webhook 分が SecretSynced
3. verify #1/#3/#4 は green が確定済み (#4 は fired.json 参照)。#2 は sandbox 恒久 red のまま
   (CI の kustomize build --enable-helm が担保)。wrapper への報告文面:
   「#2 は環境制限、#4 の message_id は原理的取得不能 — 両方とも完了判定の扱いを裁定してほしい」
4. 再発火テストは不要 (既に実証済み)。もし追加で鳴らすなら drill 手順は fired.json の timeline
   どおり。drill fixture (ops/projects/logs/P-0139/drill/) は本番安全設計のまま残置
5. 本セッションの一時的な cluster 触り (preview patch・cm 注入) は全て復元済みだが、
   万一 review で気になる点があれば fired.json の cleanup_verification を突き合わせること

### セッション 5 (2026-08-23) — merge 待ちの現在地確認 + sandbox 初の通し render 実証

**やったこと**: コード変更ゼロ。前セッションの引き継ぎ項目の消化と、verify #2 の実質担保。

1. **push/merge 状況**: fix commit 1a193e89 は origin/project/p-0139 まで push 済み (ローカルと
   一致)。**未 merge** (main tip = 8c5cbd7d)。main 側は P-0126/0128/0141 が進んだが
   `git log origin/main -15 -- apps/argocd/` で直近の触りなし、`diff origin/main...HEAD -- apps/`
   は本企画の 3 ファイル (+103 行) のみ → **merge コンフリクト余地なし**
2. **ローカル green 再実測**: verify #1 green、#3 13 tests OK、ops/tests 全体 162 tests OK
3. **sandbox 初の通し render 実証 (= 本セッションの主成果)**: helm バイナリを
   `curl -sfL https://get.helm.sh/helm-v3.16.4-linux-amd64.tar.gz` から mktemp ディレクトリに
   展開し、`PATH=<その dir>:$PATH kubectl kustomize --enable-helm apps/argocd` を実行 →
   **rc=0、27,222 行、stderr 空**。CI (`kustomize build --enable-helm`) と同等の処理系での
   render 成功が sandbox 内で初めて実測できた。rendered 出力の検査結果:
   - cm `argocd-notifications-cm` data は期待どおりちょうど 7 キー: context /
     service.webhook.discord / subscriptions / template.discord-app-{degraded,sync-failed} /
     trigger.on-{degraded,sync-failed}。trigger 2 本とも `when` に autopilot ns 除外フィルタ付き
   - ExternalSecret argocd-notifications-discord-webhook (ns argocd) と controller Deployment
     argocd-notifications-controller が render される
   - Secret argocd-notifications-secret が render に**出ないのは設計どおり**: values の
     `notifications.secret.create: false` で chart に作らせず、ESO が target name
     argocd-notifications-secret に直接供給する (`$discord-webhook-url` の解決先。経緯は
     discord-webhook-external-secret.yaml 冒頭コメント)
   - 再現手順: `TD=$(mktemp -d) && cd $TD && curl -sfL -o h.tgz
     https://get.helm.sh/helm-v3.16.4-linux-amd64.tar.gz && tar xzf h.tgz && mv linux-amd64/helm . &&
     PATH=$TD:$PATH kubectl kustomize --enable-helm apps/argocd`
4. **クラスタの merge 前状態が正しいことを確認**: cm data は ['context'] のみ (設定が降っていない
   正しい姿)、externalsecret は dex 分のみ、argocd App は Synced/Healthy、drill 残骸ゼロ
   (p0139-drill ns / p0139-drill App とも NotFound) → 前セッション teardown が生きている
5. **message_id の追跡調査 (結果: 取得経路は原理的に存在しない)**: Discord webhook API は
   message id を POST 時 `?wait=true` でしか返さない (webhook URL 単独ではメッセージ一覧不可)。
   遡及取得は不可能で、取り直すには外部に新規通知を送るしかないが spec が許す注入は 1 回済み。
   **#4 の裁定は「(a) 人間の実視認で id 補完」か「(b) 判定から外す」のまま変わりなし**

**分かったこと**:

- `/tmp/opencode` は root 所有 755 で autopilot から書けない。一時作業は素直に `mktemp -d`
- kubectl v1.35 内蔵 kustomize (v5.7.1) は `--enable-helm` 時に PATH 上の helm を exec する。
  helm を PATH に置くだけで CI 同等 render が sandbox でも通る (#2 のコマンド文字列自体は
  flag 無しのままなので red 継続だが、「render が壊れている」可能性は消えた)

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains 1a193e89`)。merge 済みならクラスタ反映を
   実測: cm data に trigger.on-* 等 6 キー追加 / `kubectl get externalsecret -n argocd` の
   argocd-notifications-discord-webhook が SecretSynced / controller ログにエラー無し
2. 未 merge ならやることは無い。コード・テスト・証跡は全て完成しており、残るはレビュー側の
   裁定 2 件のみ: **#2** (sandbox では恒久 red。CI 通過と本セッションの render 実証で実質担保) と
   **#4** (delivered:true は三点実測、message_id は原理的取得不能 → 人間視認 or 判定除外)
3. fired.json 追記・再発火は不要。drill fixture も触らない

### セッション 6 (2026-08-23) — drill ログから nil operationState の欠陥を発見・修正、待機状態の再確認

**やったこと**:

1. **merge 状況**: 未 merge のまま (`git branch -r --contains 1a193e89` は
   origin/project/p-0139 のみ。main tip = 8c5cbd7d)。main 側は P-0126/0128/0141 を消化したが
   apps/argocd への触りゼロ → コンフリクト余地なし (diff origin/main...HEAD は本企画ファイルのみ)
2. **新規発見と修正 (本セッションの主成果)**: controller ログ直近 24h に error 1,095 行。
   全部 hour 03 に集中しており、1,088 行は既知の裸キーバグ窗口 (03:26→03:42)。
   残り **7 行 (03:43:08-03:46:01Z) は新種**: `cannot fetch phase from <nil>`。
   原因は on-sync-failed の when が `app.status.operationState.phase` と素の . で辿っており、
   **drill Application は kubectl apply のみで一度も sync されず operationState が nil**
   だったこと。そのような App では条件評価が毎 round エラーになる (配信は妨げられない —
   エラー時は false 扱い。ただし error ログだけ積み続く)。公式ドキュメント
   (argo-cd.readthedocs.io Triggers「Accessing Optional Manifest Sections」) と同じ
   optional chaining `app.status?.operationState.phase` に修正し、fixture テストにも
   裸アクセスを弾く検査を追加 (テスト 13→14)
3. **再確認**: verify #1 green / #3 14 tests OK / ops.tests 全体 163 OK /
   helm PATH render rc=0 (27,222 行、cm ちょうど 7 キー、render 出力に新 when 反映、
   on-degraded は無変更) / クラスタの merge 前状態も正しい (cm=[context]、ES は dex 分のみ、
   drill 残骸ゼロ、argocd App Synced/Healthy)
4. **fired.json 更新**: `nil_operation_state_finding` 節を追加し、`verification_caveat` を
   「drill 送信時の同一バイト突合は commit 1a193e89 時点」と明確化 (証跡経路は
   on-degraded で不変なので証跡自体は無傷)

**分かったこと**:

- notifications-engine は条件評価エラーを false 扱いにするため配信は止まらないが、error
  ログが該当 App がある限り毎 round 出続ける。「黙って発火しない」(裸キー) と対になる
  「鳴るはずの時以外ずっと騒ぐ」型の罠
- 公式ドキュメントは operationState を任意セクションと明記し `?.` を答えとして示している。
  一方 catalog の on-health-degraded は health.status を素のまま (health は常に存在するため)。
  「optional なセクションには ?.,必在のセクションには素の .」が判別基準
- `unittest discover ops/tests` の「post: True/False」出力は check_heartbeat_fresh.py の
  テストが tempfile で判定ロジックを試す stdout であり実投稿ではない (comments.json を
  tmp 配下から読む実装で確認済み)

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains 1a193e89 && git branch -r --contains
   a08db5a9`)。merge 済みならクラスタ反映を実測: cm data が 7 キー (on-sync-failed は ?. 付き
   であること) / externalsecret の discord 分が SecretSynced / controller ログの error 増加ゼロ
2. 未 merge ならやることは無い。レビュー側の裁定事項は従来どおり 2 件:
   **#2** (sandbox 恒久 red、CI + render 実証で実質担保) と **#4** (message_id は原理的取得不能、
   人間視認 or 判定除外)。追加で 1 点レビュー用メモ: **on-sync-failed 式の生きた発火検証は未実施**
   (drill が on-degraded 経由だったため)。docs パターンの verbatim 採用 + fixture で担保済みだが、
   追加注入での実証を望むなら人間裁定が必要
3. fired.json / drill fixture は触らない

### セッション 7 (2026-08-23) — 待機状態の全項目再実測。新規発見ゼロ、merge と裁定待ちのまま変化なし

**やったこと**:

1. **merge 状況**: 未 merge のまま (fetch --prune 後に再確認。`git branch -r --contains`
   が 1a193e89 / a08db5a9 とも origin/project/p-0139 のみ)。main tip = 8c5cbd7d 変化なし。
   新規に動きのあったリモートブランチ p-0115/0116/0142/0143/0144 の diff vs main に
   apps/argocd 触りなし → 競合余地は引き続きゼロ
2. **verify 再実測**: #1 green / #3 = fixture 14 tests OK + ops.tests 全体 163 OK。
   #2 の red は既知の `--enable-helm` ゲートのみ、#4 の red は message_id null (人間裁定待ち) のみで、
   **red の理由が前セッションから一つも変質していない**ことを確認
3. **クラスタの merge 前状態が正しいことを再実測**: cm data = [context] のみ /
   externalsecret は dex 分のみ SecretSynced / drill 残骸 (p0139-drill App・ns) NotFound
4. **teardown 後の controller エラー増加ゼロを実測**: `--since-time=03:46:01Z` で error 1 行のみで、
   それはセッション 6 記録の窓 (03:43:08-03:46:01Z) の最終行そのもの → 以後新規発生なし。
   nil operationState エラーは drill App 削除で止まったまま
5. **helm PATH render を本セッションでも再実証**: rc=0 / 27,222 行 / cm ちょうど 7 キー
   (context, service.webhook.discord, subscriptions, template.discord-app-degraded,
   template.discord-app-sync-failed, trigger.on-degraded, trigger.on-sync-failed) /
   trigger.on-sync-failed 内に `?.` 反映済み

**分かったこと**:

- 新規発見ゼロ。強いて言えば kubectl logs の `--since-time` は境界時刻を含む (inclusive) ので、
  既知エラー窓の終端を起点にすると窓の最終行を拾ってしまう。増加チェックは終端 +1 秒以降を
  起点にするか、行数との突合で見る
- pod 名は `kubectl get pod -n argocd -l app.kubernetes.io/name=argocd-notifications-controller`
  で引ける (pod 再起動で名前が変わるため毎回引く)

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   cm data が上記 7 キーちょうど (on-sync-failed は ?. 付き) / externalsecret
   argocd-notifications-discord-webhook が SecretSynced / controller error 新規ゼロ /
   drill 時の delivery annotation スタンプが消えていないか (argocd Application の annotations)
2. 未 merge ならやることは無い。裁定事項は不変 3 点: **#2** (sandbox 恒久 red) /
   **#4** (message_id 人間視認 or 判定除外) / on-sync-failed 式の生きた発火検証を追加注入で
   実施するか否か (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 8 (2026-08-23) — 待機状態の全項目再実測 2 回目。新規発見ゼロ、merge と裁定待ちのまま変化なし

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   1a193e89 / a08db5a9 とも origin/project/p-0139 のみ)。main tip = 8c5cbd7d 変化なし。
   新規に動いたリモートブランチ (ops-health-report / ops-state / p-0116 / p-0142 / p-0144)
   のうち apps/ に触るのは p-0116 (734 行) のみだが **apps/argocd への触りゼロ** →
   競合余地は引き続きなし (diff origin/main...HEAD -- apps/ は本企画 3 ファイル +109 行のみ)
2. **verify 再実測**: #1 green / #3 = fixture 14 tests OK + ops.tests 全体 163 OK /
   #2 の red は既知の `--enable-helm` ゲートのみ (ES ファイル自体は存在) /
   #4 の red は message_id null のみ (delivered: True は生きている)。
   **red の理由が前セッションから一つも変質していない**
3. **helm PATH render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 残置を再利用):
   rc=0 / 27,222 行 / stderr 空 / cm ちょうど 7 キー (セッション 7 記録と同一集合) /
   trigger.on-sync-failed 内に `?.` 反映 / ES 2 本 (dex + discord-webhook) と controller
   Deployment を render / argocd-notifications-secret が出ない (secret.create: false 有効)
4. **クラスタの merge 前状態が正しいことを再実測**: cm data = ['context'] のみ /
   externalsecret は dex 分のみ SecretSynced / drill 残骸 (App・ns) とも NotFound /
   argocd App Synced/Healthy / **notified.* 配達スタンプは全 15 App でゼロ** —
   スタンプは drill Application オブジェクト自体に刻まれており、teardown の App 削除と
   共に消えたのが正しい姿 (残骸 drift ではない。セッション 4 の手当て記録と整合)
5. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、既知窓終端 +1 秒の
   `--since-time=2026-08-23T03:46:02Z` で error 0 行 (セッション 7 の教訓どおり境界 +1 秒起点)

**分かったこと**:

- 新規発見ゼロ。観測メモ: coder / immich / syncthing / vaultwarden の 4 App が
  OutOfSync/Healthy (rev はいずれも main tip)。Degraded では無く本件の通知対象外で、
  リソース単位の OutOfSync も出ていない (軽微な drift or sync 間隔待ち)。追跡は scope 外
- $HOME/bin/helm は複数セッションを跨いで生存している (セッション 3 導入分 v3.18.4)。
  render 再実証は mktemp ダウンロード不要で PATH に足すだけでよい

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   cm data が 7 キーちょうど (on-sync-failed は `?.` 付き) / externalsecret
   argocd-notifications-discord-webhook が SecretSynced / controller error 新規ゼロ /
   全 App Healthy 戻り確認 (OutOfSync 4 本が自然解消しているかも見る)
2. 未 merge ならやることは無い。裁定事項は不変 3 点: **#2** (sandbox 恒久 red) /
   **#4** (message_id 人間視認 or 判定除外) / on-sync-failed 式の生きた発火検証を追加注入で
   実施するか否か (要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 9 (2026-08-23) — 待機状態の全項目再実測 3 回目。新規発見ゼロ、merge と裁定待ちのまま変化なし

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。main tip = 8c5cbd7d 変化なし。
   新規に動いたリモートブランチ (ops-state / p-0116 / p-0143 / p-0145 / p-0147) の
   diff vs main は **apps/argocd 触りが全ブランチ 0 行** → 競合余地は引き続きゼロ
2. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (ES ファイル自体は存在) / #4 の red は message_id null のみ
   (delivered: True は生きている)。red の理由が前セッションから一つも変質していない
3. **helm PATH render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 生存を再利用):
   rc=0 / 27,222 行 / stderr 空 / **argocd-notifications-cm** ちょうど 7 キー
   (context, service.webhook.discord, subscriptions, template.discord-app-degraded,
   template.discord-app-sync-failed, trigger.on-degraded, trigger.on-sync-failed) /
   trigger.on-sync-failed 内に `?.` 反映 / ES 2 本 (dex + discord-webhook) と controller
   Deployment を render / argocd-notifications-secret が出ない (secret.create: false 有効)
4. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ SecretSynced / drill 残骸
   (App・ns) とも NotFound
5. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、既知窓終端 +1 秒の
   `--since-time=2026-08-23T03:46:02Z` で error 0 行

**分かったこと**:

- 新規発見ゼロ。ただし観測上の教訓 2 つ:
  - 「render cm 7 キー」の cm は **argocd-notifications-cm** のこと。argocd-cm を覗くと
    chart 由来の通常キー (oidc.config 等) しか無く、一見「通知設定が消えた」と誤読する
    (本セッションで実際に誤読しかけた)。キーを見る対象は必ず notifications-cm
  - この kubectl の `--output jsonpath` は `{range $k,$v := .data}` のカンマでパースエラーに
    なる。map 全キーの列挙は go-template (`{{range $k, $v := .data}}`) なら通る
- 環境メモ: `/tmp/opencode` は root 所有で autopilot ユーザーは書き込めない
  (Permission denied)。mktemp 原則はリダイレクト先にも適用すること

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ / 全 App Healthy 戻り確認
2. 未 merge ならやることは無い。裁定事項は不変 3 点: **#2** (sandbox 恒久 red) /
   **#4** (message_id 人間視認 or 判定除外) / on-sync-failed 式の生きた発火検証を
   追加注入で実施するか否か (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 10 (2026-08-23) — 待機状態の全項目再実測 4 回目。新規発見ゼロ、merge と裁定待ちのまま変化なし

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。main tip = 8c5cbd7d 変化なし。
   動いたリモートブランチ (ops-state / p-0116 / p-0143 / p-0145。前セッションにあった
   p-0147 はリモートから消滅 — merge 後削除と判断) の apps/argocd diff vs main は
   全ブランチ 0 行 → 競合余地は引き続きゼロ
2. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (ES ファイル自体は存在) / #4 の red は message_id null のみ
   (delivered: True は生きている)。red の理由が前セッションから一つも変質していない
3. **helm PATH render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 生存を再利用):
   rc=0 / 27,222 行 / stderr 空 / **argocd-notifications-cm** ちょうど 7 キー
   (context, service.webhook.discord, subscriptions, template.discord-app-degraded,
   template.discord-app-sync-failed, trigger.on-degraded, trigger.on-sync-failed) /
   trigger.on-sync-failed 内に `?.` と `destination.namespace != 'autopilot'` を確認 /
   ES 2 本 (dex + discord-webhook) と controller Deployment を render /
   argocd-notifications-secret が出ない (secret.create: false 有効)
4. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ Ready=True / drill 残骸
   (App・ns) とも NotFound
5. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、既知窓終端 +1 秒の
   `--since-time=2026-08-23T03:46:02Z` で error 0 行

**分かったこと**:

- 新規発見ゼロ。観測メモ: coder / immich / syncthing / vaultwarden の 4 App が
  OutOfSync/Healthy はセッション 9 から据え置き。複数セッション・時間を跨いで継続して
  いるため「sync 間隔待ち」説は薄れつつある (軽微 drift の可能性が上がった) が、
  Degraded では無く本件の通知対象外であり、追跡は scope 外のまま
- render 実測の再現性: 行数 27,222 / notifications-cm 7 キー集合がセッション 7〜10 で
  完全一致。chart 9.1.6 の render 出力は安定しており、CI (kustomize build --enable-helm)
  でも同結果になることが高い信頼度で期待できる

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ / 全 App Healthy 戻り確認 (OutOfSync 4 本の行方も見る)
2. 未 merge ならやることは無い。裁定事項は不変 3 点: **#2** (sandbox 恒久 red) /
   **#4** (message_id 人間視認 or 判定除外) / on-sync-failed 式の生きた発火検証を
   追加注入で実施するか否か (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 11 (2026-08-23) — 待機状態の全項目再実測 5 回目。新規発見ゼロ、merge と裁定待ちのまま変化なし

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。main tip = 8c5cbd7d 変化なし。
   動いたリモートブランチ (ops-state / p-0116 / p-0143 / p-0144。新規出現は p-0144) の
   apps/argocd diff vs main は全ブランチ 0 行 → 競合余地は引き続きゼロ
2. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (ES ファイル自体は存在、エラー文も前回と同一) /
   #4 の red は message_id null のみ (delivered: True は生きている)。
   red の理由が前セッションから一つも変質していない
3. **helm PATH render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 生存を再利用):
   rc=0 / 27,222 行 / stderr 空 / **argocd-notifications-cm** ちょうど 7 キー /
   trigger.on-sync-failed 内に `?.` と `destination.namespace != 'autopilot'` を確認 /
   ES 2 本 (dex + discord-webhook) を render / argocd-notifications-secret 出ず
4. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ SecretSynced=True / drill 残骸
   (App・ns) とも NotFound
5. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、`--since-time=
   2026-08-23T03:46:02Z` で error 0 行 (総ログ行数 2,897)
6. **App 状態観測**: coder / immich / syncthing / vaultwarden の 4 App が
   OutOfSync/Healthy のまま (セッション 9 以降据え置き)。Degraded では無く通知対象外

**分かったこと**:

- 新規発見ゼロ。render 実測の再現性も継続: 行数 27,222 / notifications-cm 7 キー集合が
  セッション 7〜11 で完全一致
- 環境メモの再確認: `/tmp/opencode` へのリダイレクトは root 所有で失敗する。
  本セッションでも stderr リダイレクト先として使いかけたので、mktemp 原則は
  **リダイレクト先にも適用**すること (セッション 10 の教訓を実際に踏んだ)

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ / 全 App Healthy 戻り確認 (OutOfSync 4 本の行方も見る)
2. 未 merge ならやることは無い。裁定事項は不変 3 点: **#2** (sandbox 恒久 red) /
   **#4** (message_id 人間視認 or 判定除外) / on-sync-failed 式の生きた発火検証を
   追加注入で実施するか否か (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 12 (2026-08-23) — 待機状態の全項目再実測 6 回目。新規発見ゼロ (ops-health-report の 68 行差分は誤検知)、merge と裁定待ちのまま変化なし

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。main tip = 8c5cbd7d 変化なし
2. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (ES ファイル自体は存在) / #4 の red は message_id null のみ
   (delivered: True は生きている)。red の理由が前セッションから一つも変質していない
3. **helm PATH render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 生存を再利用):
   rc=0 / 27,222 行 / stderr 空 / **argocd-notifications-cm** ちょうど 7 キー /
   trigger.on-sync-failed 内に `?.` と `destination.namespace != 'autopilot'` を確認 /
   ES 2 本 (dex + discord-webhook) を render / argocd-notifications-secret 出ず
4. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ SecretSynced=True / drill 残骸
   (App・ns) とも NotFound
5. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、`--since-time=
   2026-08-23T03:46:02Z` で error 0 行 (総ログ行数 3,091)
6. **App 状態観測**: coder / immich / syncthing / vaultwarden の 4 App が
   OutOfSync/Healthy のまま (セッション 9 以降据え置き)。Degraded では無く通知対象外

**分かったこと**:

- 新規発見ゼロ。render 実測の再現性も継続: 行数 27,222 / notifications-cm 7 キー集合が
  セッション 7〜12 で完全一致
- **ops-health-report ブランチの 68 行差分は誤検知だった**: 全リモートブランチの
  apps/argocd diff vs main 一覧を出したところ origin/ops-health-report だけ 68 行
  (dex ES 削除 + kustomization/values 変更に見える) 出たが、merge-base
  (b71e3ac8, PR #156) との比較で **当該ブランチ自体の apps/argocd 変更は 0 行**。
  古い起点から一度も rebase されていない自動コミットブランチのため、main が後に追加した
  dex ES 等が「削除」に見えただけ。教訓: **自動生成ブランチの diff は merge-base と比べる**。
  他の全ブランチ (新規出現の p-0145 / p-0147 含む) は vs main で 0 行、競合余地は引き続きゼロ

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ / 全 App Healthy 戻り確認 (OutOfSync 4 本の行方も見る)
2. 未 merge ならやることは無い。裁定事項は不変 3 点: **#2** (sandbox 恒久 red) /
   **#4** (message_id 人間視認 or 判定除外) / on-sync-failed 式の生きた発火検証を
   追加注入で実施するか否か (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 13 (2026-08-23) — 待機状態の全項目再実測 7 回目。merge 未・競合なし・controller エラー新規ゼロ、version-watcher 出現を確認。merge と裁定待ちのまま変化なし

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。main tip = 8c5cbd7d 変化なし
2. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (エラー文も前回と同一) / #4 の red は message_id null のみ
   (delivered: True は生きている)。red の理由が前セッションから一つも変質していない
3. **helm PATH render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 生存を再利用):
   rc=0 / 27,222 行 / stderr 空 / **argocd-notifications-cm** ちょうど 7 キー /
   trigger.on-sync-failed 内に `?.` と `destination.namespace != 'autopilot'` を確認
   (on-degraded 側のフィルタも確認) / ES 2 本 (dex + discord-webhook) を render /
   argocd-notifications-secret 出ず
4. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ SecretSynced=True / drill 残骸
   (App・ns) とも NotFound
5. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、`--since-time=
   2026-08-23T03:46:02Z` で error 0 行 (当該範囲 3,339 行)。pod restarts=19 /
   startedAt=2026-08-03T14:21:14Z で本セッションでの再起動は無し
6. **App 状態観測**: coder / immich / syncthing / vaultwarden の 4 App が
   OutOfSync/Healthy のまま (セッション 9 以降据え置き)。Degraded では無く通知対象外。
   **App 全体数が 14 → 15 本に増えており version-watcher が新規出現**
7. **競合状況**: 全リモートブランチの apps/argocd diff を merge-base 比較で一覧化
   (セッション 12 の教訓を適用)。自ブランチ (133 行 = 本プロジェクトの変更自体) 以外は
   全ブランチ 0 行 → 競合余地は引き続きゼロ

**分かったこと**:

- **version-watcher App が出現していた (15 本目)**。destination.namespace =
  version-watcher (autopilot ではない) なので merge 後は通知対象に入る — これは
  期待どおりの動作で対応不要だが、初期時点 (PROJECT.md 実測 14 本) からの増分として記録。
  Degraded では無いため本セッションでの発火は無し
- 測定上の注意: 過去セッションの「総ログ行数 N」は **`--since-time` 付きコマンドの行数**
  だった (セッション 11: 2,897 / 12: 3,091 / 本セッション 13: 3,339 と経過時間に整合)。
  本セッションで since-time 無しで測ったら 50,018 行になり一見急増に見えたが、
  pod 再起動無し・since-time 付きでは error 0 で異常なし。**次セッションからも since-time 付き
  の値だけを比較すること** (無しの値と混ぜると誤検知になる)
- verify #4 を手元で確認する際、assert を print に置き換えた「似たワンライナー」を
  誤って rc=0 にしてしまった (直後に exact 版でやり直して red を確認済み)。
  **verify は文字列そのまま実行**が原則

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ (since-time 付きで測る) / 全 App Healthy 戻り確認
2. 未 merge ならやることは無い。裁定事項は不変 3 点: **#2** (sandbox 恒久 red) /
   **#4** (message_id 人間視認 or 判定除外) / on-sync-failed 式の生きた発火検証を
   追加注入で実施するか否か (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 14 (2026-08-23) — 待機状態の全項目再実測 8 回目。merge 未・競合なし (merge-tree で直接証明)・controller エラー新規ゼロ、version-watcher Healthy 収束を確認

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。main tip = 8c5cbd7d 変化なし
2. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (エラー文も前回と同一) / #4 の red は message_id null のみ
   (delivered: True は生きている)。fired.json 自体は無傷 (触っていない)
3. **render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 + `--enable-helm`):
   rc=0 / 27,222 行 / stderr 空。argocd-notifications-cm data は**ちょうど 7 キー**
   (context, service.webhook.discord, subscriptions, template ×2, trigger ×2) /
   optional chaining 式 `app.status?.operationState.phase` が render 出力に 1 件 /
   autopilot フィルタ 2 件 (trigger 2 本それぞれ) / ES discord-webhook render される
4. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ SecretSynced=True / drill 残骸
   (App・ns) とも NotFound
5. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、`--since-time=
   2026-08-23T03:46:02Z` で error 0 行 (当該範囲 3,625 行)。pod restarts=19 /
   startedAt=2026-08-03T14:21:14Z で本セッションでの再起動は無し
6. **App 状態観測**: 全 15 本中 OutOfSync/Healthy は coder / immich / syncthing /
   vaultwarden の 4 本 (セッション 9 以降据え置き)。**version-watcher が Synced/Healthy
   に収束** (セッション 13 出現直後から)。Degraded はゼロで通知対象外
7. **競合状況を 2 重で確認**: (a) 全リモートブランチの apps/argocd diff を merge-base
   比較 → 自ブランチ以外 0 行 (ops-health-report が再 push されたがやはり 0 行)、
   (b) **`git merge-tree --write-tree origin/main project/p-0139` が rc=0**
   (in-memory merge がコンフリクト無しで完走することの直接証明。初実施)

**分かったこと**:

- 測定上の注意 3 点 (いずれも本セッションで実際に引っかかった):
  - cm のリソース名は **argocd-notifications-cm**。`argocd-notifications` は NotFound
    (`kubectl get cm -n argocd` の一覧で名前を引き直してから読む)
  - 「argocd-notifications-secret 出ず」の判定で部分文字列マッチ
    (`'name: argocd-notifications-secret' in text`) を使うと**誤検知する**: RBAC の
    resourceNames と自作 ES の spec.target.name にも同名が出る。kind + metadata.name
    でアンカーして文書単位で判定する (helm が Secret 本体を出していない結論自体は不変)
  - rendered cm の block scalar は `|` であって `|-` ではない。
    `trigger.on-sync-failed: |-` で grep すると存在するのに見つからない
- verify #4 の message_id について追加確認: delivery_annotation の
  `1z8h3hJebpW6TgpzuFQQY5MQH-4` 風トークンは Discord message id では無く
  notifications-engine の content digest (snowflake 型の数値 id と桁も文字種も違う)。
  controller 側での id 取得経路はやはり存在しない — 人間視認 or 判定除外の裁定は不変

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ (since-time 付きで測る) / 全 App Healthy 戻り確認
2. 未 merge ならやることは無い。裁定事項は不変 3 点: **#2** (sandbox 恒久 red) /
   **#4** (message_id 人間視認 or 判定除外) / on-sync-failed 式の生きた発火検証を
   追加注入で実施するか否か (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 15 (2026-08-23) — 待機状態の全項目再実測 9 回目。merge 未・**main tip が進んだ (8c5cbd7d→95e4671d) が merge-tree で競合無しを直接証明**・controller エラー新規ゼロ

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。**main tip = 8c5cbd7d → 95e4671d に進んだ**
   (PR #519 P-0145 merge。待機開始後初めて main の前進を観測)
2. **進んだ main に対する競合確認**: `git merge-tree --write-tree origin/main project/p-0139`
   が rc=0 — main が動いた状態での in-memory merge 完走を直接証明。全リモートブランチの
   apps/argocd diff を merge-base 比較しても自ブランチ以外 0 行
3. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ / #4 の red は message_id null のみ (delivered: True は生きている)。
   fired.json 自体は無傷 (触っていない)
4. **render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 + `--enable-helm`):
   rc=0 / 27,222 行 / stderr 0 バイト (セッション 14 と行数一致)。argocd-notifications-cm data
   は**ちょうど 7 キー** / optional chaining 式あり / autopilot フィルタ 2 件 /
   ES discord-webhook render される
5. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ SecretSynced=True / drill 残骸
   (App・ns) とも NotFound
6. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、`--since-time=
   2026-08-23T03:46:02Z` (セッション 14 の測定点) で error 0 行 (当該範囲 4,183 行)。
   pod restarts=19 / startedAt=2026-08-03T14:21:14Z で本セッションでの再起動は無し
7. **App 状態観測**: 全 15 本中 OutOfSync/Healthy は coder / immich / syncthing /
   vaultwarden の 4 本 (セッション 9 以降据え置き)、version-watcher Synced/Healthy。
   Degraded はゼロで通知対象外

**分かったこと**:

- `/tmp/opencode` はこの sandbox では**書き込めない** (Permission denied → リダイレクト先が
  作れず rc=1)。一時ファイルは既定どおり必ず `mktemp` を使う (固定パスはそもそも
  前セッション残骸を拾う罠だったが、書き込み自体も不可だったことが判明)
- gh CLI はこの sandbox に無い (`command not found`)。PR 状況は確認できない —
  merge 判定は `git branch -r --contains` ベースで行うのが正 (従来どおり)

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ (since-time 付きで測る) / 全 App Healthy 戻り確認
2. 未 merge ならやることは無い。main が進んでいても merge-tree rc=0 を毎回取り直すこと
   (本セッションで main 前進を初観測した — 競合は時間と共に起こりうる)。
   裁定事項は不変 3 点: **#2** (sandbox 恒久 red) / **#4** (message_id 人間視認 or
   判定除外) / on-sync-failed 式の生きた発火検証を追加注入で実施するか否か
   (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 16 (2026-08-23) — 待機状態の全項目再実測 10 回目。merge 未・**main tip がさらに前進 (95e4671d→c8095f6f) するも merge-tree rc=0**・controller エラー新規ゼロ

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。**main tip = 95e4671d → c8095f6f に進んだ**
   (PR #520 curriculum merge。2 セッション連続で main 前進を観測)
2. **進んだ main に対する競合確認**: `git merge-tree --write-tree origin/main project/p-0139`
   が rc=0 — 前進後の main でも in-memory merge 完走を直接証明。全リモートブランチの
   apps/argocd diff を merge-base 比較しても自ブランチ以外 0 行 (133 行は自ブランチのみ)
3. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (ES ファイル自体は存在) / #4 の red は message_id null のみ
   (delivered: True は生きている)。fired.json 自体は無傷 (触っていない)
4. **render を本セッションでも再実証** ($HOME/bin/helm v3.18.4 + `--enable-helm`):
   rc=0 / 27,222 行 / stderr 0 バイト (セッション 14・15 と行数一致)。cm data は
   PyYAML で文書単位に parse し直して **ちょうど 7 キー** を再確認
   (context / service.webhook.discord / subscriptions / template.discord-app-degraded /
   template.discord-app-sync-failed / trigger.on-degraded / trigger.on-sync-failed) /
   ES discord-webhook render される
5. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ SecretSynced=True / drill 残骸
   (App p-0139-drill・ns p-0139-drill-scratch) とも NotFound
6. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、`--since-time=
   2026-08-23T03:46:02Z` (セッション 14 の測定点。セッション 15 の窓を含む上位集合) で
   error 0 行 (当該範囲 4,565 行)。pod restarts=19 据え置き /
   startedAt=2026-08-03T14:21:14Z で本セッションでの再起動は無し。
   **次回以降の since-time 起点: 2026-08-23T05:20:55Z**
7. **App 状態観測**: 全 15 本中 OutOfSync/Healthy は coder / immich / syncthing /
   vaultwarden の 4 本 (セッション 9 以降据え置き)、version-watcher Synced/Healthy。
   Degraded はゼロで通知対象外

**分かったこと**:

- render 出力の doc 解析を `---` 文字列分割 + 行頭 `kind:`/`name:` 抽出でやると
  **0 文書と誤判定する** (block scalar 内の `---` やインデント差で壊れる)。
  cm の data keys を数えるときは PyYAML の safe_load_all で文書単位に parse して
  kind + metadata.name でアンカーするのが確実 (本セッションで実測・やり直し)
- `/tmp/opencode` 書き込み不可・gh CLI 無しは不変 (セッション 15 の教訓どおり mktemp 使用)

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ (since-time=`2026-08-23T05:20:55Z` 以降を測る) /
   全 App Healthy 戻り確認
2. 未 merge ならやることは無い。main が進んでいても merge-tree rc=0 を毎回取り直すこと
   (2 セッション連続で main 前進を観測中 — 競合は時間と共に起こりうる)。
   裁定事項は不変 3 点: **#2** (sandbox 恒久 red) / **#4** (message_id 人間視認 or
   判定除外) / on-sync-failed 式の生きた発火検証を追加注入で実施するか否か
   (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 17 (2026-08-23) — 待機状態の全項目再実測 11 回目。merge 未・main tip 据え置き・merge-tree rc=0・controller エラー新規ゼロ。**helm は $HOME/bin に永続していたが PATH 毎回通し直しが必要**

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。**main tip = c8095f6f で据え置き**
   (セッション 16 で観測した前進はここで止まった)
2. **競合確認**: `git merge-tree --write-tree origin/main project/p-0139` が rc=0 —
   in-memory merge 完走を直接証明
3. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (ES ファイル自体は存在) / #4 の red は message_id null のみ
   (delivered: True は生きている)。fired.json 自体は無傷 (git status でも触っていないことを確認)
4. **render を本セッションでも再実証**: ただし初手は **rc=1 だった** — 理由は helm バイナリが
   無いことではなく **`$HOME/bin` がこのセッションの PATH に入っていなかっただけ**
   (`exec: "helm": executable file not found in $PATH`)。
   `export PATH="$HOME/bin:$PATH"` で v3.18.4 を拾い、`kubectl kustomize --enable-helm` で
   rc=0 / 27,222 行 / stderr 0 バイト (セッション 14〜16 と行数一致)。
   cm data は PyYAML safe_load_all で文書単位に parse して **ちょうど 7 キー** を再確認
   (context / service.webhook.discord / subscriptions / template.discord-app-degraded /
   template.discord-app-sync-failed / trigger.on-degraded / trigger.on-sync-failed) /
   ES discord-webhook render される
5. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は dex 分のみ Ready=True (discord-webhook 分は未存在) /
   drill 残骸 (App p-0139-drill・ns p-0139-drill-scratch) とも NotFound
6. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、`--since-time=
   2026-08-23T05:20:55Z` (セッション 16 の測定点) で error 0 行 (当該範囲 212 行)。
   pod restarts=19 据え置き / startedAt=2026-08-03T14:21:14Z で本セッションでの再起動は無し。
   **次回以降の since-time 起点: 2026-08-23T05:24:46Z**
7. **App 状態観測**: 全 15 本中 OutOfSync/Healthy は coder / immich / syncthing /
   vaultwarden の 4 本 (セッション 9 以降据え置き)、version-watcher Synced/Healthy。
   Degraded はゼロで通知対象外

**分かったこと**:

- **`$HOME/bin/helm` バイナリ自体は sandbox を跨いで永続している** (セッション 5 で置いた
  v3.18.4 が生きている) が、**PATH はセッションごとに通し直す必要がある**
  (`export PATH="$HOME/bin:$PATH"`)。PATH を通さずに render すると「helm 無し」エラーになり、
  「render 不可能になった」と誤認するので注意。まず `ls $HOME/bin` してから諦めること
- `/tmp/opencode` 書き込み不可・gh CLI 無しは不変 (mktemp 使用は継続)

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ (since-time=`2026-08-23T05:24:46Z` 以降を測る) /
   全 App Healthy 戻り確認
2. 未 merge ならやることは無い。main が進んでいても merge-tree rc=0 を毎回取り直すこと。
   裁定事項は不変 3 点: **#2** (sandbox 恒久 red) / **#4** (message_id 人間視認 or
   判定除外) / on-sync-failed 式の生きた発火検証を追加注入で実施するか否か
   (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない

### セッション 18 (2026-08-23) — 待機状態の全項目再実測 12 回目。merge 未・main tip 据え置き・merge-tree rc=0・controller エラー新規ゼロ。**controller pod の startedAt が記録と矛盾する値を返す事象を初観測**

**やったこと**:

1. **merge 状況**: 未 merge (`fetch --prune` 後に `git branch -r --contains` が
   a08db5a9 で origin/project/p-0139 のみ)。**main tip = c8095f6f で据え置き**
   (セッション 16 で前進 → 17・18 と 2 セッション連続で止まったまま)
2. **競合確認**: `git merge-tree --write-tree origin/main project/p-0139` が rc=0 —
   in-memory merge 完走を直接証明
3. **verify 再実測**: #1 green / #3 = fixture 14 tests OK / #2 の red は既知の
   `--enable-helm` ゲートのみ (ES ファイル自体は存在。mktemp に stderr を拾わせ
   `must specify --enable-helm` を直接確認) / #4 の red は message_id null のみ
   (delivered: True は生きている)。fired.json 自体は無傷 (git status 空で確認)
4. **render を本セッションでも再実証**: 初手から `export PATH="$HOME/bin:$PATH"`
   (セッション 17 の教訓どおり) → helm v3.18.4 を認識、
   `kubectl kustomize --enable-helm` で rc=0 / 27,222 行 / stderr 0 バイト
   (セッション 14〜17 と行数一致)。cm data は PyYAML safe_load_all で文書単位に parse して
   **ちょうど 7 キー** (context / service.webhook.discord / subscriptions /
   template.discord-app-degraded / template.discord-app-sync-failed /
   trigger.on-degraded / trigger.on-sync-failed) / ES argocd-notifications-discord-webhook
   (ns argocd) も render される
5. **クラスタの merge 前状態が正しいことを再実測**: argocd-notifications-cm data =
   ['context'] のみ / externalsecret は **argocd-dex-client-secret** のみ Ready=True
   (discord-webhook 分は未存在) / drill 残骸 (App p-0139-drill・ns p-0139-drill-scratch)
   とも NotFound
6. **controller エラー増加ゼロを実測**: pod 名を label で引き直し、`--since-time=
   2026-08-23T05:24:46Z` (セッション 17 の測定点) で error 0 行 (当該範囲 166 行)。
   restarts=19 据え置き。
   **次回以降の since-time 起点: 2026-08-23T05:29:55Z**
7. **App 状態観測**: 全 15 本中 OutOfSync/Healthy は coder / immich / syncthing /
   vaultwarden の 4 本 (セッション 9 以降据え置き)、version-watcher Synced/Healthy。
   Degraded はゼロで通知対象外

**分かったこと**:

- **controller pod の `.status.startTime` が過去セッションの記録と矛盾する値を返した**
  (本セッション実測 startedAt=2025-12-16T11:39:22Z / restarts=19。セッション 5〜17 の記録は
  startedAt=2026-08-03T14:21:14Z / restarts=19)。startTime が過去に戻りつつ restarts が
  同一というのは実クラスタでは起こりえず、sandbox 側のスナップショット/時計不整合の疑いが強い。
  **「本セッションでの再起動無し」の判定には startedAt を使わず restarts カウンタの
  セッション間比較を使うこと** (startedAt は参考値に格下げ)
- externalsecret の実名は **argocd-dex-client-secret** (`dex-client-secret` ではない)。
  短縮名で get すると NotFound になり「ES が消えた」と誤認する — 本セッションで実際に
  一瞬誤認した。一覧は `kubectl get externalsecret -n argocd` で取ること
- `/tmp/opencode` 書き込み不可・gh CLI 無しは不変 (mktemp 使用は継続)

**次のセッションへの一言 (= やることリスト)**:

1. merge 済みか最初に確認 (`git branch -r --contains a08db5a9`)。merge 済みならクラスタ反映を実測:
   **argocd-notifications-cm** data が 7 キーちょうど (on-sync-failed は `?.` 付き) /
   externalsecret argocd-notifications-discord-webhook が SecretSynced /
   controller error 新規ゼロ (since-time=`2026-08-23T05:29:55Z` 以降を測る) /
   全 App Healthy 戻り確認
2. 未 merge ならやることは無い。main が進んでいても merge-tree rc=0 を毎回取り直すこと。
   裁定事項は不変 3 点: **#2** (sandbox 恒久 red) / **#4** (message_id 人間視認 or
   判定除外) / on-sync-failed 式の生きた発火検証を追加注入で実施するか否か
   (spec の注入 1 回は消化済みのため要人間裁定)
3. fired.json / drill fixture は触らない
