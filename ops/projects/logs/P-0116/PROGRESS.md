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

## session5 (P-0116 worker, 2026-08-23)

やったこと: **受入 #3 の green Run を成立させ、evidence を保存した**。
`ops/projects/logs/P-0102/check_evidence.json` 新規作成 (5 レコード全 exit_code==0)、
受入 #3 の python verify はローカル実測 rc=0。受入 #2 も再実測 green (28 tests)。

### その前に起きたこと (前提が崩れていた)

checkpoint の前提「昨夜の backup 窓が成功している」は**崩れていた**。実測:

- 昨晩の窓 (08-23 02:45–04:00 JST = 08-22 17:45Z 頃) は coder-postgres / immich /
  syncthing / vaultwarden の **4 リポジトリが 403 で失敗** (coder は DeadlineExceeded、
  他 3 はリトライ後に fatal — session4 障害 1 と同一签名)。coder-workspace-homes
  だけは成功していた (cap 解除のタイミングか、読み量の差)
- 最終成功が ~30h 前 → 鮮度警報 (>24h) が発火する状態で、このまま evidence 収取は
  不可能だった
- 対処: **既存 CronJob と同一スクリプトの手動 Job で 4 リポジトリを手動 backup**
  (`kubectl create job --from=cronjob/<name> ... -n <ns>`、vaultwarden をカナリアに
  して B2 生死を確認してから残り 3 本)。全部 rc=0・snapshot 保存ずみ。
  append-only への追記のみで非破壊。B2 download 200 実測 (session4 の cap 解除が生きている)
- 手動 backup / 手動 spawner の Job は片付け済み。chb-* 子 Job は TTL 1h で自然 GC

### 発見 1: workspace-home-backup の spawner が子 Job の失敗を吞み込む

`coder-workspace-home-backup` CronJob の本体は `spawn_backup_jobs.py` (実 backup は
`chb-<workspace-id>` 子 Job、**ttlSecondsAfterFinished=3600 で自動 GC**)。
今朝の窓では子が全滅 (403) しても **spawner 本体だけが Complete 表示**になり、
子の失敗痕跡は 1 時間後に消える。「retention が初期化プローブ失敗を吞み込む」の
同型盲点。**本プロジェクトの鮮度警報がこれを実際に検出した** (Run #4 の
workspace-homes age=30.19h WARN — プロジェクト why の 2 例目の実証)。curriculum が
拾うべき論点: spawner が子 Job の結果を待って反映する設計への改修。

### 発見 2: P-0080 restore drill が immich リポジトリに stale lock を残していた

Run #4 で immich だけ check_rc=11。診断 Pod (通知の飛ばない素 restic check) で切り分け:
**`repository is already locked by PID 8 on restore-drill-immich-library-cj7pg`
(lock 生成 2026-08-22T14:37Z, 10h 超 前)**。drill Pod は既に存在しない (= stale 確定)。

- check は排他 lock を取るので失敗するが、backup は共有 lock なので**成功し続ける**。
  「夜間 backup は黙って成功し check だけが死ぬ」状態だった。夜間 backup 成功と
  check 失敗の乖離は lock 疑え
- lock 保持者の死亡確認 (pod/job/cronjob 全空) の上で、append-only 鍵で
  `restic unlock` → **"successfully removed 2 locks"** (stale drill 分 + 計 2 件目の
  正体不明 lock。こちらも unlock 後 list locks は空で、稼働中の lock ではなかった)。
  unlock 後の `restic check --read-data-subset=1%` が **rc=0** — append-only 鍵での
  check/unlock 完走はこれが初実績 (hide マーカー経路の再裏取り)
- curriculum が拾うべき論点: restore drill は終了時に lock を解放しない & 専用 repo
  コピーを使わない設計だと本番 repo に stale lock を撒く。drill 側の後始末 or
  `--unlock`/lock TTL の常設化

### Green Run (restic-check-evidence2, 2026-08-23T00:59:33Z)

```
overall=OK(0)
vaultwarden / immich / coder-postgres / coder-workspace-homes / syncthing
すべて check=rc0 freshness=ok (age 0.18–0.41h)
```

- Discord 通知なし (成功時黙る契約どおり)。Job ログ最終行 EVIDENCE_JSON を
  `ops/projects/logs/P-0102/check_evidence.json` へ保存 → verify #3 rc=0 実測
- 片付け: `kubectl delete ns restic-check` 済み (apps/kustomization.yaml への配線は
  済んでいるので、merge 後 ArgoCD が管理を引き取る。未管理リソース衝突は回避)
