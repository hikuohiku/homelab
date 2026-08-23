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

## session4 (worker)

やったこと: **受入 #3 の evidence 収取を試み、実機初回実行で障害を連鎖発見**。
2 件は自分の領域で修正済み (下記 障害 2/3)、1 件は外部要因で人間対応後に自然解消
(障害 1)。preview デプロイ (`kubectl apply -k apps/restic-check`) → ExternalSecret
Sync → `kubectl create job --from=cronjob/restic-check` の手順は実機で通し済み。

### 障害 1: B2 download_cap_exceeded (外部要因 — 実行中に解消)

- 初回の check が全リポジトリで `Stat(<config/>) … b2_download_file_by_name: 403`。
  クラスタ確認の結果、**昨夜 (2026-08-22 夜) の定期 backup が 5 本すべて失敗**
  (immich/coder は Failed、vaultwarden/syncthing は同エラーのリトライ後に fatal)。
  retention 4 本も「repository not initialized yet, skipping」で**実は初期化プローブが
  同一原因で失敗し、それを skip として正常終了に仮装していた** (Complete 表示は当てにならない)
- 診断 Pod から B2 API 直叩きで切り分け: append-only 鍵の capability は正常
  (`writeFiles,listFiles,readFiles,listBuckets` + namePrefix null)。download を
  直接再現すると **HTTP 403 `download_cap_exceeded`** — アカウントのダウンロード上限。
  解除は B2 コンソールの Caps & Alerts で人間が上げるしかない
- **実行途中 (~2026-08-23 00:00 UTC 頃) に誰かが上限を解除した**: 同一 Run 内で前半 3
  リポジトリは 403 で失敗、後半 2 リポジトリ (coder-workspace-homes / syncthing) は
  check rc=0 で完走。Run 後の API 再確認でも download 200。解除者・理由は不明 (heart 確認推奨)
- 影響: 全 5 リポジトリが 08-22 夜分の backup を欠落。次の backup 窓は 08-23 (月) 02:45 JST

### 障害 2: evaluate の results マウント readOnly → EROFS 即死 (自前 — commit 737745c0)

- Run #2 で job_main.py が `/work/results/*.json` へ書けず即死。判定どころか通知より前に
  落ちるため「何が起きたか」すら消える。session3 の煙霧試験は書き込み先がローカル tmp で、
  manifest 結合後の readOnly マウントを再現していなかったため未検出
- 修正: cronjob.yaml の evaluate 側 results マウントから `readOnly: true` を削除
- 教訓: **ConfigMap スクリプトの書き込み先と volume マウント属性の整合は、render ではなく
  実 Pod でしか検証できない**

### 障害 3: Discord 403 = Cloudflare error 1010 (python-urllib 既定 UA のブロック — runner 側修正済み c0ab0095)

- Run #3 はパイプライン完走 → 通知のみ `HTTP Error 403: Forbidden` で失敗
- 診断: webhook 値は有効 (UA を明示した POST は **204 成功**)。403 の本文は Discord では
  なく **Cloudflare error 1010 (ブラウザシグネチャブロック)** で、`Python-urllib/3.x`
  既定 UA が叩かれている。UA を何らかの製品トークンに変えるだけで通る
- 修正: `ops/restic_check_runner.py` post_discord() に UA 明示 + テスト 3 例追加
  (28 tests green)。apps 側コピーへ同期、sync check green
- **heart 側は未修復 (発見節へ)**: ops/heart/notify.py も素の urllib で、本日 09:16Z を
  最後に sent.jsonl が停止し、21:50Z 頃から outbox に滞留が発生している — 同一原因の可能性

### 実機で初めて証明できたこと (Run #3, 2026-08-23T00:01:10Z)

- パイプライン全体 (probe 直列検査 → staging → レコード組み立て → 判定 → レポート →
  EVIDENCE_JSON → 通知試行 → 非ゼロ終了) が設計どおり動く
