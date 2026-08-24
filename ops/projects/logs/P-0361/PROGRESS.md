# P-0361 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## セッション記録

### 2026-08-24 (worker #1) — verify 項目 2 を green にした (ops/tools/immich_checksum_check.py)

**やったこと**: DoD (4) の `ops/tools/immich_checksum_check.py` を新規作成し、
`--selftest` を同梱 fixture 8 件で通した。CI 用に `ops/tests/test_immich_checksum_check.py`
(23 テスト) も追加。

**分かったこと (上流ソースを実読した。クラスタ内の実機では未確認)**:
- immich の整合性検証の API 形を v3.1.0 相当の上流ソースから確定した
  (`server/src/controllers/integrity-admin.controller.ts` / `enum.ts`)。
  - トリガー: `POST /api/jobs` body `{"name": "integrity-checksum-mismatch"}` (admin, 204)
  - 結果: `GET /api/admin/integrity/summary` → `{"checksum_mismatch": N, "missing_file": N, "untracked_file": N}`
  - ジョブ名 `integrity-checksum-mismatch` (enum `ManualJobName.IntegrityChecksumFiles`)、
    report type `checksum_mismatch` (enum `IntegrityReport.ChecksumFail`)。認証は `x-api-key`
  - checksum ジョブは `integrityChecks.checksumFiles.timeLimit` / `percentageLimit` で
    1 run が途中停止し、`system_metadata` の `integrity-checksum-checkpoint` に再開点を
    残す。v3.0.1 の checkpoint 再開バグ (issue #29487, PR #29516) は後続リリースで修正済み
    (v3.1.0 に含まれる見込み。**実機で挙動を確認すること**)
- 受入検証項目 2 は green: `test -f ops/tools/immich_checksum_check.py &&
  python3 ops/tools/immich_checksum_check.py --selftest` → rc=0。
  `python3 -m unittest discover -s ops/tests -t .` → 556 テスト全部 green。
- **実機 (クラスタ) への到達手段がこのサンドボックスに無い** (tailscale/kubectl なし)。
  DoD 1 の API 実測・DoD 5 の実走は次以降のセッション (wrapper 側の環境依存)。

**次への一言**:
1. 残り verify 3 件: (1) `apps/immich/checksum-cronjob.yaml` + kustomization、
   (3) `docs/immich-checksum.md`、 (4) `apps/ops-health-reporter/report.py` に checksum 集約。
2. CronJob を作るときはこのツールの純関数 (parse_summary / judge_mismatch / build_report)
   をスクリプト ConfigMap に埋め込み、drift 検出 (check_pvc_usage_script_sync 型) を考える。
3. 閾値は rules.json の `checksum.mismatch_threshold` を唯一の宣言元にする (DoD 3)。
   ツール側は `DEFAULT_MISMATCH_THRESHOLD = None` で「未設定 = unconfigured」を出す設計。
4. `--summary <file> --threshold N` モードが実機確認の橋渡しになる (summary JSON を
   解釈して report を表示)。

### 2026-08-24 (worker #2) — verify 項目 1 を green にした (checksum-cronjob.yaml + kustomization)

**やったこと**:
- `apps/immich/checksum-cronjob.yaml` を新規作成 (ServiceAccount + Role + RoleBinding +
  ConfigMap `immich-checksum-script` + CronJob)。スクリプト ConfigMap は 2 キー構成:
  - `immich_checksum_check.py`: `ops/tools/immich_checksum_check.py` の**正確な複製**
    (drift 検出 `ops/check_immich_checksum_script_sync.py` を CI 配線済み)
  - `checksum_runner.py`: immich API を叩く産出側 (トリガー → queue ポーリングで完了待ち
    → summary → `immich-checksum-report` ConfigMap の report.json へ書込み)。
    失敗時は status=error の代役レコードを書く (dashboard-smoke の rc=2 と同じ思想)
- `apps/immich/immich-api-key-external-secret.yaml`: Doppler の `IMMICH_API_KEY` (admin
  API キー) を参照。**人間が Doppler に登録するまで CronJob Pod は起動しない**
  (restic の B2 鍵と同型)
- `apps/immich/kustomization.yaml` の resources に 2 ファイル追加
- `ops/check_immich_checksum_script_sync.py` + `ops/tests/test_immich_checksum_script_sync.py`
  (13 テスト、うち API フローを mock で通す 5 件)。`ci.yml` の consistency checks に配線
- `ops/check_credential_map.py` の `DECLARED_DOPPLER_KEYS` / `DECLARED_SECRET_TARGETS` に
  `IMMICH_API_KEY` / `immich-api-key` を追加

**分かったこと (immich v3.1.0 上流ソースを実読して確定)**:
- トリガー: `POST /api/jobs` body `{"name": "integrity-checksum-mismatch"}` → 204
  (`ManualJobName.IntegrityChecksumFiles` = 'integrity-checksum-mismatch'、enum.ts 424 行)
- 完了検知: `GET /api/queues/integrityCheck` → `statistics.active==0`。IntegrityCheck queue は
  concurrency 1 (config.ts 既定)。**トリガー直後は一瞬 active=0 に見える**ため、一度
  active>0 を観測してから active==0 && waiting==0 で完了とみなす。**小規模ライブラリでは
  ポーリング間に完了して active を観測できない** → queue の完了カウンタ (baseline からの
  completed 増加) でも判定する (実装済み)
- 結果: `GET /api/admin/integrity/summary` → `{checksum_mismatch, missing_file, untracked_file}`
  (IntegrityReportSummaryResponseDto。3 項目とも非負 int)
- 認証: ヘッダ `x-api-key` (`ImmichHeader.ApiKey`、enum.ts 19-20 行。auth.service.ts が受ける)
- **重要な挙動**: checksum ジョブは `integrityChecks.checksumFiles.timeLimit` (既定 1h) /
  percentageLimit で途中停止し、`system_metadata` の `integrity-checksum-checkpoint` に
  再開点を残す。**1 回の run が全アセットを覆うとは限らない** (checkpoint から再開で複数回
  に分かれる)。summary は「現時点で ChecksumFail と記録されているアセット数」で、再検証で
  合格したファイルの report は削除される — **未走査のアセットの腐りはまだ検出されない**。
  この点は docs (verify 3) に明記すること
- **immich 内蔵の checksum cron は既定で enabled** (03:00 UTC, timeLimit 1h) — 実機の
  system config で有効/無効・timeLimit を確認すること。週次トリガーが二重実行になっても
  害はない (queue concurrency 1 で直列化、checkpoint 継続)
- schedule は日曜 05:30 JST (`30 5 * * 0`、JST 評価)。土曜 20:30 UTC = immich 内蔵の日次
  03:00 UTC と restic (02:45/03:45 UTC) から離した
- `RUN_TIMEOUT_S=7200` / `activeDeadlineSeconds=7800` は既定 1h の timeLimit 前提。
  **実機で timeLimit を確認し、大きければここも上げること**

**受入検証**: 項目 1 を自分で実測 → green
(`test -f apps/immich/checksum-cronjob.yaml && grep -q 'checksum-cronjob.yaml' apps/immich/kustomization.yaml`)。

**次への一言**:
1. 残り verify 2 件: (3) `docs/immich-checksum.md`、(4) `apps/ops-health-reporter/report.py`
   の checksum 集約 + `rbac.yaml` の resourceNames に `immich-checksum-report` を 1 行足す。
   report.py の `collect_checksum()` は ConfigMap `immich-checksum-report` の report.json を
   読み、status は ok/fail/unconfigured/error (代役レコード) をそのまま載せる契約
2. **DoD 3**: rules.json に `checksum.mismatch_threshold` を追加し、CronJob の
   `MISMATCH_THRESHOLD` env を設定する。現状は未設定で report の status が unconfigured を
   正直に返す (ツールの DEFAULT_MISMATCH_THRESHOLD=None 設計どおり)
3. **DoD 5 / 実機**: Doppler の `IMMICH_API_KEY` 登録 (人間) → CronJob を走らせ、対象
   アセット数・所要時間・結果を PROGRESS に残す。system config の timeLimit と内蔵 cron の
   有効/無効も確認すること
4. 実機で checkpoint 再開が v3.1.0 で正しく動くか (issue #29487 / PR #29516 の修正) を確認する

## 発見

- (2026-08-24, worker #2) immich の内蔵 checksum cron は既定 enabled (03:00 UTC, timeLimit 1h)。
  実機の system config で有効性と timeLimit を確認すること。
- (2026-08-24) サンドボックスに pip / ruff が無い。CI 側 (`ops/`) の ruff F821 は
  CI 実行時にしか確認できない。Python の実行テスト (unittest + selftest) はローカルで全通。