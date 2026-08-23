# P-0128 — 2 日前に backup を全滅させた犯人は B2 の download cap — cap を消費するのは誰かを帳簿につけ、上限近傍で鳴る計器を既存経路に植える

## 目的

P-0111 root_cause.md が確定した一次原因は Backblaze B2 のアカウント単位 download cap 超過
(`download_cap_exceeded`。usage counter は毎日 00:00 UTC リセット) で、2026-08-22 に backup 子 Job
全滅 → ArgoCD Degraded の実害を出し、08-23T00:04Z に日次リセットで自然回復した。だが **cap の消費内訳を
測る者はまだいない**。採択済みの週次健康診断 (P-0102/P-0116, `restic check --read-data-subset`) や
隔離復元試験 (P-0114/115) はいずれもダウンロードを食う使い方で、帳簿と計器なしに稼働させれば
同じ事故を自分の手で再生する。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**
(2026-08-23、`project/p-0128` の checkout で、リポジトリルートから実行)。

- [ ] `python3 -m unittest ops.tests.test_download_budget`
  — ダウンロード推定量の集計ロジック (直近 N 日の集計・月次見積もり・閾値判定) が
  unit test として存在し、通ること。
  実測 rc=1 (`ModuleNotFoundError: No module named 'ops.tests.test_download_budget'`。テストモジュール未存在)。
- [ ] `grep -q 'download_budget' apps/ops-health-reporter/report.py`
  — health レポート (`latest.json`) に `download_budget` が載る配線が report.py に入っていること。
  実測 rc=1 (該当文字列なし。report.py の収集キーは applications/pod_issues/pvcs/nodes/
  pod_metrics/node_metrics/pvc_usage/autopilot のみ)。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した。調べ直さなくてよい)

- 一次原因の実名と修繕手順は `ops/projects/logs/P-0111/root_cause.md`。cap は **アカウント単位・鍵の種類に
  無関係・毎日 00:00 UTC リセット** (公式ドキュメント引用 + cap-watch Pod 実測)。超過日は 08-10 と 08-22 の
  みで 08-11〜21 は健全。**消費者は未特定** (root_cause.md「オープンな疑問」。候補: 土曜夜に一斉稼働する
  週次 retention 4 本、人間の B2 コンソール利用、クラスタ外の消費者)
- 配線の定石は pvc-usage-reporter: 各 namespace の CronJob が ConfigMap `pvc-usage-report` に書き戻し、
  report.py の `collect_pvc_usage()` (`apps/ops-health-reporter/report.py:203`) が読んで `pvc_usage` キーで
  latest.json / history jsonl に載せる。reporter の RBAC は configmaps get が resourceNames
  `["pvc-usage-report"]` に絞られている (`apps/ops-health-reporter/rbac.yaml`)
- **report.py は他 namespace の pods/log を読めない** (pods/log は autopilot ns に閉じた Role のみ,
  T-0110)。だから「backup Job ログから転送量を拾う」なら、拾う主体は各 namespace 側の Job/CronJob
  (ConfigMap 書き戻し) でなければならない。restic backup CronJob は immich / vaultwarden / coder-postgres /
  coder-workspace-homes / syncthing の 5 リポジトリ分あり、restic 自身が標準出力に転送統計を出す
  (DoD の「restic の転送統計または Job ログ」はここから取る)
- 警報の受け口について: **`ops/briefing/` モジュールはまだ存在しない** (P-0096 採択済み・未納品、logs 無し)。
  既存の流路は heart が review_needed を積む `briefing-queue.jsonl` (`ops/heart/heart.py:405`) と
  notify.py の digest。「器を太らせる前に使い切る」(VISION) — 新しい通知機構は作らず既存経路に乗せること。
  選んだ経路と理由を PROGRESS.md に残す
- cap の実値 (download bandwidth / Class C transactions の具体的な数値) は B2 コンソールにしかなく repo には無い。
  「docs 化されている範囲」とは root_cause.md が引用した公式仕様 (アカウント単位・日次リセット・2 種類の cap) の
  ことで、閾値は定数として設定可能な形に持ち、docs 外の実値には依存しない

### 決めてあること

- **集計は namespace 側、表示は health ブランチ側。** 既存の backup/健康診断 CronJob の枠内 (スクリプト追記 or
  同じ ConfigMap への追加キー) で推定量を積み、report.py が `download_budget` キーで集約して
  latest.json + history jsonl に載せる。verify #2 はこの配線を見張る
- 新しい外部 credential は要求しない (DoD 明示)。B2 の usage API を叩く方向には進まない
- DoD(3) の削減候補: 現在稼働している download 消費者は日次 backup 5 本 (repo open 時の config/index 読み) と
  週次 retention (prune 時の index 再読み込み)。週次健康診断・復元試験は未稼働だが採択済みで、将来の最大消費者に
  なる — 「メタデータだけで足りる箇所」の特定はまずこの未来の消費者への帳簿面からの指摘になる。減らせなかったら
  理由を logs に書く (DoD の許容)

## やらないこと

- **B2 コンソールでの cap 引き上げ・Caps & Alerts の実値確認**。管理コンソール作業 = 人間専有
  (CHARTER §4)。必要になったら needs-human 化して依頼文言を整えるところまで
- **P-0102/P-0116 (週次健康診断)・P-0114/P-0115 (隔離復元試験) の実装そのもの**。それらの cap 消費を帳簿に
  載せ、設計上の配慮を返すまで (1 PR 1 論点)
- **backup CronJob のスケジュール・保持世代・retention 方針の変更**。日次バックアップは単一障害点であり
  触らない。削減は「メタデータだけで足りる箇所」の特定と実証にとどめる
- **消費者特定の捜査そのもの** (B2 コンソール統計画面の読み取り等、repo 外の証拠収集)。帳簿 (推定量の内訳) を
  付けて近づけるところが本プロジェクトの範囲
