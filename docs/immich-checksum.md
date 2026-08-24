# immich アセット整合性検証 (checksum) — P-0361

immich は「写真の実データを持つ唯一のサービス」で、ホスト側のファイルが黙って壊れる
(bit rot・誤書き込み) と人間の唯一の原本が静かに死ぬ。この文書は immich v3.0.0 導入の
整合性検証ジョブを毎週走らせ、原本の腐りを検出して報告する常設装置 (P-0361) の
API 形・手順・根拠の記録。

- 判断ロジック: `ops/tools/immich_checksum_check.py` (`--selftest` で同梱 fixture 自己検証可)
- 産出側 CronJob: `apps/immich/checksum-cronjob.yaml`
- 集約側: `apps/ops-health-reporter/report.py` の `collect_checksum()`
- 進捗・実測値: `ops/projects/logs/P-0361/PROGRESS.md`

## 文書の書き方

この文書の「**上流ソースで確定**」は immich v3.1.0 相当の上流ソース
(`server/src/controllers/integrity-admin.controller.ts` / `server/src/enum.ts` /
`server/src/config.ts` 等) を 2026-08-24 に実読して確定した内容。
「**実機確認待ち**」はクラスタ内の実機でまだ確かめていない内容。

API 形・挙動は実測 (実機または上流ソース) でしか書かない方針 (P-0035 の流儀)。
**クラスタ内の実機で確認するまで、この装置は「動作中の産出装置」ではなく
「配置済みの未検証装置」である。** 実機で確かめたら PROGRESS.md に記録し、
この文書の実機確認待ちを実測値に書き換えること。

## 何をどう測るか (全体像)

週次 CronJob (`apps/immich/checksum-cronjob.yaml`) が immich の整合性検証ジョブ
(`integrity-checksum-mismatch`) を管理 API でトリガーし、完了を待って
`GET /api/admin/integrity/summary` の ChecksumFail 件数を専用 ConfigMap
`immich-checksum-report` の `report.json` に書き戻す。ops-health-reporter の
`collect_checksum()` がそれを読み、latest.json の `checksum` 節に載せる
(「産出 CronJob → 専用 ConfigMap → reporter が集約」の既存パターン。
`pvc-usage-report` / `download-budget` / `dashboard-smoke` と同じ形)。

```
週次 CronJob (immich ns)
  └─ immich API: POST /api/jobs {"name": "integrity-checksum-mismatch"}
       └─ 完了待ち: GET /api/queues/integrityCheck (active==0 かつ waiting==0)
       └─ 結果: GET /api/admin/integrity/summary → checksum_mismatch
  └─ ConfigMap immich-checksum-report の report.json に書く
       └─ ops-health-reporter: latest.json の checksum 節へ集約
```

## API 形

| 項目 | 内容 | 確定度 |
|---|---|---|
| トリガー | `POST /api/jobs` body `{"name": "integrity-checksum-mismatch"}` → 204 | 上流ソースで確定 (`ManualJobName.IntegrityChecksumFiles`、enum.ts 424 行) |
| 完了検知 | `GET /api/queues/integrityCheck` → `statistics.active==0` かつ `waiting==0` | 上流ソースで確定。**実機の挙動は確認待ち** |
| 結果 | `GET /api/admin/integrity/summary` → `{checksum_mismatch, missing_file, untracked_file}` (3 項目とも非負 int) | 上流ソースで確定 (`IntegrityReportSummaryResponseDto`)。**実機レスポンスの実測は確認待ち** |
| 認証 | ヘッダ `x-api-key` (admin API キー) | 上流ソースで確定 (`ImmichHeader.ApiKey`、enum.ts 19-20 行。auth.service.ts が受ける) |
| 途中停止 | `integrityChecks.checksumFiles.timeLimit` (既定 1h) / `percentageLimit` | 上流ソースで確定。**実機の設定値は確認待ち** |
| 再開点 | `system_metadata` の `integrity-checksum-checkpoint` | 上流ソースで確定。**実機の再開挙動は確認待ち** |

### 完了検知の実装 (checksum_runner.py)

IntegrityCheck queue は concurrency 1 (config.ts 既定) で、ジョブは 1 本ずつ実行される。
トリガー直後はキューに載る前に一瞬 `active=0` に見えることがあるため、
`wait_for_checksum_run()` は「一度 `active>0` を観測してから `active==0 && waiting==0`」
で完了とみなす。小規模ライブラリでポーリング間 (30s) に完了して active を観測できない
場合は、トリガー前の完了カウンタ baseline からの `completed` 増加、または一定時間
(ポーリング 3 回分) キューが空のまま経過した場合も完了とみなす。

### summary の意味 (重要)

`checksum_mismatch` は「**現時点で ChecksumFail と記録されているアセット数**」。
ジョブは再検証で合格したファイルの report を削除するため、これは「検出済みの腐り」の
現在値であって「全アセットの検査結果」ではない — **未走査のアセットの腐りはまだ
検出されない**。timeLimit による途中停止・checkpoint 再開で複数回の run に分かれる (下記)。

