# P-0139 — アプリが Degraded になっても誰の耳にも鳴らない — ArgoCD Notifications (既に稼働済み・設定ゼロ) を Discord に繋ぎ、合成障害で本当に鳴るところまで通す

## 目的

argocd-notifications-controller はクラスタで稼働しているが、apps/argocd には設定が 1 行も無く
(2026-08-23 実測)、アプリの Degraded は人間がダッシュボードか health を開くまで誰にも届かない。
08-22 の B2 cap 超過 → backup 子 Job 失敗 → coder/immich Degraded も発見は health レポートで
あって通知ではなかった。#49 と heart 9 日沈黙と同じ「green のまま / 黙ったまま使えなくなる」型に
対する push 型警報 (VISION 接点設計の push 側) の ArgoCD 層空席を、既にある部品
(controller・DISCORD_WEBHOOK_URL・ExternalSecret の定石) の配線だけで埋める。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0139` の checkout でリポジトリルートから実行)。

- [ ] `grep -q 'on-degraded\|on-sync-failed' apps/argocd/values.yaml`
  — trigger (on-degraded / on-sync-failed) が values.yaml に定義されていること。
  実測 rc=1 ('notifications' 自体が values.yaml に 1 件も無い)。
- [ ] `test -f apps/argocd/discord-webhook-external-secret.yaml && kubectl kustomize apps/argocd >/dev/null`
  — webhook URL を供給する ExternalSecret manifest が実在し、apps/argocd 全体が render
  できること。実測 rc=1 (ファイル自体が未存在)。
  **環境注意**: この sandbox には helm バイナリが無く、kubectl v1.35 は helmCharts を含む
  kustomization に `--enable-helm` を要求するため、後半の render は sandbox 内では常に失敗する。
  実 render は CI (`.github/workflows/ci.yml:94` の `kustomize build --enable-helm`) と
  wrapper 再実測が担保する。worker はファイル存在と YAML 構造を verify #3 の fixture テストで
  固定し、render 成否を CI に委ねてよい。
- [ ] `python3 -m unittest ops.tests.test_argocd_notifications`
  — trigger/template の YAML 構造を固定する fixture テストが通ること。
  実測 ImportError (テストモジュール未存在)。
- [ ] `python3 -c "import json; f=json.load(open('ops/projects/logs/P-0139/fired.json')); assert f.get('delivered') and f.get('message_id')"`
  — 合成障害が Discord に実際に届いた記録 (メッセージ id) があること。
  実測 rc≠0 (`ops/projects/logs/P-0139/` ごと未存在)。

**verify は DoD の下限であって DoD そのものではない。** verify #1/#3 は YAML 構造しか見ず、
「Discord に本当に鳴った」は #4 の fired.json (メッセージ id 実測) だけが証拠になる。
逆に #4 は一度きりの合成障害であり、以後の通知が生き続けていることは見張らない —
その担保は fixture テスト (#3) と CI render が担う。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した。調べ直さなくてよい)

- apps/argocd/kustomization.yaml は helm chart argo-cd **9.1.6** を valuesFile values.yaml で
  render し、resources に ingress.yaml と dex-client-secret-external-secret.yaml を載せる。
  新しい ExternalSecret ファイルは **resources への追記が必須** (追記忘れは死にコードになる)
- ExternalSecret の定石は apps/argocd/dex-client-secret-external-secret.yaml:
  external-secrets.io/v1 / ClusterSecretStore `doppler` / namespace argocd /
  target.template.labels に `app.kubernetes.io/part-of: argocd` / Doppler remoteRef key。
  Discord 用はこの形を踏襲して Doppler キー `DISCORD_WEBHOOK_URL` を参照する
  (spec の why が「ExternalSecret の定石」を名指し)
- values.yaml 現状 (72 行): configs.cm (oidc/rbac/params) + dex.enabled=false +
  各コンポーネント resources のみ。notifications 関連は完全に無い。
  chart 側 default で controller が有効なのかは sandbox では render 不能 (helm 無し) のため
  未検証 — spec は「稼働済み」と断言しているので、worker は最初にクラスタ実機
  (MCP read or `kubectl get deploy -n argocd`) で controller Deployment と
  argocd-notifications-secret の有無を確認し、values に `notifications.enabled` の明示が
  要るかを実機で決める
- webhook URL の参照構文は oidc の `$name:key` (T-0060, dex-client-secret 参照) とは
  別系統のはず (notifications は argocd-notifications-secret 由来の `$<key>` 解決)。
  chart 9.1.6 の notifications 関連 values 形 (secret.items / service.webhook.discord 等) を
  chart ソースか公式 doc で確認してから書く。推測で values を書かない
- **Application オブジェクトは全 14 本とも argocd namespace に在住する** (apps/apps.yaml 実測)。
  「器自身の namespace の変化では鳴らさない」フィルタは Application 自身の属する namespace
  では表現できず、`app.spec.destination.namespace != 'autopilot'` のように **destination 側**
  で絞る (ops-dashboard / autopilot の application.yaml が destination.namespace: autopilot
  であることを実測)。除外条件と理由は DoD どおり values.yaml のコメントに残す
- capabilities に `kubectl-write` あり。合成障害の捨て Application 作成・削除は kubectl CLI で
  行う (CLAUDE.md: write は CLI、read は MCP)。scratch は Git 管理外の直接 apply なので
  apps root (prune: true) の対象にならないが、それでも削除まで必ず自力で行う

### 決めてあること

- 配線順: (1) ExternalSecret manifest + kustomization resources 追記 → (2) values.yaml に
  trigger 2 本 (on-degraded / on-sync-failed) と template (Discord 用・日本語 1 行) →
  (3) fixture テスト → (4) 合成障害注入 → fired.json → 後始末。manifest 段落ごとに CI を通す
- 合成障害は spec dod (3) どおり「壊れた image を指す捨て Application」を scratch namespace に
  直接 apply。imagePullBackOff なら数分で Degraded に落ちる。fired.json には verify が見る
  delivered / message_id に加えて、時刻・Application 名・後始末の削除確認も書いてよい
  (証跡は多いほうが良い。形式は自由)
- Discord メッセージは日本語 1 行 (spec dod)。embeds 等に凝らない。通知先は人間の Discord 1 本

## やらないこと

- **on-degraded / on-sync-failed 以外の trigger や他チャネル (Slack/email 等) の追加**。
  spec dod の範囲はこの 2 本 + Discord 1 本のみ
- **器自身 (autopilot ns / heart / runner / curriculum) の失敗通知の設計**。本件は homelab
  アプリ層の push 警報。器の観測経路は ops-health-reporter が既に担っており触らない
- **chart version の bump / argocd 本体の更新**。kustomization.yaml の version は nix bootstrap
  (k3s-manifests.nix) との同時更新制約がある (check_version_sync.py が CI で検査)。bump しない
- **ダッシュボードや health レポート側の手直し**。pull 型は既に在るので触らない (1 PR 1 論点)
- **ops/backlog.json / ops/state.json / ops/journal/ の更新**。heart が直接 push する領域で
  コンフリクトする (CLAUDE.md)
