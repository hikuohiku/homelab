# P-0175 — 秘密の給水塔が止まった日を先に演じる — External Secrets の唯一の上流 (Doppler) を一時遮断し、既存の Secret とアプリが何時間持つかを秒で実測する

## 目的

Doppler (homelab/prd) は全 ExternalSecret の唯一の上流で、credential 分離設計 (CLAUDE.md) も backup
鍵もここを通る単一点。しかし「Doppler が落ちてもクラスタは動き続けるのか、いつ崩れ始めるのか」を
実測した者はない — B2 download cap (P-0111 の一次原因) と同型の「上流通過型障害」が最後まで残る場所。
NetworkPolicy で ESO Pod の Doppler 行きだけを一時遮断し、(a) 既存 Secret が残り続けるか (b) 動中の
Pod は生き続けるか (c) 遮断中の Pod 再作成が死ぬか (d) SecretSyncedError は何秒後に顔を出すか、を
壁時計で記録してから復旧も実測する。#49・heart 沈黙と同じ「失敗すら起こらない停止」の候補地を、
障害当日ではなく平時に潰す。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0175` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f ops/projects/logs/P-0175/drill-report.json`
  — 遮断演習の記録ファイルが存在すること。
  実測 rc=1 (`ops/projects/logs/P-0175/` 自体がまだ無い)。
- [ ] `python3 -c "import json; d=json.load(open('ops/projects/logs/P-0175/drill-report.json')); assert d.get('blocked_minutes',0)>=30 and d.get('recovery_verified') is True and isinstance(d.get('pod_recreate_result'),str) and d.get('eso_error_first_seen_seconds') is not None"`
  — 演習が仕様どおり行われたことの機械判定: 遮断 30 分以上・復旧確認済み・遮断中の Pod 再作成結果が
  文字列で記録・SecretSyncedError 初発までの秒数が数値で記録されていること。
  実測 rc=1 (FileNotFoundError — drill-report.json 未存在)。
- [ ] `python3 -c "c=open('apps/ops-health-reporter/report.py').read(); assert 'externalsecret' in c.lower() or 'secretsynced' in c.lower()"`
  — health レポートに ExternalSecret の状態 (SecretSyncedError 数・最終同期からの経過) が載り、
  静かな鮮度劣化が見える化されていること。
  実測 rc=1 (AssertionError — report.py は現状 ExternalSecret を一切見ていない)。
- [ ] `test -f docs/doppler-outage-runbook.md && grep -q '応急' docs/doppler-outage-runbook.md`
  — 人間が読める障害手順書が存在し、「応急」節を含む (症状 → 判定 → 応急 → 恒久) こと。
  実測 rc=1 (docs/ に doppler-outage-runbook.md 未存在)。

verify は DoD の下限であって DoD そのものではない。verify が直接見ないもの —
(1) 演習が **kubectl-write で実際に適用された** NetworkPolicy によるものであること (紙上演習でない)、
(2) 既存 Secret の不変確認が「vaultwarden 等」複数対象で取られていること、
(3) 復旧後の再同期 (SecretSynced への戻り) までの時間が記録されていること —
は worker が drill-report.json と PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- クラスタは **k3s** (`nix/images/proxmox-cloud/configuration.nix`)。k3s は内蔵の network policy
  コントローラ (kube-router 由来) を持つため、追加コンポーネントなしに NetworkPolicy が効く
- ESO は Helm chart `external-secrets` 2.9.0、namespace `external-secrets`
  (`apps/external-secrets/kustomization.yaml`)。上流は ClusterSecretStore `doppler`
  (`cluster-secret-store.yaml`、トークンは同 namespace の Secret `doppler-token`) で、
  ESO は HTTPS (443) で `api.doppler.com` に取りに行く
- ExternalSecret は 15 ファイル・多数の namespace に散在 (vaultwarden / immich / coder / dex /
  autopilot / ops-health-reporter / telegram-adapter / syncthing / version-watcher /
  ops-dashboard / argocd / external-secrets)。`refreshInterval` は概ね 1h — つまり遮断しても
  何も起きずに 1 時間以上黙る可能性があり、「失敗すら起こらない停止」の仮説と整合する
- 一時的な観測オブジェクトを `ops/projects/logs/<id>/` に YAML で残す先例がある
  (P-0111 `cap-watch.pod.yaml`: Git 管理アプリではなく kubectl apply の一時 Pod)。
  演習用 NetworkPolicy もこの流儀に従い Git ツリー外の適用→削除で行う