### 途中停止 / checkpoint 再開

checksum ジョブは `integrityChecks.checksumFiles.timeLimit` (既定 1h) /
`percentageLimit` に達すると途中停止し、`system_metadata` の
`integrity-checksum-checkpoint` に再開点を残す。次の run は checkpoint から再開するため、
**1 回の run が全アセットを覆うとは限らない**。大規模ライブラリ (上流実例で 33k assets /
750GB) では 1 回 run が 1 時間超になる。

v3.0.1 には checkpoint 再開が 0 件処理のまま「全カバー」と嘘をつく既知バグがある
(issue #29487 / PR #29516)。v3.1.0 に修正が含まれる見込み (上流確認) だが、
**実機の挙動で確認すること** (実機確認待ち)。

## 実装の詳細

### 産出側 (apps/immich/checksum-cronjob.yaml)

- ServiceAccount `immich-checksum` + Role (自 namespace の configmaps に
  get/create/update のみ。最小形。`pvc-usage-reporter` と同じ) + RoleBinding
- ConfigMap `immich-checksum-script` (2 キー):
  - `immich_checksum_check.py`: 判断ロジックの正。`ops/tools/immich_checksum_check.py` の
    正確な複製。drift は `ops/check_immich_checksum_script_sync.py` が CI で検出
  - `checksum_runner.py`: immich API を叩く産出側 (トリガー → queue ポーリングで完了待ち
    → summary → ConfigMap 書込み)。失敗時は status=error の代役レコードを書く
    (dashboard-smoke の rc=2 と同じ思想)
- 出力先 ConfigMap `immich-checksum-report` は manifest に宣言しない (ArgoCD 管理外。
  selfHeal との綱引きを避ける。`dashboard-smoke` / `pvc-usage-reporter` と同じ形)
- API キー: `apps/immich/immich-api-key-external-secret.yaml` (ExternalSecret、
  Doppler の `IMMICH_API_KEY` 参照)。**人間が Doppler に登録するまで CronJob Pod は
  起動しない** (restic の B2 鍵と同型)
- CronJob: schedule `30 5 * * 0`、concurrencyPolicy Forbid、
  `RUN_TIMEOUT_S=7200` / `activeDeadlineSeconds=7800`、image `python:3.14-alpine`
  (stdlib のみ。immich API 呼び出しは urllib)

### スケジュールの根拠

- CronJob の schedule は node01 では JST 評価 (`ops/memory/substrate.md`)。
  日曜 05:30 JST = 土曜 20:30 UTC。
- immich 内蔵の integrity checksum cron は既定 enabled (03:00 UTC, timeLimit 1h —
  **実機の system config で確認すること**) と、restic backup (02:45 UTC) / retention
  (03:45 UTC) から離した。
- 週次トリガーと内蔵日次 cron が二重実行になっても害はない (queue concurrency 1 で
  直列化、checkpoint 継続)。
- `RUN_TIMEOUT_S=7200` / `activeDeadlineSeconds=7800` は既定 1h の timeLimit 前提。
  **実機で timeLimit を確認し、大きければここも上げること** (実機確認待ち)。

### 集約側 (apps/ops-health-reporter)

- `report.py` の `collect_checksum()` が ConfigMap `immich-checksum-report` の
  `report.json` を読み、latest.json の `checksum` 節に載せる。status は産出側が判定済みの
  ok / fail / unconfigured / error をそのまま通す (集約側で再判定しない。判定の正は
  `ops/tools/immich_checksum_check.py`)。
- 産出側未稼働・記録破損 (JSON でない・dict でない・status 未知値) は例外にせず
  no_data で正直に出す (未知 status は「検出ゼロ」と「帳簿の壊れ」を区別できるよう
  no_data に落とす)。
- `apps/ops-health-reporter/rbac.yaml` の reader ClusterRole configmaps `resourceNames` に
  `immich-checksum-report` を追加済み。

### 閾値と incident 経路 (DoD 3)

不一致検出時の incident 閾値は `ops/rules.json` の `checksum.mismatch_threshold` が
**唯一の宣言元**。CronJob の `MISMATCH_THRESHOLD` env にこの値を渡し、
`ops/check_version_sync.py` の GROUPS が両者の同期を機械的に検査する
(rules.json と env の食い違いは CI で落ちる)。

- 2026-08-24 時点の実値は `1` (検出したら即 incident)。原本 (人間の唯一の写真データ)
  の腐り検出が目的のため、黙って 0 や 2 を決め打ちせず 1 にした。誤通知が気になる
  場合は PR で上げる (rules.json は人間レビュー必須パス)。
- report の status は `checksum_mismatch >= mismatch_threshold` で fail になる
  (`ops/tools/immich_checksum_check.py` の `judge_mismatch`。この判定の正は実機で
  確定すること)。
- fail / error は latest.json の `checksum` 節から heart の `checksum_alert()`
  (ops/heart/facts.py) が拾い、briefing-queue.jsonl への追記と incident 通知に乗せる。
  同じ status の同一日内の再通知は cursors の `checksum_alert` 記録で落とす
  (download-budget / dashboard-smoke と同じ流儀)。
- env を外す (または rules.json から消す) と report の status が `unconfigured` を
  正直に返し、heart は鳴らさない (budget の unconfigured と同じ判断)。

### 判定と報告の契約 (report.json)

産出側が書く report.json の形 (`immich_checksum_check.build_report` / `error_report`):

```json
{
  "generated_at": "YYYY-MM-DDTHH:MM:SSZ",
  "namespace": "immich",
  "status": "ok | fail | unconfigured | error",
  "reason": "…",
  "ok": true,
  "checksum_mismatch": 0,
  "missing_file": 0,
  "untracked_file": 0,
  "job": { "name": "integrity-checksum-mismatch", "triggered_at": "…", "run_elapsed_s": 123 }
}
```

- status の意味: `ok` 不一致ゼロ / `fail` 不一致が閾値以上 (原本の腐りを検出) /
  `unconfigured` 閾値未設定 / `error` 産出側自身の失敗 (代役レコード)
- `job` は成功時のみ載る。`run_elapsed_s` が所要時間の実測値 (DoD 5)

## 既知の罠・注意

- **1 回の run が全アセットを覆うとは限らない** (checkpoint 再開)。summary は
  「現時点の検出済み腐り」で、未走査分は含まれない
- **immich 内蔵の checksum cron は既定 enabled** (03:00 UTC, timeLimit 1h)。
  実機の system config で有効/無効・timeLimit を確認すること
- CronJob の `.spec.template` は immutable。マニフェスト差し替え時は
  `argocd.argoproj.io/sync-options: Force=true,Replace=true` が要る場合がある
  (`ops/memory/substrate.md`)。既存の immich CronJob には付いていない
- ConfigMap は ArgoCD 管理外 (manifest に宣言しない)。宣言すると selfHeal が毎回
  書き戻し、CronJob の更新と綱引きになる
- 一時ファイルは `mktemp` を使う (固定パス `/tmp/…` は前セッションの残骸を拾う。
  `ops/memory/substrate.md`)

## 実機で確認すること (未確認項目)

このサンドボックスには tailscale / kubectl が無いため、以下は実機での確認待ち。
確認したら PROGRESS.md に記録し、この文書の該当箇所を実測値に書き換えること。

1. **Doppler に `IMMICH_API_KEY` を登録する (人間)**。immich の管理 UI で admin が
   API キーを発行し、Doppler に登録する。登録まで CronJob Pod は起動しない
   (ExternalSecret が SecretSyncedError)
2. **system config の確認**: `integrityChecks.checksumFiles.timeLimit` の実値と、内蔵
   checksum cron の有効/無効。timeLimit が `RUN_TIMEOUT_S` (7200s) を超えるなら
   CronJob の env / `activeDeadlineSeconds` も上げる
3. **CronJob を 1 回実行する**:
   ```
   kubectl -n immich create job --from=cronjob/immich-checksum immich-checksum-manual-$(date +%s)
   kubectl -n immich logs -l job-name=immich-checksum-manual-... -f
   ```
   対象アセット数・所要時間 (`job.run_elapsed_s`)・結果 (checksum_mismatch 件数) を
   PROGRESS.md に残す (DoD 5)
4. **結果の確認**:
   ```
   kubectl -n immich get configmap immich-checksum-report -o jsonpath='{.data.report\.json}'
   # 集約後 (latest.json の checksum 節):
   kubectl -n autopilot get configmap ops-health-report -o jsonpath='{.data.latest\.json}' | jq .checksum
   ```
5. **checkpoint 再開の挙動を確認する** (v3.1.0 で issue #29487 / PR #29516 の修正が
   効いているか)。途中停止後の再トリガーで 0 件処理のまま「全カバー」と嘘をつかないこと
6. **実機の summary レスポンスが上記の形と一致することを確認する**。食い違ったら
   `ops/tools/immich_checksum_check.py` の fixture を実測値で更新する
   (定数と実機が食い違ったら、実測値を根拠に fixture ごと修正すること)
   ```
   python3 ops/tools/immich_checksum_check.py --summary summary.json --threshold 1
   ```

## 参照

- `ops/projects/logs/P-0361/PROJECT.md` — 仕様・設計方針・やらないこと
- `ops/projects/logs/P-0361/PROGRESS.md` — セッション記録と実測値
- immich 上流: `server/src/controllers/integrity-admin.controller.ts` /
  `server/src/enum.ts` / `server/src/config.ts` (v3.1.0 相当)
- 既知バグ: immich issue #29487 / PR #29516 (v3.0.1 の checkpoint 再開)