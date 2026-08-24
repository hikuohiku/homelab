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

### 2026-08-24 (worker #3) — verify 項目 4 を green にした (report.py checksum 集約 + rbac)

**やったこと**:
- `apps/ops-health-reporter/report.py` に `collect_checksum()` を追加。immich ns の
  専用 ConfigMap `immich-checksum-report` の report.json を読み、latest.json の
  `checksum` 節へ載せる。**status は産出側が判定済みの ok/fail/unconfigured/error を
  そのまま通す契約** (集約側で再判定しない。判定の正は ops/tools/immich_checksum_check.py)。
  産出側未稼働・記録破損 (JSON でない・dict でない・status 未知値) は例外にせず
  no_data で正直に出す (collect_dashboard_smoke と同じ思想。未知 status は「検出ゼロ」
  と「帳簿の壊れ」を区別できるよう no_data に落とす)
- `apps/ops-health-reporter/rbac.yaml` の configmaps resourceNames に
  `immich-checksum-report` を 1 行追加 (worker #2 の「次への一言」の指針どおり)
- report の `notes` に checksum 節の説明を追記
- `ops/tests/test_report_checksum.py` (11 テスト) を新規作成。dashboard-smoke と同じ
  AST 抽出方式で collect_checksum の契約 (パス・status 素通し・no_data 分岐) を固定
- `python3 -m unittest discover -s ops/tests -t .` → 579 テスト全部 green

**受入検証**: 項目 4 を自分で実測 → green
(`grep -q 'checksum' apps/ops-health-reporter/report.py` → rc=0)。

**次への一言**:
1. **残り verify 1 件**: (3) `docs/immich-checksum.md` を作る。上流ソースから確定した
   API 形・認証・queue 完了検知・summary の読み方は worker #2 の PROGRESS に全部ある。
   **実機での確定 (DoD 1) と時間実測 (DoD 5) はまだ** — このサンドボックスには
   tailscale/kubectl が無い。docs には「上流ソースから確定」の事実と、実機確認が必要な
   箇所 (timeLimit 実値・checkpoint 再開の実機挙動・内蔵 cron の有効/無効) を明記して
   書ける。ただし PROJECT.md は「API 形・挙動は実測でしか書かない (P-0035 の流儀)」と
   言っている — docs の本文は上流ソース根拠と実機待ち項目の区別を明示する形で書くこと
2. **DoD 3 (未着手)**: rules.json に `checksum.mismatch_threshold` を追加し、CronJob の
   `MISMATCH_THRESHOLD` env を設定する。現状は未設定で report の status が unconfigured
   を正直に返す (worker #2 が CronJob を unconfigured 前提で実装済み)
3. **DoD 5 / 実機**: Doppler の `IMMICH_API_KEY` 登録 (人間) → CronJob を走らせ、対象
   アセット数・所要時間・結果を PROGRESS に残す。system config の timeLimit と内蔵 cron
   の有効/無効も確認すること
4. 実機で checkpoint 再開が v3.1.0 で正しく動くか (issue #29487 / PR #29516 の修正) を確認する

### 2026-08-24 (worker #4) — verify 項目 3 を green にした (docs/immich-checksum.md)

**やったこと**:
- `docs/immich-checksum.md` を新規作成。verify 項目 3 (`test -f docs/immich-checksum.md`)
  を green にした (下記「受入検証」)。
- PROJECT.md の「API 形・挙動は実測でしか書かない (P-0035 の流儀)」に従い、本文は
  「**上流ソースで確定**」(worker #1/#2 が実読した immich v3.1.0 相当の enum.ts /
  integrity-admin.controller.ts / config.ts) と「**実機確認待ち**」(timeLimit 実値・
  checkpoint 再開の実機挙動・内蔵 cron の有効/無効・実機レスポンス) を**表と節で
  明示的に区別**して書いた。実機で確かめるまで「配置済みの未検証装置」である旨を明記。
- 内容: API 形一覧表 (トリガー/完了検知/結果/認証/途中停止/再開点)、完了検知の実装
  (checksum_runner.py の active>0 観測 + completed baseline + 一定時間空キュー)、
  summary の意味 (現時点の検出済み腐り・未走査分は含まれない)、checkpoint 再開と
  v3.0.1 バグ (issue #29487 / PR #29516)、スケジュール根拠 (日曜 05:30 JST =
  immich 内蔵 cron / restic から離す)、産出側・集約側の実装、閾値 (DoD 3 未設定 →
  unconfigured)、report.json 契約、実機での確認手順 (kubectl create job から
  ConfigMap 確認まで)。

**分かったこと**:
- 受入検証は 4 項目とも green になった (v1: CronJob + kustomization、v2: selftest 8
  fixture、v3: docs、v4: report.py checksum 集約)。ローカル unittest は 579 件全部 green。
  **verify はこれで全部揃ったので、wrapper の再実測が通ればレビューに進む** —
  ただし DoD 5 (実機 1 回実行と時間実測) と DoD 1 の実機確定はまだで、それらは
  docs の「実機確認待ち」節と下の「次への一言」3 に残っている。verify は DoD の下限
  (PROJECT.md の注記どおり)。
- docs の書き方の参考に `docs/immich-postgres-upgrade.md` (実測 vs 推測の明示) と
  `docs/node01-storage.md` を読んだ。docs は他プロジェクトの記録と混在する
  `docs/` 直下に平置き (専用サブディレクトリを作るパターンは現存しない)。

**受入検証**: 項目 3 を自分で実測 → green
(`test -f docs/immich-checksum.md` → rc=0)。4 項目全体の再実行もすべて green。

**次への一言**:
1. **DoD 3 (未着手)**: rules.json に `checksum.mismatch_threshold` を追加し、CronJob の
   `MISMATCH_THRESHOLD` env を設定する。現状は未設定で report の status が unconfigured
   を正直に返す (worker #2 が CronJob を unconfigured 前提で実装済み。docs にも「閾値
   (DoD 3 — 未設定)」節で明記)。これは verify 項目に無いが DoD 本文 (3) に含まれる
2. **DoD 5 / 実機**: Doppler の `IMMICH_API_KEY` 登録 (人間) → CronJob を走らせ、対象
   アセット数・所要時間 (`job.run_elapsed_s`)・結果を PROGRESS に残す。system config の
   timeLimit と内蔵 cron の有効/無効も確認すること。手順は docs/immich-checksum.md の
   「実機で確認すること」に全部書いた
3. 実機で checkpoint 再開が v3.1.0 で正しく動くか (issue #29487 / PR #29516 の修正) を
   確認する。summary レスポンスが docs の形と食い違ったら
   ops/tools/immich_checksum_check.py の fixture を実測値で更新する

## 発見

- (2026-08-24, worker #3) `test_health_report_path.py` は reader ClusterRole
  (ops-health-reporter-reader) の resourceNames を検証していない — 産出 ConfigMap を
  resourceNames に足しても既存 CI は落ちない (足し忘れも検出されない)。dashboard-smoke
  と同じく、ここは docs と人が守る
- (2026-08-24, worker #2) immich の内蔵 checksum cron は既定 enabled (03:00 UTC, timeLimit 1h)。
  実機の system config で有効性と timeLimit を確認すること。
- (2026-08-24) サンドボックスに pip / ruff が無い。CI 側 (`ops/`) の ruff F821 は
  CI 実行時にしか確認できない。Python の実行テスト (unittest + selftest) はローカルで全通。