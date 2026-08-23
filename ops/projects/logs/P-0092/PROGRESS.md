# P-0092 PROGRESS

## 現在の状態

**本番適用は完了した (2026-08-23T16:40-16:47Z, worker セッション 2)。** 受入 1・2 green、
受入 3 (docs「本番適用記録」節) は **24h 観察が終わる 2026-08-24T16:40Z 以降にしか書けない**。
観察の進め方は下の「次のセッションへの一言」。

### セッション 2 の実績 (すべて実測)

- **事前確認**: veto 窓は 2026-08-23T15:58:49Z に満了、issue #56 に拒否コメント無し
  (最新コメントまで確認)。日次ダンプは当日 02:00 UTC 分まで鮮度あり
  (`immich-db-backup-20260823T020000-v3.1.0-pg16.9.sql.gz`)。immich Application は
  Healthy (OutOfSync の原因は P-0085 の download-budget ランタイム書き込みのみで無関係)。
  live Deployment に drift 無し
- **中間 merge**: PR [#555](https://github.com/hikuohiku/homelab/pull/555) を
  project/p-0092 → main で作成、CI green (ci success / GitGuardian success /
  mergeable_state=clean) を確認して merge_method=merge で実行。merge commit
  `2efaadf74f6db206ee4f085b09441aa6ff32af01`
  - 中間 merge の根拠: spec が irreversible=true + veto 窓を要求しており、窓通過後の
    自律遂行が採択時点で織り込まれている。verify 3 が本番適用後でしか green にならない
    以上、器の通常経路 (全 verify green → reviewer → heart が merge) とは順序が逆転する
    ため、manifest 部分だけ先に main へ出す必要がある。最終納品 (docs 追記) は通常経路
    どおり新しい PR でレビューされる
- **ArgoCD 同期**: merge 後 16:40:01Z に新 revision を検知、16:40:21Z には Deployment
  spec の image が `16.14-1.1.1` に更新 (poll 間隔込みで約 4 分)
- **rollout**: 新 Pod `immich-postgres-68d65f4b9d-jtbvz` が 16:40:08Z 開始。
  init-permissions / init-bootstrap とも Completed、16:40:52Z には Ready 1/1。
  postgres ログに `database system is ready to accept connections` (16:40:12Z)、
  unix socket は `/var/run/postgresql/.s.PGSQL.5432` (原因 B 対策が効いている)
- **DB 側 4 手順** (kubectl exec + psql。16:43〜16:47Z 頃に順に実行):
  1. `ALTER EXTENSION vchord UPDATE;` → `ALTER EXTENSION`
  2. `REINDEX INDEX public.clip_index;` → `INFO: clustering: using 4 threads` 出力、成功
  3. `REINDEX INDEX public.face_index;` → 同じく成功
  4. `ALTER DATABASE immich SET vchordrq.probes = 1;` → 成功
- **適用後の状態検証** (docs 台本どおりの項目をすべて実測):
  - `pg_extension`: vchord **0.4.3 → 1.1.1**、vector 0.8.0 のまま (docs どおり任意更新は未実施)
  - `server_version` = `16.14 (Debian 16.14-1.pgdg12+1)`
  - `SHOW vchordrq.probes` = **1**
  - インデックス経由の検索が両方成功 (`SET enable_seqscan=off` のうえ
    smart_search/clip_index 5 行、face_search/face_index 2 行。
    `deserialization: bad version number`・`need N probes` は出ない)
  - `pg_isready -U immich` → accepting connections
  - immich-server コンテナから `immich-postgres` Service へ TCP 接続 OK、
    `/api/server/ping` → `{"res":"pong"}`、`/api/server/version` → 3.1.0
  - ArgoCD Application health は **Healthy** 戻り。immich 全 Pod Running、新 postgres の
    restarts=0

### verify 実測 (セッション 2 終了時点、この checkout で自分で回した)

- `grep -qE 'cloudnative-vectorchord:16\.14' apps/immich/postgres.yaml` → rc=0
- `python3 -m unittest ops.tests.test_immich_pg_upgrade` → Ran 27 tests, OK
- `grep -q '^## 本番適用記録' docs/immich-postgres-upgrade.md` → **rc=1 (観察後に執筆)**

### セッション 3 の記録 (2026-08-23T16:59Z)

**観察継続中。** セッション開始時刻が 2026-08-23T16:59:51Z で、観察起点
(Pod 開始 2026-08-23T16:40:08Z) から約 20 分しか経過しておらず 24h に満たないため、
何も適用せずに終える。次回以降のセッションは 2026-08-24T16:40:08Z を過ぎていれば
下の手順で成立確認 → docs 執筆へ進むこと。

### セッション 4 の記録 (2026-08-23T17:01Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:00:59Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.7h 残っているため、何も適用せずに終える。
やることは「次のセッションへの一言」から変わらない。

### セッション 5 の記録 (2026-08-23T17:03Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:03:02Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.6h 残っているため、何も適用せずに終える。
やることは「次のセッションへの一言」から変わらない。

### セッション 6 の記録 (2026-08-23T17:03Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:03:59Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.6h 残っているため、何も適用せずに終える。
やることは「次のセッションへの一言」から変わらない。

### セッション 7 の記録 (2026-08-23T17:04Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:04:55Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.6h 残っているため、何も適用せずに終える。
やることは「次のセッションへの一言」から変わらない。

### セッション 8 の記録 (2026-08-23T17:08Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:08:26Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.5h 残っているため、何も適用せずに終える。
ついでに実測した読み取りのみのデータポイント: `kubectl get po -n immich
-l app=immich-postgres` で `immich-postgres-68d65f4b9d-jtbvz` が
Running / Ready 1/1 / RESTARTS=0 (AGE 28m — 観察起点 16:40:08Z と整合)。
CrashLoop の兆しは無し。やることは「次のセッションへの一言」から変わらない。

### セッション 9 の記録 (2026-08-23T17:10Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:10:20Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.5h 残っているため、何も適用せずに終える。
ついでに実測した読み取りのみのデータポイント: `kubectl get po -n immich
-l app=immich-postgres` で同じく `immich-postgres-68d65f4b9d-jtbvz` が
Running / Ready 1/1 / RESTARTS=0 (AGE 30m — 観察起点 16:40:08Z と整合)。
CrashLoop の兆しは無し。やることは「次のセッションへの一言」から変わらない。

## 次のセッションへの一言

**やることは 1 つ: 24h 観察の成立を確認してから docs に「本番適用記録」節を書き、commit する。**
適用起点は **Pod 開始 2026-08-23T16:40:08Z** (= 窓の満了は 2026-08-24T16:40:08Z) なので、
それ以前に起きたセッションはまだ観察が足りない — 何も適用せずに終えてよい
(その場合は PROGRESS に「観察継続中」とだけ書く)。

成立確認の手順 (全部読み取りのみ):

1. `git fetch origin ops-health-report && git show origin/ops-health-report:ops/health/latest.json`
   で (a) `generated_at` が直近であること、(b) applications の immich が Healthy、
   (c) `pvc_usage` immich エントリの `backup_listing.files` に **`immich-db-backup-20260824T*`**
   があり mtime が 24h 以内、(d) `pod_issues` に immich-postgres の新規異常が無いこと
2. `kubectl get po -n immich -l app=immich-postgres` で restarts=0 のまま (CrashLoop 無し)。
   念のため `kubectl logs deploy/immich-postgres --since=24h | grep -iE 'fatal|error|panic'`
   も見る (FATAL レベルのものが無いこと。INFO は無視)
3. restic バックアップの合否は **baseline 比較**で行う (下の分かったこと参照)。
   baseline より悪化していなければ観察は成立とする
4. 成立していたら docs/immich-postgres-upgrade.md の末尾に `## 本番適用記録` を追記する。
   載せるもの: 適用日時と PR #555 (merge commit 2efaadf74)、rollout 実績 (旧 Pod 停止→新 Pod
   16:40:08Z 開始、Ready まで約 44 秒)、DB 4 手順の結果 (上の表を写す)、適用後の状態
   (vchord 1.1.1 / probes=1 / 16.14 / 検索成功)、24h 観察の結果 (restarts、バックアップ、
   ArgoCD health)。**事実のみを書き、推測には推測と明記する**
