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

### セッション 10 の記録 (2026-08-23T17:12Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:12:34Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.5h 残っているため、何も適用せずに終える。
ついでに実測した読み取りのみのデータポイント: `kubectl get po -n immich
-l app=immich-postgres` で同じく `immich-postgres-68d65f4b9d-jtbvz` が
Running / Ready 1/1 / RESTARTS=0 (AGE 32m — 観察起点 16:40:08Z と整合)。
CrashLoop の兆しは無し。やることは「次のセッションへの一言」から変わらない。

### セッション 11 の記録 (2026-08-23T17:15Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:15:21Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.4h 残っているため、何も適用せずに終える。
ついでに実測した読み取りのみのデータポイント: `kubectl get po -n immich
-l app=immich-postgres` で同じく `immich-postgres-68d65f4b9d-jtbvz` が
Running / Ready 1/1 / RESTARTS=0 (AGE 35m — 観察起点 16:40:08Z と整合)。
CrashLoop の兆しは無し。やることは「次のセッションへの一言」から変わらない。

### セッション 12 の記録 (2026-08-23T17:16Z)

**観察継続中。** セッション開始時刻が 2026-08-23T17:16:56Z で、窓の満了
(2026-08-24T16:40:08Z) まで約 23.4h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 37m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 次のセッションへの一言

**やることは 1 つ: 24h 観察の成立を確認してから docs に「本番適用記録」節を書き、commit する。**
適用起点は **Pod 開始 2026-08-23T16:40:08Z** (= 窓の満了は 2026-08-24T16:40:08Z) なので、
それ以前に起きたセッションはまだ観察が足りない — 何も適用せずに終えてよい
(その場合は PROGRESS に「観察継続中」とだけ書く)。

窓満了前のセッションへ 2 つの注意:

- **最小工数で終えること**。wrapper が数分間隔で再起動している (セッション 3〜10 が
  2026-08-23T16:59〜17:12Z の約 13 分に集中)。やることは `date -u` で満了前確認 →
  kubectl 読み取り 1 回 (任意) → PROGRESS 追記 → commit のみ。budget soft cap を無駄に
  消費しないこと
- **`## 本番適用記録` の前倒し執筆は絶対にしない**。この見出しを書いた瞬間 verify 3 の
  grep が green になり、wrapper が観察未成立のままレビューへ進めてしまう。
  必ず下の成立確認手順を全て通してから書く

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
- **wrapper (runner) が観察待ちの worker を数分間隔で連続再起動している**: セッション
  3〜10 が 2026-08-23T16:59〜17:12Z の約 13 分に 8 回起きた。worker 側は「窓満了前は何も
  しない」しか打てないため、満了時刻を知っている runner 側で次回起動を満了後まで遅らせる
  (または起動間隔を空ける) のが器側の改善点。それまでは worker が最小工数で抜けるしかない

## 2026-08-23T17:18Z (セッション 13、開始 2026-08-23T17:18:09Z)

**観察継続中。** `date -u` で 2026-08-23T17:18:09Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.4h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 38m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:19Z (セッション 14、開始 2026-08-23T17:19:21Z)

**観察継続中。** `date -u` で 2026-08-23T17:19:21Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.3h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 39m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:20Z (セッション 15、開始 2026-08-23T17:20:29Z)

**観察継続中。** `date -u` で 2026-08-23T17:20:29Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.3h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 40m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:21Z (セッション 16、開始 2026-08-23T17:21:55Z)

**観察継続中。** `date -u` で 2026-08-23T17:21:55Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.3h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 42m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:23Z (セッション 17、開始 2026-08-23T17:23:12Z)

**観察継続中。** `date -u` で 2026-08-23T17:23:12Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.3h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 43m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:25Z (セッション 18、開始 2026-08-23T17:25:55Z)

**観察継続中。** `date -u` で 2026-08-23T17:25:55Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.2h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 45m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:28Z (セッション 19、開始 2026-08-23T17:28:47Z)

