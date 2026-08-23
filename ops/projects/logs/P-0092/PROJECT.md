# P-0092 — 判定役が「次回最優先」と指名した宿題を完済する — immich-postgres を 16.9-0.4.3 から 16.14 系へ本番更新し、写真の実データを載せる土台を現行にする

## 目的

前回判定 (2026-08-22 12:47) が P-0079 を「技術的失敗ではなく外部要因で止まった」「次回最優先候補」と明言して繰り越した宿題の完済。原因確定と本番複製での予行演習は済んでおり (docs/immich-postgres-upgrade.md、P-0035 delivered)、日次 DB ダンプが今も取れ続け、ロールバック材料もある。これは「4 度目の挑戦」ではなく**判定役が名指した残り 1 マス**であり、16.14 へ乗り換えれば同型案は今後生まれない。immich が写真の実データを載せられる土台 (DB エンジン) を現行にすることが本体。irreversible=true なので veto 窓 (rules.json `veto.window_hours`=24h) を必ず待つ。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0092` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `grep -qE 'cloudnative-vectorchord:16\.14' apps/immich/postgres.yaml`
  — マニフェストのイメージが 16.14 系に上がっていること。
  実測 rc=1 (現行は `16.9-0.4.3`)。
- [ ] `python3 -m unittest ops.tests.test_immich_pg_upgrade`
  — 更新対策 (command 明示・chmod ガード・run emptyDir 等) の形がテストで固定されていること。
  実測 rc=1 (ModuleNotFoundError — テストモジュール未存在、FAILED errors=1)。
- [ ] `grep -q '^## 本番適用記録' docs/immich-postgres-upgrade.md`
  — 本番適用の実録 (適用後 immich Healthy・24h 以上 CrashLoop 無し・バックアップ成功) が
  docs に書かれていること。
  実測 rc=1 (節未存在)。

**verify は DoD の下限であって DoD そのものではない。** DoD 本体は「本番適用後、
immich が Healthy に戻り 24 時間以上 CrashLoop 無し・バックアップ成功が続く」ことで、
verify だけ満たして適用していない状態は完了ではない。証跡は PROGRESS.md → 最終的に
docs の「本番適用記録」節へ。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- 台本は全部ある: docs/immich-postgres-upgrade.md (P-0035 が**本番の日次ダンプから作った複製**上で通し成功済み)。「本番に適用するときの手順」節が入力。対象タグは `ghcr.io/tensorchord/cloudnative-vectorchord:16.14-1.1.1` (2026-08-10 GHCR 実測で 16 系最新)
- 対象ファイルは `apps/immich/postgres.yaml` (PVC + Deployment Recreate + Service、kustomization 済み配線)。現行イメージには ENTRYPOINT (`docker-entrypoint.sh`) があり chmod と空 PVC 時の initdb を暗黙に担うが、16.14 系は ENTRYPOINT 無し (`CMD ["bash"]`) — 過去 2 回 (#244/#257) の失敗と P-0035 の FATAL 実測はこの差に起因し、原因は確定済み
- テストモジュール `ops/tests/test_immich_pg_upgrade.py` は未存在 (worker が新設)。py3-yaml で postgres.yaml を読み、対策の形を固定するのが ops/tests の定石

### 作り方 (docs 差分 5 点 + DB 側 4 手順 + 観察)

1. **manifest 差分 (docs「apps/immich/postgres.yaml に必要な差分」5 点)**:
   image を 16.14-1.1.1 へ / `command: ["postgres"]` 明示 (#244 再発防止) /
   init-permissions に `[ -d …pgdata ]` ガード付き `chmod 0700` (**`&&` 連結禁止** — 空 PVC で init-permissions が exit 1 し Init:CrashLoopBackOff から自然回復しない実測) /
   `/var/run/postgresql` へ emptyDir マウント (unix_socket_directories=/tmp は pg_isready probe が既定パスを見て落ちるため採らない) /
   init-bootstrap (空 PVC からの initdb) を載せる — 現行が entrypoint に暗黙担わせている災害復旧経路が 16.14 化で静かに消えるので、複製上で実測済みのこの経路を引き継ぐ
2. `ops/tests/test_immich_pg_upgrade.py` 新設 — 上記対策の形を両方向固定 (verify 2 の中身)
3. veto 窓 24h 経過を確認してから merge → ArgoCD 同期 (Deployment strategy Recreate で旧 Pod 停止→新起動)。恒久 revert は git revert (Git → ArgoCD 経路を守る)、kubectl-write は spec の名目どおり rollout 監視と緊急 scale の手動制御に使う
4. イメージ差し替え直後に DB 側 4 手順を順に流す: `ALTER EXTENSION vchord UPDATE;` → `REINDEX INDEX clip_index;` → `REINDEX INDEX face_index;` → `ALTER DATABASE immich SET vchordrq.probes = 1;`。**省くとサーバは上がるのに immich の検索が `deserialization: bad version number` で壊れ、起動確認では見つからない** (docs 実測)
5. 適用後、immich Healthy 戻りを確認し 24h 以上 CrashLoop 無し・バックアップ成功 (restic CronJob + immich 内蔵日次ダンプ) を観察 — セッションを跨ぐので PROGRESS.md に証跡を残す
6. 全部揃ったら docs/immich-postgres-upgrade.md に「本番適用記録」節を追記して commit

### ロールバック

`ALTER EXTENSION vchord UPDATE` は失敗時カタログ 0.4.3 のまま・REINDEX は再実行可 (docs「戻し方」)。REINDEX 後に 16.9 へ戻すなら再度 REINDEX が要る (未検証・推測)。最後の砦は T-0071 で復元確認済みの日次ダンプ。

## やらないこと

- **immich アプリ本体 (server / machine-learning / valkey 等) の更新** — 本件は DB エンジン層のみ (1 PR 1 論点)
- **`ALTER EXTENSION vector UPDATE` (pgvector 0.8.0 → 0.8.3)** — docs が「必須ではない、本番更新とは分けて判断してよい」と明記
- **storage 側への着手** (稼働中の P-0085 は受け入れ側 intake) — 触るファイルもリスクも別
- **既存 PVC の作り直し・ダンプ流し直し** — 通常経路は既存 PGDATA のままの更新。空 PVC 経路は災害復旧時の保険としてマニフェストに載せるだけで実施しない
- **docs 既存節の書き換え・予行演習マニフェストの変更** — 「本番適用記録」節の追記のみ
- **人間レビュー必須パス (.github/ 等) への変更**
