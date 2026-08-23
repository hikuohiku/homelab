# P-0258 — 復旧の実証を一回の勇気から毎晩の時計へ — 捨ててもいい canary を夜ごとに壊し、ArgoCD が何秒で生き返らせるかを health 履歴に積む

## 目的

「node01 が今日消えたら」「ArgoCD が止まったら」系の復旧演習 (P-0054/P-0080/P-0164) は
どれも大掛かりで棄却・停滞が続いた。この spec は対象を**捨ててよい canary 1 本に極小化し**、
1 回のイベント証明ではなく毎晩 1 点ずつ復旧所要秒を health 履歴に積む常時の計器を作る。
VISION の「ループが止まらないこと」の実測裏付けであり、inventory policy manual の
argocd-chart 更新の前後比較にも使える。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0258` の checkout・リポジトリルートで実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `kubectl kustomize apps | grep -q 'name: recovery-canary'`
  — canary 一式が root Kustomization (`apps/kustomization.yaml`) の render に載り、
  ArgoCD が実際に管理対象として認識すること。実測 rc=1 (一致無し)。
- [ ] `python3 -m unittest ops.tests.test_recovery_probe_parse -v`
  — 計測記録のパース/要約契約が unittest で固定され green であること。
  実測 rc=1 (モジュール未存在)。
- [ ] `git show origin/ops-health-report:ops/health/latest.json | python3 -c "import json,sys; d=json.load(sys.stdin); r=d.get('recovery_probe'); assert r and isinstance(r.get('last_recovery_seconds'), int), r"`
  — health レポートに `recovery_probe` キーが搭載され、`last_recovery_seconds` が int で
  載ること (履歴 `ops/health/history/*.jsonl` も同じ report dict の 1 行)。実測は
  `recovery_probe: None` で assert 失敗。

## 設計方針

### 前提 (initializer が 2026-08-23 にコード読解で確認。調べ直さなくてよい)

- **産出側/集約側の分離パターンがある**: 先例は download-budget (P-0128) と dashboard-smoke
  (P-0193)。産出 CronJob は**専用 ConfigMap の `report.json` キー**に書き、reporter が読む。
  pvc-usage-report への追加キー書き込みは不可 (既存 writer が PUT で data 全体置換するため、
  report.py:236 のコメント参照)。reporter 側では
  (a) `apps/ops-health-reporter/rbac.yaml:31` の configmaps get resourceNames に追加、
  (b) `apps/ops-health-reporter/report.py` に `collect_recovery_probe()` と main() report dict
  へのキー追加、(c) notes 文言 (他キーの説明と同型。夜間 Degraded を誤報しない注記含む)
  の 3 点セットが要る
- **テストの型**: ops/tests/test_download_budget.py — 単一ファイルの純関数モジュールを
  importlib でロードする。report.py 自体は import 時に ServiceAccount token を読むため
  cluster 外からロードできず、パース/要約の純関数は別ファイルに分離するのが確立済みの流儀
  (`test_recovery_probe_parse` という spec のモジュール名もこの分離を指す)
- **アプリの雛形**: apps/version-watcher/ (namespace.yaml + cronjob.yaml +
  kustomization.yaml + application.yaml)。Application を `apps/kustomization.yaml` へ登録すると
  root App of Apps が拾う。CronJob の schedule は `spec.timeZone` 未指定だと **JST** 評価
  (substrate「Kubernetes」節)。夜間実行なら schedule を JST 深夜に置く
- **計測対象の隔離 (DoD 3)**: canary Deployment (pause コンテナ 1 本) 専用 namespace に置く。
  CronJob 用 SA の権限は自 namespace 内の当該 deployment の get/delete と結果書き込み用
  ConfigMap に絞り、resourceNames で対象名まで固定する。pause コンテナ
  (registry.k8s.io/pause) は何もしないので、削除が本体アプリへ波及する経路が manifest 上で
  存在しない形にする
- **計測の定義**: delete 呼び出しから、ArgoCD selfHeal による再作成後に Pod が Ready に
  戻るまでの秒。スクリプトは k8s API poll で判定し、ConfigMap に
  `{generated_at, last_recovery_seconds(int), ...}` を書く。ArgoCD 既定の refresh 間隔が
  計測値に乗るが、それごとが「ループの速さ」なので正しい値
- **意図的な health 揺れを誤報させない**: ArgoCD v3.2.1 は live の Job 失敗を Application
  health に反映する (appTree、substrate「観測経路」節)。canary アプリの health は夜間に
  短時間 Degraded/Progressing になるのが仕様。report.py の notes にその旨を書き、autopilot が
  CHARTER §2 の健全性チェックで誤検知しないようにすること
- **縛る値への制約**: memory limits は実測裏付けなしに付けない (T-0055)。pause の消費は
  極小だが、limits を付けるなら一次ソースでの裏付けを取り、根拠コメントを残す
  (version-watcher/cronjob.yaml の前例)

### 作り方

1. **canary アプリ (DoD 1, 3)**: `apps/recovery-canary/` に namespace / canary Deployment /
   削除+計測 CronJob / RBAC / kustomization / application.yaml を置き、root kustomization に登録
2. **計測記録 (DoD 2)**: 専用 ConfigMap `recovery-probe` の `report.json` キーへ書く
   (manifest には事前作成しない — dashboard-smoke と同じく selfHeal との競合回避)。
   パース/要約の純関数を reporter 側の単一ファイルモジュールに切り出し、
   reporter RBAC + collect 関数 + notes を足す
3. **テスト**: ops/tests/test_recovery_probe_parse.py — 記録の契約 (last_recovery_seconds が
   int、壊れた記録は例外ではなく no_data/stale 扱い等) を fixture で固定
4. **受入 verify を実行して green 化**し、最初の夜間 run の結果を PROGRESS.md に実測として残す

## やらないこと

- **ArgoCD 自身・sync policy・refresh 間隔の変更** — 測定器の側を変えない。chart 更新の
  前後比較が目的なので、測定系を固定したままにする
- **node01 全滅・ArgoCD 全停止などの大掛かりな演習** — P-0054/P-0080/P-0164 系の演目。
  この spec は canary 1 本に極小化した差分であり、そこへ戻らない
- **本体アプリ (immich/vaultwarden 等) へ触れる変更** — canary は専用 namespace に完全隔離。
  「影響がないこと」は manifest 上の分離で示す
- **履歴の可視化・グラフ化・ダッシュボード表示** — recovery_probe を latest.json/history に
  積むまで。見せ方は別論点
- **復旧遅延時の通知/alerting 新設** — まずは記録。異常検知への接続は次の論点で起票
- **ops-health-reporter の CronJob 間隔や既存キーの改修** — 触るのは rbac resourceNames 追加、
  collect 関数追加、notes 文言の 3 点のみ