- `report.py` は stdlib のみ・`collect(fn)` パターンの集約構造 (`applications` / `pod_issues` /
  `download_budget` 等の各セクション)。新規セクションはこの形に沿う。**ただし現行 RBAC
  (`apps/ops-health-reporter/rbac.yaml`) は `external-secrets.io` API group を読めない**ので、
  `externalsecrets` の get/list 追加が必須 (read-only verb のみ、T-0110 の閉じ込み方針は維持)。
  reporter CronJob は 30 分毎 (`cronjob.yaml`)
- 手順書の先例: `docs/sops-recovery.md` (SOPS 復旧)、P-0111 `root_cause.md`
  (一次原因と修繕手順を人間が読める形に書くスタイル)

### 作り方

1. **演習 (DoD 1)**: `external-secrets` namespace の ESO Pod (Helm 由来 label で選択) に向けた
   NetworkPolicy を kubectl-write で適用する。素の NetworkPolicy は DNS 名で絞れないため、
   (a) 適用前に `api.doppler.com` を名前解決して ipBlock で拒否するか、(b) default-deny egress +
   DNS/クラスタ内許可の形で Doppler 行きを実質遮断するかを worker が選ぶ (推奨は (b): IP 固定は
   演習窓内の IP 変動に脆い)。以後 30 分以上、`kubectl get externalsecrets -A -o json` の
   status.conditions を監視ループで回し、SecretSyncedError 初発までの秒数・既存 Secret
   (`kubectl get secret` の resourceVersion/updatedAt 相当) の不変・稼働中 Pod の生存を
   壁時計付きで記録する。**遮断中に Pod 再作成を 1 回起こす** (データ損失リスクの無い低影響対象を
   選ぶこと。例: 監視系ワークロードの rollout restart)。最後に NP を削除し、再同期完了
   (全 ExternalSecret が Synced に戻る) までを測って `drill-report.json` に纏める。
   キーは verify の要求どおり最低限 `blocked_minutes` / `recovery_verified` /
   `pod_recreate_result` (文字列) / `eso_error_first_seen_seconds` + 各事象のタイムスタンプ列
2. **runbook (DoD 2)**: `docs/doppler-outage-runbook.md` を演習の実測値を使って書く。
   症状 (何が静かに止まり、何が顔を出すか — 実測どおり) → 判定 (ExternalSecret の status と
   ESO ログのどこを見るか) → 応急 (**既存 Secret は消さない・アプリを再起動して救済しようとしない**
   等の実測に基づく指示) → 恒久 (Doppler 復旧確認の手順)。演習で判明したことを根拠に書き、
   推測を書かない (substrate.md 流儀)
3. **レポーター拡張 (DoD 3)**: `rbac.yaml` に `external-secrets.io` / `externalsecrets` の
   get/list を追加し、`report.py` に `collect_externalsecrets()` を `collect()` パターンで新設。
   載せるのは SecretSyncedError の件数と対象名、各 ExternalSecret の最終同期からの経過秒
   (静かな鮮度劣化を見える化するのが目的なので「Synced のまま古い」ものも見えるようにする)。
   1 namespace/1 収集の失敗で全体を止めない既有思想に従う

安全弁: 演習は `irreversible: false`。NP は `external-secrets` namespace の ESO Pod に限定し、
演習後必ず削除する。遮断しても既存 Secret と動中 Pod は公式仕様上影響を受けない — それを「仕様でなく
実測」にするのが本演習であり、万一予想外の崩れが出ても NP 削除 1 発で復旧する (これ自体も実測値)。

## やらないこと

- **Doppler 上流の冗長化・代替プロバイダ導入**。実測の結果「複数上流が必要」という結論が出たら
  それは別プロジェクト (1 PR 1 論点)。本案は実測と文書化まで
- **NetworkPolicy の恒久適用・Git 管理 (apps/ 配下への追加)**。演習用 NP は一時オブジェクト
  (P-0111 cap-watch 流儀)。常設 egress 制御を採用するかは実測結果を持って別論点にする
- **ESO 本体・chart version・refreshInterval の変更**。spec `touches_apps` が許すのは
  ops-health-reporter の report.py / rbac.yaml (+ kustomization の追加分) に限る
- **Secret の値の閲覧・記録・持ち出し**。扱うのは status・タイムスタンプ・件数のみ
  (T-0110「生ログを git 管理ブランチへ持ち出さない」の準用)
- **backlog.json / state.json / journal の編集**。autopilot 直接 push 領域でコンフリクトする (CLAUDE.md)