- **鮮度警報が実データで初点灯**: coder-workspace-homes age=29.51h / syncthing age=29.1h
  を WARN 判定。「夜間 backup の静的失敗」をまさに検出した (プロジェクト why の実証)
- 失敗 Run の EVIDENCE_JSON は `check_evidence_20260823_incident.json` として保存
  (受入用の check_evidence.json は**わざと書いていない** — 非ゼロ混入を緑と偽らないため)

### verify 現状

- #1 grep: red のまま (BusyBox grep が --include 非対応。manifest 実体はあり —
  heart の判断待ち、session3 罠節のとおり)
- #2 unittest: **green (28 tests)**
- #3 evidence: 未達。あと 1 回の green Run で取れる (下記手順)

### 次セッションへの要点 (受入 #3 の green 化手順)

前提: 08-23 (月) 02:45–03:55 JST の backup 窓が**成功して**全 snapshot が 24h 以内に
なっていること (B2 cap は既に解除済みを実測。ただし再発するかもしれないので最初に
`kubectl get jobs -A | grep restic-backup` で昨夜の成否を確認すること)。

1. `kubectl apply -k apps/restic-check` (namespace は session4 の片付けで削除済みのため
   再作成される) → `kubectl get externalsecret -n restic-check` で 2 本とも SecretSynced 待ち
2. `kubectl create job --from=cronjob/restic-check restic-check-evidence -n restic-check`
   → B2 が健全なら完了は数十分 (cap 障害時のような 15 分×10 回のリトランはない)
   → `kubectl logs -n restic-check job/... -c evaluate` が overall=OK(0) になり、
   Discord には何も飛ばない (成功時黙る)
3. ログ最終行 `EVIDENCE_JSON ` 以降の配列をそのまま
   `ops/projects/logs/P-0102/check_evidence.json` へ保存 → 受入 #3 の python verify が
   green になることを実測
4. **片付け: `kubectl delete ns restic-check`** (merge 後の ArgoCD 導入時に未管理
   リソースが衝突するのを防ぐ。evidence は git に残る)

罠 (実測ずみ — 次セッションは踏まないこと):

