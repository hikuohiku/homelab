# P-0102 — 進捗ログ

## session1 (initializer)

PROJECT.md を作成。受入チェックリストの 3 項目とも現時点で failing を実測済み
(2026-08-22)。実装は後続の worker セッションで開始すること (initializer は実装しない)。

worker への要点: 実鍵での evidence はクラスタ内の一時 Job でしか取れない
(B2 credential はエージェント環境に無い — substrate.md)。設計の前提と方針は
PROJECT.md「設計方針」を読むこと。

## session2 (worker)

やったこと: 受入 #2 を green 化。

- `ops/restic_check_runner.py` 新設: 判定ロジックの単一ソース。REPOS 表 (5 リポジトリ)・
  RFC3339 パーサ (restic はナノ秒小数を出すので自前で切る)・鮮度算出・集約・
  evidence/report/incident 整形・`main()`。テストは `ops/tests/test_restic_check_runner.py`
  25 ケース (test_backup_coverage.py 流儀: 合成入力両方向)。
- 自分での verify 実測: `python3 -m unittest ops.tests.test_restic_check_runner` → green。
  `discover -s ops/tests` 全体も 93 テスト green (既存に影響なし)。
- 終了コード契約: **0**=全健全 / **1**=check 失敗 or レコード欠落 or REPOS 表に無い
  リポジトリの混入 / **2**=check は全部成功だが鮮度 24h 超または snapshot 取得不能。
  非ゼロで webhook 通知、成功時は黙る。鮮度も「警報」対象にした (DoD の文言は warn 出力
  だけだが、warn がどこにも届かないのは backup 静的失敗の検知という why を満たさないため)
- レコード契約: クラスタ側シェルがリポジトリごとに 1 ファイル
  `{"repo", "check_rc", "snapshots_rc", "snapshots_json"}` を `RESTIC_CHECK_RESULTS_DIR`
  (デフォルト `/work/results`) へ JSON で書く。レポート最終行が
  `EVIDENCE_JSON <配列>` — 受入 #3 は Job ログからこの行を切り出して
  `check_evidence.json` に保存すればよい (形状は evidence_records() が担保)

次セッションへの要点 (**受入 #1 manifest をやるとき**、この契約を崩さないこと):

- restic バイナリと python は 1 イメージに入らない (ランタイム apk 追加は vaultwarden
  前例で拒否済み)。→ **initContainer (restic/restic:0.19.1) が restic ループ + レコード
  書き出し、main コンテナ (python:3.12-alpine) が ConfigMap マウントの
  /scripts/restic_check_runner.py を実行する 2 コンテナ構成**にすること。
  initContainer は失敗しても必ず exit 0 で抜ける (失敗はレコードに入れる。欠落は判定側が
  赤くする設計済み)。emptyDir (/work/results) で受け渡し
- credential は namespace 越え不可 → 新 namespace に ExternalSecret 4 本を複製
  (`vaultwarden-restic-backup-credentials` / `immich-…` / `syncthing-…` /
  coder 共有 `coder-restic-backup-credentials`)。envFrom は同名キー衝突するので
  **secretKeyRef ではなく secret をリポジトリ名ごとのパスにボリュームマウント**して
  シェルで export
- webhook は `RESTIC_CHECK_WEBHOOK_URL` ← Doppler `DISCORD_WEBHOOK_URL`
  (ClusterSecretStore なので新規登録不要)
- schedule 提案: 日曜 JST 05:30 頃。backup 帯 02:45–03:55 と retention 帯 日曜
  03:45–04:50 を避ける (PROJECT.md 前提節)。retention 直後だと prune 後の状態を検査できる
- image pin は既存 CronJob 同様 `restic/restic:0.19.1` と `python:3.12-alpine`

罠 (未解決 — 人間の判断が必要):

- **受入 #1 のコマンドはこの実行環境では BusyBox grep のため `--include` 非対応で、
  manifest を作っても usage エラー rc=2 のまま緑にならない可能性が高い**
  (wrapper 実測 + 本セッション実測どちらも `grep: unrecognized option:
  include=*.yaml`、BusyBox v1.37.0)。中身としては `grep -rq 'restic-check' apps/`
  (--include 無し) なら rc=0 になることを確認済み。次セッションは (a) manifest 作成、
  (b) --include 無し版での green 実測、(c) この制限を PR 説明に明記、までやること。
  spec 文言は heart の領分なので触らない

発見 (スコープ外、curriculum が拾うこと):

- なし (今回の手を動かした範囲では)。強いて挙げれば「verify コマンドが GNU 拡張オプション
  を含むと BusyBox 環境の wrapper で永遠に赤くなる」という仕組み上の論点は上の罠節参照
