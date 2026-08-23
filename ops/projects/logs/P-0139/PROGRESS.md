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
