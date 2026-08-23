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
