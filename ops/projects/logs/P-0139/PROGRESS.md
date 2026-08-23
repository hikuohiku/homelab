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