**観察継続中。** `date -u` で 2026-08-23T17:28:47Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.2h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 48m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:30Z (セッション 20、開始 2026-08-23T17:30:11Z)

**観察継続中。** `date -u` で 2026-08-23T17:30:11Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.2h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 50m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 次のセッションへの一言

**やることは 1 つ: 窓の満了 (2026-08-24T16:40:08Z) 後に起きたセッションだけが
下の手順を実行する。満了前なら「観察継続中」と書いて commit して終わり。**
適用起点は **Pod 開始 2026-08-23T16:40:08Z** (= 窓の満了は 2026-08-24T16:40:08Z)。

窓満了後セッションの手順 (全部読み取りのみ):

1. `git fetch origin ops-health-report && git show origin/ops-health-report:ops/health/latest.json`
   で (a) `generated_at` が直近であること、(b) applications の immich が Healthy、
   (c) `pvc_usage` immich エントリの `backup_listing.files` に **`immich-db-backup-20260824T*`**
   があり mtime が 24h 以内、(d) `pod_issues` に immich-postgres の新規異常が無いこと
2. `kubectl get po -n immich -l app=immich-postgres` で restarts=0 のまま (CrashLoop 無し)。
   念のため `kubectl logs deploy/immich-postgres --since=24h | grep -iE 'fatal|error|panic'`
   も見る (FATAL レベルが無いこと。INFO は無視)
3. restic バックアップの合否は **baseline 比較** (「分かったこと」節参照) で行う。
   baseline より悪化していなければ観察は成立とする。内蔵日次ダンプの成功を主、restic を従
4. 成立していたら docs/immich-postgres-upgrade.md の末尾に `## 本番適用記録` を追記する。
   載せるもの: 適用日時と PR #555 (merge commit 2efaadf74)、rollout 実績 (旧 Pod 停止→新 Pod
   16:40:08Z 開始、Ready まで約 44 秒)、DB 4 手順の結果、適用後の状態
   (vchord 1.1.1 / probes=1 / server 16.14 / 検索成功)、24h 観察の結果 (restarts、バックアップ、
   ArgoCD health)。**事実のみを書き、推測には推測と明記する**
5. commit する。これで wrapper の verify 3 が green になり、以後は器の通常経路へ乗る。
   完成宣言はしないこと

注意:

- **最小工数で終えること**。wrapper が数分間隔で再起動している。budget soft cap を
  無駄に消費しないこと
- **`## 本番適用記録` の前倒し執筆は絶対にしない**。この見出しを書いた瞬間 verify 3 の
  grep が green になり、wrapper が観察未成立のままレビューへ進めてしまう

## 2026-08-23T17:31Z (セッション 21、開始 2026-08-23T17:31:36Z)

**観察継続中。** `date -u` で 2026-08-23T17:31:36Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.1h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 51m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:33Z (セッション 22、開始 2026-08-23T17:32:38Z)

**観察継続中。** `date -u` で 2026-08-23T17:32:38Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.1h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 52m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:34Z (セッション 23、開始 2026-08-23T17:34:07Z)

**観察継続中。** `date -u` で 2026-08-23T17:34:07Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.1h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 54m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:35Z (セッション 24、開始 2026-08-23T17:34:48Z)

**観察継続中。** `date -u` で 2026-08-23T17:34:48Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.1h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 55m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:37Z (セッション 25、開始 2026-08-23T17:37:04Z)

**観察継続中。** `date -u` で 2026-08-23T17:37:04Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.0h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 57m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。


## 2026-08-23T17:40Z (セッション 26、開始 2026-08-23T17:40:04Z)

**観察継続中。** `date -u` で 2026-08-23T17:40:04Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.0h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 60m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:42Z (セッション 27、開始 2026-08-23T17:41:04Z)

**観察継続中。** `date -u` で 2026-08-23T17:41:04Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.0h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 61m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:43Z (セッション 28、開始 2026-08-23T17:42:42Z)

**観察継続中。** `date -u` で 2026-08-23T17:42:42Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 23.0h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 62m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:47Z (セッション 29、開始 2026-08-23T17:47:11Z)