- Run #4 (失敗) と診断 Pod ×2 は Discord 通知を飛ばさない構成で実施したが、
  Run #4 自体は evaluate が CHECK-FAILURE(1) で終わったため **本番 Discord に 1 通
  飛んでいる** (immich rc=11 + homes 鮮度 WARN の内容。誤報ではなく実障害の検出なので問題なし)

### verify 現状 (このセッション終了時点)

- #1 grep: wrapper 環境では red のまま (BusyBox grep --include 非対応。manifest 実体は
  `apps/restic-check/` にあり、`grep -rq 'restic-check' apps/` なら rc=0 実測)。
  **heart による spec 文言判断待ち (issue #56)** — コード側に直すものは無い
- #2 unittest: **green (28 tests)** 再実測ずみ
- #3 evidence: **green**。`check_evidence.json` 保存、python verify rc=0 実測

### 次セッションへの要点

- 受入 3 項目のコード側作業は**完了**。残りは heart の verify #1 文言判断のみ
  (issue #56 に回答が来ているか確認すること)。回答次第で spec 側修正が入るだけで、
  worker 側の追加実装は不要なはず
- wrapper が verify を回すとき、#1 は BusyBox 環境では文言どおりだと赤になる点を
  PR 説明にもう一度明記すること
- 今晩 (08-24 02:45 JST 窓) の定期 backup が通常どおり成功すれば鮮度は保たれる。
  cap 再発に備え、次の週次発火 (日曜 05:30 JST) の前に B2 Caps & Alerts の恒久対策
  (download_cap 上限引き上げ or alert 設定) が人間側で決着していると望ましい

## session6 (P-0116 worker, 2026-08-23)

やったこと: **main への rebase** (validate.py error 解消) と受入状況の再確認。
コード・manifest 側の追加作業は不要だったことを再確認したセッション。

### issue #56 の確認結果

verify #1 文言判断への回答は**まだ来ていない** (コメント全 176 件を API で時系列確認。
最後のプロジェクト関連は `ack P-0102 (継続として再採択済み)` 2026-08-23T00:24:16Z)。

### 発見: 分岐後の main 先行で validate.py が赤 → rebase で解消

- ローカル CI 相当で `python3 ops/validate.py` が
  「error: archive.jsonl: origin/main の内容と先頭一致しない」を出した
- ブランチ側の帳簿破損ではなく、**merge-base (5c9a1c73) 以降に origin/main が先行して
  いたことだけが原因** (curriculum 採択・P-0111/P-0103/P-0088/P-0107 の merge で
  `ops/projects/archive.jsonl` が +18 行)。validate.py は「ブランチ側の archive.jsonl が
  origin/main の内容で始まること」(ops/validate.py:418 の startswith) を要求するので、
  追従していない古いブランチは内容が健全でも必ず赤になる
- 対処: P-0107 の前例 (`19336fd5 P-0107: worker #2 — main rebase ...`) にならい
  `git rebase origin/main` 実行。コンフリクト予測は `ops/inventory.json` のみで、
  実際も双方が別位置への要素追記 (branch: restic-check-restic-image /
  main: openclaw-bridge-image) だったため自動解決、両要素の共存を実測
- 教訓: **寿命の長いプロジェクトブランチ (継続で 1 日超) は、push 前に必ず main 追従を
  自分で確認する**。validate.py の archive 検査は「rebase 忘れ」を機械的に教えてくれる

### rebase 後の再実測 (全 green, 2026-08-23T01:0xZ)

- validate.py: **0 error** (11 warning は既存の heart 領分で変化なし)
- 受入 #2: `unittest ops.tests.test_restic_check_runner` **28 tests OK**
- discover 全体: `unittest discover -s ops/tests` **177 tests OK rc=0**
  (session2 時点の 93 から増加 — main 側の新規テスト群を取り込んだため。出力中の
  ResourceWarning / ::error:: 行は openclaw bridge・sops 系テストが意図的に流す
  stderr であり失敗ではない)
- 受入 #3 python verify: rc=0 再実測
- script sync / credential map / version sync: 全 rc=0

### heart 向け材料 (verify #1 の置換候補 — 判断は heart のまま)

BusyBox grep 対応の等価コマンド 2 案 (どちらもこの環境で rc=0 を実測):

- `grep -rq 'restic-check' apps/` — --include 削除の最小差分。YAML 以外のファイルも
  拾いうるが、誤検知の方向が安全側 (manifest が有るのに無いと判定される方向ではない)
- `find apps/ -name '*.yaml' -exec grep -q 'restic-check' {} \;` — YAML 限定の趣旨を保持

### verify 現状

- #1 grep: **red のまま** (spec 文言 × BusyBox grep --include 非対応は変化なし。
  manifest 実体は `apps/restic-check/` にあり、--include 無し版は rc=0 実測ずみ)。
  heart 判断待ち — コード側に直すものは無い
