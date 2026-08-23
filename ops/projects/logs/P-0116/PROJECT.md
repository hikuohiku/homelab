# P-0102 — 毎晩書いて、一度も検査していない — 5 本の restic リポジトリに週次の健康診断と「最終成功からの経過時間」警報を常設する

## 目的

vaultwarden / coder-postgres / immich / coder-workspace-homes / syncthing の 5 リポジトリは
毎晩 append-only 鍵で B2 に書かれているが、`restic check` は一度も走っておらず、破損は
復元時 (P-0080 の RTO 計測や本当の事故) に初めて判る形になっている。append-only 鍵移行後の
check 挙動も未検証。さらに backup CronJob が黙って失敗し続けていても気づく仕組みが無い
(journal では各回が手動で mtime を確認していただけ)。seeds H1 系の「取れているだけの
バックアップを戻せるバックアップに」の下請け。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing** (2026-08-22、`project/p-0102`
の checkout、リポジトリルートから実行)。

- [ ] `grep -rq 'restic-check' apps/ --include='*.yaml'`
  — restic-check の CronJob manifest が apps/ 配下の YAML として存在すること (配線済みかは
    別検査だが、まず「無い」と同じ状態を撲滅する)。実測 rc=1 (該当なし)
- [ ] `python3 -m unittest ops.tests.test_restic_check_runner`
  — check runner の判定ロジック (鮮度算出・exit code 集約・evidence/レポート整形) に
    単体テストがあること。実測: モジュールが存在せず FAILED (errors=1)
- [ ] `python3 -c "import json; d=json.load(open('ops/projects/logs/P-0102/check_evidence.json')); assert len(d)>=5 and all(x['exit_code']==0 for x in d)"`
  — **実際の append-only 鍵**で全 5 リポジトリの check が rc=0 で完走した証拠 (5 件以上、
    全て exit_code==0) が残っていること。実測: ファイル未存在

## 設計方針

### 前提 (initializer が 2026-08-22 に実読した事実)

- 5 リポジトリの現在地は `b2:<RESTIC_B2_BUCKET>:{vaultwarden, immich, coder-postgres,
  coder-workspace-homes, syncthing}` (docs/backup.md 各節)。credential は namespace ごとに
  分かれた ExternalSecret: `vaultwarden-restic-backup-credentials` / `immich-restic-backup-credentials`
  / `syncthing-restic-backup-credentials` / coder のみ `coder-restic-backup-credentials` 1 本を
  coder-postgres と workspace-homes で共有。いずれも**削除鍵を持たない append-only 鍵のみ**
- k8s Secret は namespace を跨げない。単一 Job で 5 本を見るなら配置先 namespace に
  ExternalSecret を複製する形になる。ExternalSecretStore はクラスタスコープなので
  Doppler への新規登録は不要 (P-0047 の syncthing 追加が「新規発明ゼロ」だった前例、
  docs/backup.md §P-0047)
- append-only 鍵での lock 除去は B2 では hide マーカーになり機能する — ただしこれは
  backup/unlock で確認した話で、**check の完走は未検証**。これを実鍵で証明するのが
  受入 3 項目目 (docs/backup.md「append-only 鍵への切り替え」節)
- 既存 CronJob の image pin は全部 `restic/restic:0.19.1`。schedule は `spec.timeZone`
  未記載 = JST 評価 (ops/memory/substrate.md)。夜間帯は backup 02:45–03:55 /
  retention 日曜 03:45–04:50 JST で埋まっている → 週次 check はこの帯を避ける
- incident 通知の既存流路は heart の `ops/heart/notify.py` (`DISCORD_WEBHOOK_URL`) だけで、
  **CronJob から Discord への流路はまだ無い**。DISCORD_WEBHOOK_URL 自体は Doppler 既存キー
  (apps/autopilot/external-secret.yaml が参照)
- スクリプトを repo 側の実ファイルに置き ConfigMap 埋め込みとの一致を CI で見る前例が
  ある (`ops/check_pvc_usage_script_sync.py` + apps/*/pvc-usage-cronjob.yaml)。
  テストは unittest のみ (pytest は Job イメージに無い)。CI ops job は ubuntu-latest +
  python3 で helm/kustomize 無し
- 「PVC を宣言するアプリには restic backup CronJob がある」ことは既に
  `ops/tests/test_backup_coverage.py` (P-0047) が見張る。本プロジェクトのテスト対象は
  check 側 (鮮度判定・集約・整形) であり、coverage 検査の二重化はしない
- **B2/restic の credential はエージェント環境に無い** (substrate.md 実測)。受入 3 項目目の
  実鍵 proof はクラスタ内での一時 Job 実行でしか取れない (kubectl write は capability
  `kubectl-write` が宣言済み。CLI で行ってよいのは CLAUDE.md のルールどおり)

### 方針

1. 単一の週次 CronJob `restic-check` が全 5 リポジトリを直列に検査する。1 リポジトリの
   失敗で打ち切らず最後まで回してサマリを出し、非ゼロが 1 件でもあれば Job 全体も非ゼロ終了
2. 検査本体は `restic check --read-data-subset=<数>%`。鮮度は同じ Job 内で
   `restic snapshots --latest 1 --json` の timestamp から算出し、24h 超を warn 出力。
   coder-workspace-homes は host 単位世代管理なので全 host 中の最新 1 件を見ればよい
   (どれか 1 host でも新しければ nightly は生きている)
3. repo テーブル・鮮度計算・集約・evidence 整形などの判定ロジックは repo 側 python
   モジュールに置き `ops/tests/test_restic_check_runner.py` で固定 (test_backup_coverage.py
   の流儀: 純関数は合成入力で両方向)。manifest への埋め込み方法 (ConfigMap 直埋め +
   sync check 追加など) は worker が決める
4. 配置は新規小ディレクトリ (例: `apps/restic-check/` + application.yaml +
   apps/kustomization.yaml 登録、ops-health-reporter 同型) を推奨。namespace には
   4 種の append-only 鍵と DISCORD_WEBHOOK_URL の ExternalSecret を置く。失敗時のみ
   webhook POST で incident 通知し、成功時は黙る
5. レポートの永続先は Pod ログ (構造化した stdout) を正とし、新しい保存基盤を作らない。
   失敗は incident 通知で即時に人間へ届き、成功の痕跡はログで足りる — 足りなければ
   その時点で別プロジェクト

## やらないこと

- **復元試験 (restore drill) 本体**。P-0080 系の論点。このプロジェクトは「壊れていないこと・
  新鮮であること」の常設観測まで
- **既存 backup / retention CronJob の改修** (スケジュール・保持世代・鍵構成・スクリプト)。
  触れるのは check の追加のみ (1 PR 1 論点)
- **削除権限つき鍵 (retention 用 credentials) を check 側に持ち込む**こと。append-only 鍵
  だけで完走させること自体が受入の検証対象なので、楽をするために強い鍵を使わない
- **Prometheus / Alertmanager 等の監視基盤導入**。Discord webhook の既存流路で足りる
- **backup CronJob 自身の Job 失敗 (pod crash 等) の即時検知**。24h 鮮度警報が代替する。
  即時性が必要になったら別プロジェクトで論点を分ける
