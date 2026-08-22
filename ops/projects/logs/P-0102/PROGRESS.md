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

## session3 (worker)

やったこと: **受入 #1 の manifest 一式を作成** (受入 #2 は session2 で green 済み)。

- `apps/restic-check/` 新設: namespace / ExternalSecret / CronJob / kustomization /
  application (ops-health-reporter 同型) + `apps/kustomization.yaml` 登録。
  CronJob `restic-check` は session2 計画どおり 2 コンテナ:
  initContainer `restic-probe` (restic/restic:0.19.1) が 5 リポジトリを直列で
  `check --read-data-subset=5%` + `snapshots --latest 1 --json`、本体 `evaluate`
  (python:3.14-alpine) が判定・通知。schedule は日曜 05:30 JST (backup 帯 02:45–03:55 /
  retention 帯 日曜 03:45–04:50 を避け prune 直後を検査)。backoffLimit 0 (再試行は
  B2 再読みと Discord 通知の重複だけ)、activeDeadlineSeconds 14400
- **session2 計画からの変更点 1 (ExternalSecret 4 本 → 1 本)**: 既存 4 namespace の
  backup 用 ExternalSecret はどれも同一 Doppler キー参照 (RESTIC_PASSWORD 等、
  リポジトリ違いはパス末尾だけ — docs/backup.md) なので、複製は 1 本で足りる。
  4 本作ると同じ値を持つ Secret の量産になるだけで分離の利点が無い。webhook 用
  (`restic-check-webhook` ← DISCORD_WEBHOOK_URL) と合わせ計 2 本
- **session2 計画からの変更点 2 (init→main の受け渡し)**: busybox sh には安全な JSON
  生成手段が無い (sed エスケープは restic 出力の制御文字で壊れうる)。そこで
  initContainer は生フィールド 3 ファイル `{repo}.check_rc` / `.snapshots_rc` /
  `.snapshots.out` を `/work/results/staging/` へ書くだけにし、本体の新スクリプト
  `apps/restic-check/job_main.py` が runner のレコード契約 JSON へ組み立ててから
  `restic_check_runner.main()` を呼ぶ。**runner 側の契約は 1 バイトも変えていない**
  (ops/restic_check_runner.py をそのまま ConfigMap へコピー。一致は新設の
  `ops/check_restic_check_script_sync.py` が CI で検査、ci.yml consistency checks に追加済み)。
  判定ロジックは job_main.py にも 1 行も置いていない
- 周辺帳簿の配線: `ops/check_credential_map.py` DECLARED_SECRET_TARGETS に新 Secret 2 種
  追加 (**この検査が実際に機能した** — 登録前に discover が赤で教えてくれた)。
  `ops/check_version_sync.py` restic GROUP に6ファイル目 + `ops/inventory.json` に
  `restic-check-restic-image` 追加 (いずれも P-0047 の前例どおり)

検証 (全部自分で実測):

- `kubectl kustomize apps/restic-check` rc=0 + `kubectl apply --dry-run=client` rc=0
  (エージェント環境に kubectl あり。render 検証に使える)
- パイプライン結合スモーク 3 方向: 全健全→rc0 / check 失敗混在→rc1 / staging 空滅
  (init 死に相当) →全リポジトリ MISSING_RC で rc1。EVIDENCE_JSON 行も期待形状
- 埋め込みシェルを `sh -n` 構文検査。CI 相当をローカル回線: unittest 25 + discover 93
  green / credential map / version sync / script sync / validate.py 0 error

罠 (未解決 — 人間 = heart の判断が必要):