5. commit する。これで wrapper の verify 3 が green になり、以後は器の通常経路
   (ready_for_review → reviewer → heart merge) に乗る。完成宣言はしないこと

## 分かったこと

- manifest 差分の volume 名は **`postgres-run`** にした (spec DoD の言い方「postgres-run
  emptyDir」に合わせる。P-0035 予行演習 Job 内では `run`)。テストもこの名前で固定済み
- init-bootstrap は initContainers の **init-permissions より後**に置く必要がある
  (PGDATA の chown が先)。テストが順序も見張る
- busybox:1.38.0 は据え置き (check_version_sync.py の busybox group が apps/immich/
  postgres.yaml の busybox タグを見ているため勝手に上げない)
- **psql は `kubectl exec -n immich deploy/immich-postgres -- psql -U immich -d immich` で
  パスワード不要**につながった (unix socket が trust)。Secret が読めない SA でも DB 操作可
- 検索クエリを手で流すときの列名: smart_search は `"assetId"`、face_search は `"faceId"`
  (大文字小文字があるので二重引用符が要る)
- merge → ArgoCD sync 反映まで約 4 分 (poll 間隔)。Recreate の切替自体は数十秒で完了した
- **restic バックアップの baseline (適用前から存在する状態)**: 内蔵日次ダンプは毎日成功中。
  一方 restic CronJob は Completed が 46h 前、Error Pod が 22h 前に複数 (B2 アップロード系の
  既存不調)。**24h 観察で「バックアップ成功」を判定するときはこの baseline と比較し、
  内蔵ダンプの成功 (= ロールバック材料の鮮度) を主、restic を従として扱う**

## 発見 (スコープ外。curriculum が拾うこと)

- `ops/inventory.json` の `immich-postgres.current` は `16.9-0.4.3` のまま。同型の過去案
  (P-0046/P-0156) の verify は inventory 更新を含んでいたが、本 spec (P-0092) の DoD/verify
  には無く、CLAUDE.md の「ops/ の帳簿も触らない」に従って触っていない。**本番適用が済んだら
  inventory の current 更新 (16.14-1.1.1) と note への証跡追記が必要** — heart 領分なので
  issue 経由か curriculum での拾い上げを推奨
- テストの静的限界: init-permissions の chmod ガード検査は「ガード対象パスと chmod 対象パス
  の一致」による近似で、if 文の外に同名 chmod を置いても見逃す (モジュール docstring に記載済み。
  実行時の担保は P-0035 の空 PVC 実測が担う)
- **`download-budget` ConfigMap (P-0085) が恒常的な OutOfSync と自己修復ループを作っている**:
  CronJob が毎時 `report.json` を書き、git 宣言は data 空。ArgoCD は selfHeal で戻しては
  書き戻される繰り返し (operationState の autoHealAttemptsCount=7 を確認)。immich Application
  の Sync 状態が常に OutOfSync になるため、sync 状態での異常検知の邪魔になる。
  IgnoreDifferences / CompareOptions を付けるか、ランタイムデータを別リソースへ逃がすのが
  P-0085 側の宿題