**観察継続中。** `date -u` で 2026-08-23T17:47:11Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 22.9h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 67m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:50Z (セッション 30、開始 2026-08-23T17:50:14Z)

**観察継続中。** `date -u` で 2026-08-23T17:50:14Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 22.8h 残っているため、最小工数で終える。
読み取りのみのデータポイント: `kubectl get po -n immich -l app=immich-postgres`
で同じく `immich-postgres-68d65f4b9d-jtbvz` が Running / Ready 1/1 /
RESTARTS=0 (AGE 70m — 観察起点 16:40:08Z と整合)。CrashLoop の兆しは無し。
やることは「次のセッションへの一言」から変わらない。

## 2026-08-23T17:53Z (セッション 31、開始 2026-08-23T17:52:57Z)

**観察継続中。** 窓の満了 (2026-08-24T16:40:08Z) まで約 22.8h。Pod 実測:
`immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 / AGE 72m
(観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。

**バックアップ観測 (新規データポイント)**: ウィンドウ内の restic 実行
`immich-restic-backup-29791785` が 2026-08-23T17:45Z 頃に開始し、本セッション時点で
Running (pod z4dzp、10m 経過)。実績では日次実行は毎日 ≒17:45Z に起きている
(cron 文字列は `45 2 * * *` だが直近 3 日分の job 生成時刻はすべて 17:45Z)。
この場合ウィンドウ内の restic 実行は**この 1 回だけ**かもしれない (次回は窓外)。
cron 文字列どおりなら次回は 2026-08-24T02:45Z にあり得る。完成セッションは
`kubectl get jobs -n immich | grep restic-backup` で **29791785 → Complete** を必ず確認。
DB ダンプ本体は UPLOAD_LOCATION/backups/ (ライブラリ PVC 内) に落ちる設計なので、
restic 1 本の成功が「ライブラリ+ダンプ」の担保になる (restic-backup-cronjob.yaml 冒頭注記)。

**発見 (スコープ外・前日分)**: アップグレード前の 2026-08-22T17:45Z 実行
(`immich-restic-backup-29790345`) は B2 の `b2_download_file_by_name: 403` で Failed。
外部認証系の問題で本件 (DB エンジン更新) とは無関係だが、もし今日のウィンドウ内実行が
同じ 403 で落ちたら DoD の「バックアップ成功」が窓内で満たせない。その場合は
本番適用記録を書かず、この節に追記して人間へエスカレート。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
見るのは (1) postgres Pod の RESTARTS=0 維持、(2) job 29791785 の成否。

## 2026-08-23T18:05Z (セッション 32、開始 2026-08-23T17:56:46Z)

**観察継続 + エスカレーション確定。** `date -u` で 2026-08-23T18:05:10Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 22.6h。Pod 実測: `immich-postgres-68d65f4b9d-jtbvz`
Running / Ready 1/1 / RESTARTS=0 / AGE 82m (観察起点 16:40:08Z と整合)。CrashLoop 無し。
namespace 全体も immich-server / ML / valkey 含め健全。

### 新規データポイント 1 — 窓内の restic 実行は job 29791785 で最後 (セッション 31 の仮説を訂正)

CronJob の schedule 文字列 `45 2 * * *` は **UTC ではなくノードローカル (JST) で解釈されている**。

- 実証: `spec.timeZone` 未設定・manifest は 2026-08-10 (P-0028) 以降無変更なのに、
  直近 4 日以上すべて 17:45Z 発火 (= JST 表記の 02:45)。retention (`45 3 * * 0`) も
  Sat 18:45Z 発火 (23h 前 = 日曜 03:45 JST) で同じ挙動
- 推定メカニズム: k3s 組み込み controller-manager のローカルタイム解釈 (仮説。実証したのは発火時刻のみ)
- 結論: **次回発火は 2026-08-24T17:45Z = 窓満了後**。セッション 31 の「cron 文字列どおりなら
  次回 02:45Z があり得る」は誤り。job 29791785 が窓内唯一の restic 実行だった

### 新規データポイント 2 — job 29791785 は昨日と同一の B2 403 経路に入った

- 17:45Z 開始後 ~14 分間ログゼロ。理由が判明: command 冒頭の
  `restic snapshots >/dev/null 2>&1` が出力を握り潰すため (沈黙自体は異常ではない)
- ~18:00Z 実測: snapshots が諦めて `restic init` にフォールバックし、こちらの
  `Stat(<config/>) b2_download_file_by_name: 403` リトライが見え始めた (指数バックオフ)
- 昨日 (29790345) の実績: attempt1 開始から 28.5 分で Error → backoffLimit:1 で
  attempt2 も 29 分で Error、計 ~58 分で job Failed。今夜も同様なら
  **~18:43Z 頃に Failed 確定**する見込み

### 新規データポイント 3 (スコープ外・重大) — 被害は immich に留まらない

- vaultwarden / coder / syncthing の restic-backup もすべて「47h 前成功 → 23h 前一斉 Failed」。
  4 アプリは同一 bucket + 同一 append-only credential を共用する設計 (docs/backup.md)
- 一方 retention (削除権限つき別鍵 `immich-restic-credentials`) は 23h 前に Complete
- 結論: B2 アカウント/bucket 全体ではなく、**共用 append-only application key
  (`*_APPEND_ONLY`) が 2026-08-21T17:45Z〜08-22T17:45Z の間に壊れた** (期限切れ or
  無効化か。docs/backup.md に duration 設定の記載は無し)。オフサイトバックアップが
  ~24h 前から全滅状態

### 判定とエスカレーション

- セッション 31 の規則どおり **本番適用記録は書かない**: 窓内 restic 成功は通常スケジュールでも
  到達不能になり、走っている 1 本も外部認証エラーで死にゆく最中
- 本体の観察項目 (CrashLoop 無し) は順調に蓄積中。DoD を塞いでいるのは「バックアップ成功」の脚のみで、
  原因は DB エンジン更新と無関係な外部認証系
- 人間への依頼: (1) Backblaze コンソールで append-only 鍵を再発行し Doppler の
  `B2_ACCOUNT_ID_APPEND_ONLY`/`B2_ACCOUNT_KEY_APPEND_ONLY` を更新 (ExternalSecret 再同期)。
  (2) もし窓内完成を目指すなら鍵修正後に手動 restic 実行
  (`kubectl create job --from=cronjob/immich-restic-backup`) で窓内成功を狙えるが、
  これは spec の kubectl-write 名目 (rollout 監視・緊急 scale) 外なので人間の承認が前提

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。見るのは
(1) postgres Pod の RESTARTS=0 維持、(2) job 29791785 の最終状態 (~18:43Z 以降の起動なら
Failed 確定済みのはず、`kubectl get jobs -n immich | grep 29791785`)。
B2 鍵が人間の手で直った兆候 (新規 job の Complete 等) がない限り状況は変わらない。

## 2026-08-23T18:08Z (セッション 33、開始 2026-08-23T18:07:22Z)

**観察継続。** `date -u` で 2026-08-23T18:07:22Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 22.5h。最小工数で終える。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 /
  AGE 87m (観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。
- job 29791785 実測: **まだ Running (22m、pod z4dzp、RESTARTS=0 = attempt1 のまま)**。
  セッション 32 の Failed 見込み (~18:43Z) には未達。ログ実測では
  `restic init` フォールバック後の `Stat(<config/>) b2_download_file_by_name: 403`
  リトライループ中で、昨日と同一経路 — 見込み自体は変わらず、attempt1 単独でも
  昨日実績の 28.5 分を過ぎているので失敗方向で進行中。
- jobs 一覧に新しい手動 job 無し → **B2 鍵が直った兆候は無し**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
~18:45Z 以降の起動なら job 29791785 は Failed 確定済みのはず (確認だけする)。
状況が動くのは「人間が B2 鍵を直し、かつ 2026-08-24T16:40:08Z 満了前に窓内での
バックアップ成功 (手動実行含む) が観測された場合」だけ。それまでは Pod の
RESTARTS=0 維持を読み取って終える。

## 2026-08-23T18:46Z (セッション 36、開始 2026-08-23T18:26:02Z)

**観察継続 + job Failed の境界を生で確定。** `date -u` で 2026-08-23T18:26:02Z を確認、
窓の満了 (2026-08-24T16:40:08Z) まで約 22.2h。verify 1・2 は自測で green 再確認
(verify 3 は前回規則どおり意図的に未記述)。セッション 35 と同様に in-session 待機で
attempt2 の終端を捉えた。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` 開始時 Running / Ready 1/1 / RESTARTS=0 /
  AGE 106m、終了時 (18:45:55Z) も Running / restarts=0 / ready=true。CrashLoop の兆し無し。