- **受入 #1 は spec 文言どおり (`--include='*.yaml'` 付き) ではこの環境では永遠に緑に
  ならないことを実測確定した** (wrapper 実測 + 本セッション実測の両方で BusyBox grep が
  `--include` 非対応、rc=2 usage error。manifest の有無と無関係に必ず赤)。
  中身としては `grep -rq 'restic-check' apps/` (--include 無し) で rc=0 を実測 —
  「manifest が存在する」という受入の趣旨は満たしている。spec 文言は触っていない。
  heart に (a) verify コマンドを BusyBox 対応に直すか (b) wrapper を GNU grep にするか
  (c) 実質 green の無し版実測をもって妥協するかの判断を問いたい (issue #56 へ)

次セッションへの要点 (**受入 #3 evidence の取り方**):

- 前提: この CronJob がクラスタに居ること (PR merge → ArgoCD sync 後)。merge 前に
  取るなら `just preview` 経由で apps をこのブランチへ向ける。namespace `restic-check`
  の ExternalSecret 2 本が Sync 済み (= Secret 実体あり) になってから:
  `kubectl create job --from=cronjob/restic-check restic-check-evidence -n restic-check`
  → 完了待ち → `kubectl logs job/restic-check-evidence -c evaluate | grep '^EVIDENCE_JSON '`
  の後ろの JSON をそのまま `ops/projects/logs/P-0102/check_evidence.json` に保存
  (形状は evidence_records() が担保済み)
- この Job 実行が **append-only 鍵での check 初検証**そのもの。check は lock を取るので、
  lock 除去が hide マーカー経路で通るかの実証になる (backup/unlock では実績あり)。
  もし rc≠0 が出たらそれは破損ではなく「append-only 制約下での check 挙動の発見」なので
  PROGRESS に残すこと (安易な --no-lock 追加や削除鍵持ち込みは spec 違反)
- 手動 Job でも webhook 通知は本番 Discord に飛ぶ (失敗時のみ)。evidence 目的で
  失敗が予想される実験をするなら RESTIC_CHECK_WEBHOOK_URL を空にした上書き manifest で
- python イメージは 3.14-alpine にした (session2 メモの 3.12 より既存 pin 6 箇所との
  一致を優先)。pvc-usage GROUP へは登録していない — 単一ファイル単一箇所なので
  二重管理が発生していないため

発見 (スコープ外、curriculum が拾うこと):

- なし (credential map の fail-closed が新規 ExternalSecret を正しく拾ったのは
  機能が意図どおり働いているという好例であり、論点ではない)

## session4 (worker) — 中間報告

やろうとしたこと: **受入 #3 の evidence 収取** (受入 #1 は BusyBox grep 問題で heart 判断待ち、
#2 は session2 で green 済み)。session3 メモの preview 経路を実行した。

経過:

- `kubectl apply -k apps/restic-check` で preview 配備 (application.yaml は kustomization
  に含まれないので ArgoCD 側は無関係のまま)。ExternalSecret 2 本とも SecretSynced を確認。
- 手動 Job `restic-check-evidence` を起こしたところ、vaultwarden の check が
  `Stat(<config/>) … b2_download_file_by_name: 403` で延々リトライ。

**重大な発見 — 本プロジェクトの前提が崩れている (人間の対応が必要)**:

- クラスタ側を確認すると **昨夜の定期 backup が全滅している**: immich/coder は Failed
  (各 99m/74m リトライ後 fatal)、vaultwarden/syncthing は同エラーのリトライで Running 継続。
  エラーは手動 Job と完全に同一 (`create key in repository ... failed: Stat: 403`)。
- retention 4 本も「repository not initialized yet, skipping」で**実は初期化プローブが
  失敗してスキップされていただけ** (Complete 表示は当てにならない)。
- 診断 Pod から B2 API を直接叩いて切り分け:
  1. `b2_authorize_account` は成功し、append-only 鍵の capability は
     `writeFiles,listFiles,readFiles,listBuckets` + namePrefix null で docs/backup.md L459
     どおり**鍵は正常**
  2. `GET {downloadUrl}/file/{bucket}/config` (restic の Stat と同じ経路) が
     **HTTP 403 `download_cap_exceeded`** — 「download bandwidth or transaction (Class B)
     cap exceeded. See the Caps & Alerts page to increase your cap.」
- つまり **B2 アカウントのダウンロード上限に達してアカウント全体でダウンロード拒否中**
  (アップロードは別枠なので書き込みだけ生きている)。解除は B2 コンソールの Caps & Alerts
  で上限を上げるしかなく、リポジトリ側では直せない。原因の仮説としては最近の大容量
  ダウンロード (P-0080 の RTO 計測?) が上限を消費した可能性もあるが未検証 — heart が
  コンソールで使用量を確認すること
- **この状態は P-0102 の why (backup 静的失敗の検知) が現実に起きた瞬間**。昨夜分から
  backup が取れておらず、既存の監視では誰も気づけなかった。本機能の初回実行がそれを検出した
- 影響: **受入 #3 は人間が上限を引き上げるまで green 化不可能**。check_evidence.json は
  わざと書かない (非ゼロのレコードを書くのは受入の虚偽充足になるため)。失敗 Run の
  EVIDENCE_JSON は別名で事故記録として保存する (下記)

運用上の罠 (次セッション以降):

- **`kubectl create job --from=cronjob/X` で作った Job にはこのクラスタでは CronJob への
  ownerReference が付く**。元の CronJob を削除すると GC が手動 Job も即削除する
  (実際 1 回目の手動 Job をこれで消した)。手動 Job を使う間は CronJob を残すこと。
  週次スケジュールとの二重発火を避けるなら `suspend=true` に patch する (今回そうした。
  05:30 JST 発火予定だったため)。診断終了後は namespace ごと消すので suspend の戻しは不要
- 手動 Job 実行中でも webhook は生きているので、この失敗 Run 完了時に Discord へ
  incident 通知が 1 通飛ぶ (**設計どおりの初めての実通知**)。重複防止のため週次 CronJob は
  suspend 済み

次セッションへの要点:

- 前提: 人間が B2 の download cap を引き上げ済みであること (issue #56 / PR 説明で確認)。
  未解除なら何もできない — この項目は読み飛ばすこと
- `kubectl apply -k apps/restic-check` → ExternalSecret Sync 待ち →
  `kubectl create job --from=cronjob/restic-check restic-check-evidence -n restic-check`
  → 全 5 リポジトリ rc=0 で完走 (約 1〜2h。B2 からの実読み) → evaluate ログの
  `EVIDENCE_JSON ` 行を `ops/projects/logs/P-0102/check_evidence.json` に保存 →
  受入 #3 の python verify が green になることを実測 → **namespace ごと削除して片付け**
  (merge 後の ArgoCD 導入時に未管理リソースが衝突するのを防ぐ)
- check が lock を取って hide マーカーで外せるか (append-only 鍵での完走証明) は今回の
  失敗 Run では途中までしか検証できていない。lock 作成までは成功していた (403 はその後の
  config 読み取り)。cap 解除後の Run で改めて確認すること