- **`kubectl create job --from=` で作った Job には CronJob への ownerReference が付く**
  ので、元 CronJob を削除すると GC で Job も即消える (Run #1 をこれでロスト)。
  二重発火防止は削除でなく `suspend=true` patch で。ただし **`kubectl apply -k` を打ち直すと
  suspend patch は manifest どおり false に戻る** (次の週次発火は 7 日先なので実害は無い)
- secret の GET/LIST は RBAC で禁止されている (credential 分離が効いている)。
  webhook の値比較などは Pod 経由でのみ可能
- 手動 Job でも失敗時の Discord 通知は本番に飛ぶ (今回そうだった)。診断目的の反復実行は
  通知重複に注意

発見 (スコープ外、curriculum が拾うこと):

- **ops/heart/notify.py の discord POST が Cloudflare 1010 で失敗している可能性が高い**
  (runner と同一の素 urllib。sent.jsonl 最終 2026-08-22T09:16:44Z、outbox 滞留 21:50Z〜)。
  修復は runner と同じ UA 明示 1 行。heart 自身の障害通知が今この瞬間も静かに捨てられている
  可能性があり、優先度は高い
- retention CronJob が「初期化プローブ失敗」を "repository not initialized yet, skipping"
  として exit 0 で吞み込む。backup 全滅の晩に Complete 表示になるのは監視上の盲点
  (鮮度警報が替代検知になるが、retention 単体の異常は見えない)
- B2 の Caps & Alerts 上限到達は backup/retention/check を同時に全滅させうる。使用量の
  大きい操作 (restore drill 等) の後は cap 消費を織り込むべき (P-0080 系との接続点)

## checkpoint (予算上限)

このセッションは状態の書き残しだけを行った (実装・クラスタ操作はしていない)。
ワーキングツリーはクリーンで、commit も破棄も必要な変更は無かった。

### 受入チェックリストの現在地 (2026-08-23 実測)

| # | 状態 | 内容 |
|---|------|------|
| 1 grep | **red** (実装は完了済み) | manifest は `apps/restic-check/` に存在し `grep -rq 'restic-check' apps/` (--include 無し) は rc=0 実測。spec 文言どおりだと BusyBox grep が `--include` 非対応で必ず赤 — heart の判断待ち (session3 罠節、issue #56 済み) |
| 2 unittest | **green** | 本 checkpoint セッションで再実測: 28 tests OK |
| 3 evidence | **未達** | 失敗 Run の記録は `check_evidence_20260823_incident.json` (5 レコード、失敗混在 — 緑と偽らないため意図的に別名)。green Run 1 回で取れる状態まで届いている |

つまりコード・manifest 側は完成しており、残りは「実機での green Run 1 回」と
「heart による verify #1 文言判断」だけ。

### 止まっている場所と次の一手

止まっている場所: 受入 #3 の evidence 収取。session4 で障害 3 連鎖 (B2 cap / EROFS /
Cloudflare UA) をすべて解消済みで、次の Run が成功すれば evidence が取れる。
前提は **08-23 (月) 02:45–03:55 JST の backup 窓が成功していること** (08-22 夜分は
cap 全滅で欠落済み)。この checkpoint を読む worker は以下を順に実行すること
(session4「次セッションへの要点」の手順と同一):

1. `kubectl get jobs -A | grep restic-backup` — 昨夜の backup 成否を最初に確認
2. `kubectl apply -k apps/restic-check` → `kubectl get externalsecret -n restic-check`
   で 2 本とも SecretSynced 待ち
3. `kubectl create job --from=cronjob/restic-check restic-check-evidence -n restic-check`
   → `kubectl logs -n restic-check job/restic-check-evidence -c evaluate` が
   overall=OK(0) になることを確認 (成功時は Discord に何も飛ばない)
4. ログ最終行 `EVIDENCE_JSON ` 以降の配列をそのまま
   `ops/projects/logs/P-0102/check_evidence.json` に保存 →
   受入 #3 の python verify が green になることを実測
5. 片付け: `kubectl delete ns restic-check` (merge 後の ArgoCD 導入時の衝突防止。
   session4 罠節どおり Job は CronJob 削除で GC されるので namespace ごと消してよい)
6. PR を出す。PR 説明に (a) verify #1 は spec 文言のままだと BusyBox 環境で赤になる
   旨、(b) 鮮度 warn を exit 2 (通知対象) に拡張した設計判断、を明記すること

### 残った不確実性

- **append-only 鍵での check 完走はまだ一度も証明されていない** (Run #1–#3 は全て
  cap 障害 or 鮮度 warn で非ゼロ終了。check 自体の rc=0 は後半 2 リポジトリでのみ実績)。
  受入 #3 の green Run が初検証になる
- B2 download_cap の恒久対策は未決。08-22 夜の全滅は人手で解除されただけで、再発すれば
  手順 3 の Job も同じ死に方をする (その場合は session4 障害 1 節の診断手順で切り分け)
- verify #1 の文言修正可否 (issue #56 投げ済み) と、鮮度警報の exit 2 化という
  DoD 超過の設計判断の人間側受け入れ — どちらも未回答
- スコープ外発見の ops/heart/notify.py Cloudflare 1010 (UA 明示 1 行で直る) が
  修復されたか不明。未修復なら heart の障害通知が今も静かに捨てられている

### 継続の引き継ぎ (P-0116, 2026-08-23, human-pilot)

P-0102 は実装健全のまま soft cap を使い切って budget_exhausted で停止。
この P-0116 は全成果を引き継いだ継続 (予算 4M)。checkpoint と PROGRESS.md から再開。