- **新規データポイント — job 29791785 の Failed を生で実測**: 18:26Z 時点ではまだ Running
  (41m、attempt2 `5phv8` が ~18:14:37Z 開始のまま) だったため ~19 分待機し、18:45:49Z 再読で
  **job = Failed (0/1, duration 60m)** を確認。attempt1 `z4dzp` / attempt2 `5phv8` とも Error
  (31m ≈ 昨日実績 28.5 分どおり)、セッション 32 以降の見込み (~18:43〜18:45Z) は的中。
- **意味合い**: これで窓内 restic の最後の scheduled 発火が確定失敗。セッション 32 実証の
  とおり次回発火は窓外なので、**窓内に「バックアップ成功」が観測される可能性は「人間が B2 鍵を
  直して手動 job を回す」場合を除き消えた**。DoD のバックアップ脚は外部要因のまま未達。
- jobs 一覧に新しい手動 job 無し → **B2 鍵が直った兆候は無い**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
job 29791785 の Failed は本セッションで生実測済みなので再確認不要。見るのは
(1) postgres Pod の RESTARTS=0 維持、(2) 新規手動 job の有無 (`kubectl get jobs -n immich`)。
B2 鍵が人間の手で直った兆候 (新規 job の Complete 等) がない限り状況は変わらない。