- #2 unittest: **green (28 tests)** 再実測
- #3 evidence: **green** 再実測

### 次セッションへの要点

- コード側の作業は**完全に完了**。唯一の未達 (verify #1) は heart/spec 側の判断待ちで、
  worker 側にできることは無い (issue #56 の回答確認だけ)
- wrapper が verify を回すとき、#1 は文言どおりだと BusyBox 環境では赤になる点を
  PR 説明に必ず明記すること (session5 からの繰り返し依頼)
- push 前には `git fetch && git log HEAD..origin/main --oneline` で main 先行を確認し、
  先行していたら rebase → validate.py 0 error を確認してから push すること
- 今晩 (08-24 02:45 JST 窓) の定期 backup 成功が鮮度維持の前提。B2 cap 恒久対策は
  人間側課題のまま (P-0080 依頼への直接回答は #56 で未確認)

## session7 (P-0116 worker, 2026-08-23)

やったこと: issue #56 の回答再確認 (なし) と受入 3 項目の全再実測。コード・manifest 側の
変更は無し (session6 結論の再確認セッション)。**wrapper 向けに重要な push 上の発見 1 件あり**

### issue #56 の確認結果

全 176 件を API で再取得。最新は 2026-08-23T00:24:16Z `ack P-0102` のままで、
verify #1 文言判断への回答は**まだ来ていない** (session6 から変化なし)。
open PR もまだ無い (wrapper が作る段階)。

### 受入再実測 (2026-08-23 本セッション)

- #1 spec 文言どおり: **rc=2** (BusyBox grep unrecognized option) — red のまま
- #1 `--include` 無し等価版 `grep -rq 'restic-check' apps/`: **rc=0**
  (`apps/restic-check/` 実体ありを再確認)
- #2: **28 tests OK**
- #3: evidence ok (**5 repos, 全 exit_code==0**)
- `ops/validate.py`: **0 error / 11 warning** (既存 warning のみ)

### 発見: origin/project/p-0116 と履歴分岐 — wrapper の push は --force-with-lease 必須

- session6 の rebase で local HEAD は履歴書き換え済み。現在 remote とは
  `[ahead 69, behind 10]` の分岐状態 (fetch 済み実測)
- remote tip は rebase 前の古いハッシュ (1082b3e1「受入 #3 の green Run 成立」) で、
  session6 の rebase commit (a3a9173f) は**remote 未到達**
- 通常 push は non-fast-forward で拒否される → **wrapper は
  `git push --force-with-lease origin project/p-0116` を使うこと**
- 内容面の損失は無い: local HEAD = origin/main の全コミット + 本プロジェクト作業で、
  remote ブランチの内容の上位互換 (`git diff origin/project/p-0116 HEAD -- . ':!ops/projects'`
  の差分は main 先行分のみ)
- 教訓: rebase による履歴書き換えをしたら、その時点で次の push は force 系になる。
  分岐検知は `git status -sb` の ahead/behind 表示で一目で判る

### verify 現状

- #1 grep: **red のまま** (heart 判断待ち。コード側に直すものは無い — session5/6 と同じ結論)
- #2 unittest: **green (28 tests)**
- #3 evidence: **green**

### 次セッションへの要点

- 変化なし: コード側は完全に完了。#1 の唯一の未達は issue #56 の heart 回答待ち
  (回答が来ていたら文言判断に従うだけ)
- **push は `--force-with-lease` で** (上記「発見」参照)。通常 push だと拒否されて
  セッション成果が remote に届かない
- PR 作成時は verify #1 が BusyBox 環境では文言どおりだと赤になる点を説明に必ず明記
  (session5 からの繰り返し依頼)
- push 前に `git fetch && git log HEAD..origin/main --oneline` で main 先行を確認するのは継続

## session8 (P-0116 worker, 2026-08-23)

やったこと: issue #56 の回答再確認 (**なし**)・open PR の確認 (0 件)・受入全項目と
validate.py の再実測。コード・manifest 側の変更は無し (session5→8 まで 4 回連続で
同じ結論)。**wrapper 向けに PR 説明の貼り付け用文案を本節に用意した** — もう各セッションで
書き直さなくてよい。

### issue #56 / PR の確認結果

- コメント全 **176 件** (API page 送り実測)。最新は 2026-08-23T00:24:16Z `ack P-0102`。
  verify #1 文言判断への回答は**未着** (session7 から変化なし)
- open PR: **0 件** (`GET /pulls?state=open` 実測。wrapper はまだ PR を作っていない)

### 受入再実測 (2026-08-23 本セッション)

- #1 spec 文言どおり: **rc=2** (BusyBox grep `unrecognized option`) — red のまま
- #1 等価版 `grep -rq 'restic-check' apps/`: **rc=0** / `find apps/ -name '*.yaml' | xargs grep -q 'restic-check'`: **rc=0**
- #2: **28 tests OK**
- #3: evidence ok (**5 repos, 全 exit_code==0**)
- `ops/validate.py`: **0 error / 11 warning** (既存 warning のみ)
- main 先行: **なし** (`git log HEAD..origin/main` 空。rebase 不要)。
  remote 分岐は `[ahead 70, behind 10]` (session7 の +1 = session7 コミット分。push は
  引き続き **`--force-with-lease`**)

### wrapper 向け: PR 説明 貼り付け用文案

```markdown
## P-0116 — restic 5 リポジトリの週次健康診断 (P-0102 継続)

受入 verify の実測値:

| # | コマンド | 結果 |
|---|---------|------|
| 1 | `grep -rq 'restic-check' apps/ --include='*.yaml'` | **rc=2 — ただし偽陰性ではない** |
| 2 | `python3 -m unittest ops.tests.test_restic_check_runner` | OK (28 tests) |
| 3 | evidence check (`check_evidence.json` ≥5 repos, exit_code==0) | OK |

verify #1 について: manifest 実体は `apps/restic-check/` に存在し
(`grep -rq 'restic-check' apps/` → rc=0)、**赤の原因は検査環境が BusyBox grep
で `--include` 非対応なことだけ** (オプション解釈段階で usage error、ファイルを
見る前に死ぬ)。置換候補 2 案を PROGRESS.md session6 節に提示済み。
issue #56 で判断を仰い中だが回答未着。等価性の実測は session5–8 の各記録参照。

append-only 鍵での実機 check 完走の証拠は
`ops/projects/logs/P-0102/check_evidence.json` (5 repos, 全 exit_code==0)。
```

### 環境メモ (次セッションの罠避け)

- この runner には `gh` CLI が無い → GitHub API は python urllib で (User-Agent 必須、
  無いと 403)。curl も疎通する
- `/tmp/opencode` は読み取り専用マウントで書き込めない。`/tmp` 自体と `mktemp -d` は
  使用可 — 一時ファイルは素直に `mktemp` で

### 次セッションへの要点

- 変化なし: コード側は完全に完了。#1 のみ heart 回答待ち (#56)。回答が来ていたら
  文言判断に従うだけ。来ていなければ再実測して本節に上書き追記で足りる
- push は **`--force-with-lease`** (session7 発見の継続)、push 前の main 先行確認は継続
- PR 作成時は上の「貼り付け用文案」を使えば BusyBox 注記漏れはない

## session9 (P-0116 worker, 2026-08-23)

やったこと: issue #56 の回答再確認 (**なし**)・open PR の確認 (0 件)・受入全項目と
validate.py の再実測。コード・manifest 側の変更は無し (session5→9 まで 5 回連続で
同じ結論。session8 の「貼り付け用文案」「環境メモ」は引き続き有効)。

### issue #56 / PR の確認結果

- コメント全 **177 件** (API page 送り実測)。session8 から +1 だが追加分は
  2026-08-23T01:23:30Z の P-0118 (Telegram 疎通依頼) で本プロジェクトとは無関係。
  全文を `P-0116` / `--include` / `restic-check` / `BusyBox` で走査し該当 **0 件** —
  verify #1 文言判断への回答は**未着のまま**
- open PR: **0 件** (`GET /pulls?state=open` 実測)

### 受入再実測 (2026-08-23 本セッション)

- #1 spec 文言どおり: **rc=2** (BusyBox grep `unrecognized option`) — red のまま
- #1 等価版 `grep -rq 'restic-check' apps/`: **rc=0** (`apps/restic-check/` 実体あり)
- #2: **28 tests OK**
- #3: evidence ok (**5 repos, 全 exit_code==0**)
- `ops/validate.py`: **0 error / 11 warning** (既存 warning のみ)
- main 先行: **なし** (`git log HEAD..origin/main` 空、rebase 不要)。
  remote 分岐は `[ahead 71, behind 10]` (session8 +1 分 = 本 session9 コミット除く)。
  push は引き続き **`--force-with-lease`**

### 次セッションへの要点

- 変化なし: コード側は完全に完了。#1 のみ heart 回答待ち (#56)。回答が来ていたら
  文言判断に従うだけ。来ていなければ再実測して追記で足りる (session8 の文案・環境メモ
  もそのまま使える。環境メモ: gh CLI 無し → python urllib + User-Agent、
  /tmp/opencode は読み取り専用 → mktemp を使う)
- push は **`--force-with-lease`**、push 前の main 先行確認 (`git log HEAD..origin/main`)
  は継続

## session10 (P-0116 worker, 2026-08-23)

やったこと: issue #56 の回答再確認 (**なし**)・open PR の確認 (**1 件だが P-0118
で本プロジェクト無関係**)・受入全項目と validate.py の再実測。コード・manifest 側の
変更は無し (session5→10 まで 6 回連続で同じ結論。session8 の「貼り付け用文案」
「環境メモ」は引き続き有効)。

### issue #56 / PR の確認結果

- コメント **177 件** (session9 から増減なし)。全文を `P-0116` / `--include` /
  `restic-check` / `BusyBox` で走査し該当 **0 件** — verify #1 文言判断への回答は
  **未着のまま**
- open PR: **1 件** (#512, `project/p-0118` — Telegram 疎通)。本プロジェクトのものは**無し**

### 受入再実測 (2026-08-23 本セッション)

- #1 spec 文言どおり: **rc=2** (BusyBox grep `unrecognized option`) — red のまま
- #1 等価版 `grep -rq 'restic-check' apps/`: **rc=0** / `find apps/ -name '*.yaml'
  | xargs grep -q`: **rc=0** (`apps/restic-check/` 実体あり)
- #2: **28 tests OK**
- #3: evidence ok (**5 repos, 全 exit_code==0**)
- `ops/validate.py`: **0 error / 11 warning** (既存 warning のみ)
- main 先行: **なし** (`git log HEAD..origin/main` 空、rebase 不要)。
  remote 分岐は `[ahead 72, behind 10]` (session9 +1 分 = 本 session10 コミット除く)。
  push は引き続き **`--force-with-lease`**

### 次セッションへの要点

- 変化なし: コード側は完全に完了。#1 のみ heart 回答待ち (#56)。回答が来ていたら
  文言判断に従うだけ。来ていなければ再実測して追記で足りる (session8 の文案・環境メモ
  もそのまま使える。環境メモ: gh CLI 無し → python urllib + User-Agent、
  /tmp/opencode は読み取り専用 → mktemp を使う)
- push は **`--force-with-lease`**、push 前の main 先行確認 (`git log HEAD..origin/main`)
  は継続

## session11 (P-0116 worker, 2026-08-23)

やったこと: issue #56 の回答再確認 (**なし**)・open PR の確認 (#512, P-0118 のみで
本プロジェクト無関係)・受入全項目と validate.py の再実測。コード・manifest 側の
変更は無し (session5→11 まで 7 回連続で同じ結論。session8 の「貼り付け用文案」
「環境メモ」は引き続き有効)。

### issue #56 / PR の確認結果

- コメント **177 件** (session10 から増減なし)。全文を `P-0116` / `--include` /
  `restic-check` / `BusyBox` で走査し該当 **0 件** — verify #1 文言判断への回答は
  **未着のまま**
- open PR: **1 件** (#512, `project/p-0118` — Telegram 疎通)。本プロジェクトのものは**無し**

### 受入再実測 (2026-08-23 本セッション)

- #1 spec 文言どおり: **rc=2** (BusyBox grep `unrecognized option`) — red のまま
- #1 等価版 `grep -rq 'restic-check' apps/`: **rc=0** / `find apps/ -name '*.yaml'
  | xargs grep -q`: **rc=0** (`apps/restic-check/` 実体あり)
- #2: **28 tests OK**
- #3: evidence ok (**5 repos, 全 exit_code==0**)
- `ops/validate.py`: **0 error / 11 warning** (既存 warning のみ)
- main 先行: **なし** (`git log HEAD..origin/main` 空、rebase 不要)。
  remote 分岐は `[ahead 73, behind 10]` (session10 +1 分 = 本 session11 コミット除く)。
  push は引き続き **`--force-with-lease`**

### 次セッションへの要点

- 変化なし: コード側は完全に完了。#1 のみ heart 回答待ち (#56)。回答が来ていたら
  文言判断に従うだけ。来ていなければ再実測して追記で足りる (session8 の文案・環境メモ
  もそのまま使える。環境メモ: gh CLI 無し → python urllib + User-Agent、
  /tmp/opencode は読み取り専用 → mktemp を使う)
- push は **`--force-with-lease`**、push 前の main 先行確認 (`git log HEAD..origin/main`)
  は継続