## 2026-08-23T18:10Z (セッション 34、開始 2026-08-23T18:10:07Z)

**観察継続。** `date -u` で 2026-08-23T18:10:07Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 22.5h。最小工数で終える。verify 1・2 は自測で
green 再確認 (verify 3 は前回規則どおり意図的に未記述)。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 /
  AGE 90m (観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。
  namespace 全体 (server / ML / valkey) も健全。
- job 29791785 実測: **まだ Running (25m、pod z4dzp、RESTARTS=0 = attempt1 のまま)**。
  セッション 33 の「Failed 確定済みのはず」は早すぎた訂正: 昨日実績の attempt1 28.5 分は
  開始 ~17:45Z から **~18:14Z** 相当で、まだ到達していない。失敗方向の見込み自体は不変
  (B2 403 リトライループ中と前回実測済み、~18:45Z 頃に job Failed 確定か)。
- jobs 一覧に新しい手動 job 無し → **B2 鍵が直った兆候は無し**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
~18:45Z 以降の起動なら job 29791785 は Failed 確定済みのはず (確認だけする)。
状況が動くのは「人間が B2 鍵を直し、かつ 2026-08-24T16:40:08Z 満了前に窓内での
バックアップ成功 (手動実行含む) が観測された場合」だけ。それまでは Pod の
RESTARTS=0 維持を読み取って終える。

## 2026-08-23T18:13Z (セッション 35、開始 2026-08-23T18:13:02Z)

**観察継続。** `date -u` で 2026-08-23T18:13:02Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 22.4h。最小工数で終える。verify 1・2 は自測で
green 再確認 (verify 3 は前回規則どおり意図的に未記述)。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 /
  AGE 93m (観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。
  namespace 全体 (server / ML / valkey) も健全。
- **新規データポイント — attempt1→attempt2 の境界を生で実測**: 開始から 180s 待って
  18:17:23Z に再読みしたところ、pod z4dzp (attempt1) が **Error へ遷移** (age 32m、
  エラー時刻はセッション 34 実測の 25m と今回の間 ≈ 見込みどおり ~18:14Z / 28.5 分)、
  新 pod `5phv8` (attempt2) が ~18:14:37Z 開始で Running。job 自体はまだ Running
  (backoffLimit:1)。昨日実績どおりなら attempt2 も同じ B2 403 経路を ~29 分かけて
  通り、**~18:43〜18:45Z 頃に job Failed 確定**する見込みは不変。
- jobs 一覧に新しい手動 job 無し → **B2 鍵が直った兆候は無し**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
~18:45Z 以降の起動なら job 29791785 は Failed 確定済みのはず (確認だけする)。
状況が動くのは「人間が B2 鍵を直し、かつ 2026-08-24T16:40:08Z 満了前に窓内での
バックアップ成功 (手動実行含む) が観測された場合」だけ。それまでは Pod の
RESTARTS=0 維持を読み取って終える。

## 2026-08-23T18:50Z (セッション 37、開始 2026-08-23T18:47:50Z)

**観察継続。** `date -u` で 2026-08-23T18:47:50Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 21.9h。最小工数で終える。verify 1・2 は自測で
green 再確認 (27 tests OK、verify 3 は前回規則どおり意図的に未記述)。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 /
  AGE 128m (観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。
  namespace 全体 (server / ML / valkey) も健全。
- job 29791785 実測: jobs 一覧で **Failed (0/1, duration 63m) を再確認**。
  セッション 36 の生実測と整合。追加調査不要。
- jobs 一覧に新しい手動 job 無し → **B2 鍵が直った兆候は無し**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
job 29791785 の Failed はセッション 36・37 で二重実測済みなので以後見る必要なし。
見るのは (1) postgres Pod の RESTARTS=0 維持、(2) 新規手動 job の有無
(`kubectl get jobs -n immich`)。B2 鍵が人間の手で直った兆候 (新規 job の Complete 等)
がない限り状況は変わらない。**窓満了 (2026-08-24T16:40:08Z) 後に起動したセッションへ**:
その時点で「24h 以上 CrashLoop 無し」脚は満たされるが「バックアップ成功」脚は外部要因
(B2 鍵) により未達のまま確定する。事実を PROGRESS に淡々と記すだけで判定役に渡すこと。
本番適用記録は書かない (DoD 未達の節を書くのは完成宣言と同じだから)。

## 2026-08-23T18:55Z (セッション 38、開始 2026-08-23T18:50:46Z)

**観察継続。** `date -u` で 2026-08-23T18:50:46Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 21.8h。最小工数で終える。verify 1・2 は自測で
green 再確認 (27 tests OK、verify 3 は前回規則どおり意図的に未記述)。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 /
  AGE 130m (観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。
  namespace 全体 (server / ML / valkey) も健全。
- job 29791785 の Failed 再確認は引き継ぎ指示どおりスキップ (36・37 で二重実測済み)。
  jobs 一覧で新規手動 job の有無のみ見た → **無し = B2 鍵が直った兆候は無い**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
見るのは (1) postgres Pod の RESTARTS=0 維持、(2) 新規手動 job の有無
(`kubectl get jobs -n immich`)。job 29791785 の状態確認は不要。
B2 鍵が人間の手で直った兆候 (新規 job の Complete 等) がない限り状況は変わらない。
**窓満了 (2026-08-24T16:40:08Z) 後の起動なら** セッション 37 引き継ぎどおり:
「24h 以上 CrashLoop 無し」脚は満たされる、「バックアップ成功」脚は外部要因により
未達確定。事実を淡々と PROGRESS に記して判定役へ渡すこと。本番適用記録は書かない。

## 2026-08-23T18:52Z (セッション 39、開始 2026-08-23T18:52:17Z)

**観察継続。** `date -u` で 2026-08-23T18:52:17Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 21.8h。最小工数で終える。verify 1・2 は自測で
green 再確認 (27 tests OK、verify 3 は前回規則どおり意図的に未記述)。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 /
  AGE 132m (観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。
  namespace 全体 (server / ML / valkey) も健全。
- job 29791785 の状態確認は引き継ぎ指示どおりスキップ (36・37 で二重実測済み)。
  jobs 一覧で新規手動 job の有無のみ見た → **無し = B2 鍵が直った兆候は無い**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
見るのは (1) postgres Pod の RESTARTS=0 維持、(2) 新規手動 job の有無
(`kubectl get jobs -n immich`)。job 29791785 の状態確認は不要。
B2 鍵が人間の手で直った兆候 (新規 job の Complete 等) がない限り状況は変わらない。
**窓満了 (2026-08-24T16:40:08Z) 後の起動なら** セッション 37 引き継ぎどおり:
「24h 以上 CrashLoop 無し」脚は満たされる、「バックアップ成功」脚は外部要因により
未達確定。事実を淡々と PROGRESS に記して判定役へ渡すこと。本番適用記録は書かない。

## 2026-08-23T18:53Z (セッション 40、開始 2026-08-23T18:53:47Z)

**観察継続。** `date -u` で 2026-08-23T18:53:47Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 21.8h。最小工数で終える。verify 1・2 は自測で
green 再確認 (27 tests OK、verify 3 は前回規則どおり意図的に未記述)。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 /
  AGE 133m (観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。
  namespace 全体 (server / ML / valkey) も健全。
- job 29791785 の状態確認は引き継ぎ指示どおりスキップ (36・37 で二重実測済み)。
  jobs 一覧で新規手動 job の有無のみ見た → **無し = B2 鍵が直った兆候は無い**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
見るのは (1) postgres Pod の RESTARTS=0 維持、(2) 新規手動 job の有無
(`kubectl get jobs -n immich`)。job 29791785 の状態確認は不要。
B2 鍵が人間の手で直った兆候 (新規 job の Complete 等) がない限り状況は変わらない。
**窓満了 (2026-08-24T16:40:08Z) 後の起動なら** セッション 37 引き継ぎどおり:
「24h 以上 CrashLoop 無し」脚は満たされる、「バックアップ成功」脚は外部要因により
未達確定。事実を淡々と PROGRESS に記して判定役へ渡すこと。本番適用記録は書かない。

## 2026-08-23T18:56Z (セッション 41、開始 2026-08-23T18:55:32Z)

**観察継続。** `date -u` で 2026-08-23T18:55:32Z を確認、窓の満了
(2026-08-24T16:40:08Z) まで約 21.7h。最小工数で終える。verify 1・2 は自測で
green 再確認 (27 tests OK、verify 3 は前回規則どおり意図的に未記述)。

- Pod 実測: `immich-postgres-68d65f4b9d-jtbvz` Running / Ready 1/1 / RESTARTS=0 /
  AGE 135m (観察起点 16:40:08Z と整合)。CrashLoop の兆し無し。
  namespace 全体 (server / ML / valkey) も健全。
- job 29791785 の状態確認は引き継ぎ指示どおりスキップ (36・37 で二重実測済み)。
  jobs 一覧で新規手動 job の有無のみ見た → **無し = B2 鍵が直った兆候は無い**。
  エスカレーション内容 (セッション 32 記載) に追加事項なし。

次のセッションへ: 観察継続のみ。`## 本番適用記録` は絶対に書かない。
見るのは (1) postgres Pod の RESTARTS=0 維持、(2) 新規手動 job の有無
(`kubectl get jobs -n immich`)。job 29791785 の状態確認は不要。
B2 鍵が人間の手で直った兆候 (新規 job の Complete 等) がない限り状況は変わらない。
**窓満了 (2026-08-24T16:40:08Z) 後の起動なら** セッション 37 引き継ぎどおり:
「24h 以上 CrashLoop 無し」脚は満たされる、「バックアップ成功」脚は外部要因により
未達確定。事実を淡々と PROGRESS に記して判定役へ渡すこと。本番適用記録は書かない。
