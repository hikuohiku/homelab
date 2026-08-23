# P-0092 PROGRESS

## 現在の状態

受入 1・2 が green (2026-08-23, worker セッション 1)。**残りは受入 3 (docs「本番適用記録」節)
のみ**で、これは本番適用 + 24h 観察の後にしか書けない。

- セッション 1 (worker): PROJECT.md「作り方」1・2 を完了し commit (919a348cb)。
  - `apps/immich/postgres.yaml` に docs 差分 5 点を適用済み: image `16.14-1.1.1` /
    `command: ["postgres"]` 明示 / init-permissions に PGDATA 存在ガード付き
    `chmod 0700` (**&& 連結なし**) / volume `postgres-run` (emptyDir) を
    `/var/run/postgresql` に被せ / init-bootstrap initContainer (#257/P-0035 Job 4 実績の
    ロジックを引き継ぎ、env だけ本番 Secret 参照に変更)
  - `ops/tests/test_immich_pg_upgrade.py` 新設。27 本、合成入力での両方向固定 +
    実 repo 検査 + 埋め込みスクリプトの sh -n / bash -n。discover 全体も 381 本 OK

### verify 実測 (この checkout で自分で回した)

- `grep -qE 'cloudnative-vectorchord:16\.14' apps/immich/postgres.yaml` → rc=0
- `python3 -m unittest ops.tests.test_immich_pg_upgrade` → Ran 27 tests, OK
- `grep -q '^## 本番適用記録' docs/immich-postgres-upgrade.md` → **rc=1 (未着手)**

## 次のセッションへの一言

PROJECT.md「作り方」3 から。手順: veto 窓 24h 経過を確認 (spec proposed_at
2026-08-22T15:54:12Z だが merge 判定基準は rules.json を読むこと) → merge 後 ArgoCD 同期
(Recreate で旧 Pod 停止→新起動) → **イメージ差し替え直後に docs「DB 側に当てる手順」4 手順を
順に流す** (ALTER EXTENSION vchord UPDATE → REINDEX clip_index → REINDEX face_index →
ALTER DATABASE immich SET vchordrq.probes = 1。省くと起動確認では見つからない検索壊れ) →
24h 以上 CrashLoop 無し・バックアップ成功を観察 → docs に「本番適用記録」節を追記して
commit。kubectl-write は rollout 監視と緊急 scale の手動制御に使う。

## 分かったこと

- manifest 差分の volume 名は **`postgres-run`** にした (spec DoD の言い方「postgres-run
  emptyDir」に合わせる。P-0035 予行演習 Job 内では `run`)。テストもこの名前で固定済み
- init-bootstrap は initContainers の **init-permissions より後**に置く必要がある
  (PGDATA の chown が先)。テストが順序も見張る
- busybox:1.38.0 は据え置き (check_version_sync.py の busybox group が apps/immich/
  postgres.yaml の busybox タグを見ているため勝手に上げない)

## 発見 (スコープ外。curriculum が拾うこと)

- `ops/inventory.json` の `immich-postgres.current` は `16.9-0.4.3` のまま。同型の過去案
  (P-0046/P-0156) の verify は inventory 更新を含んでいたが、本 spec (P-0092) の DoD/verify
  には無く、CLAUDE.md の「ops/ の帳簿も触らない」に従って触っていない。**本番適用が済んだら
  inventory の current 更新 (16.14-1.1.1) と note への証跡追記が必要** — heart 領分なので
  issue 経由か curriculum での拾い上げを推奨
- テストの静的限界: init-permissions の chmod ガード検査は「ガード対象パスと chmod 対象パス
  の一致」による近似で、if 文の外に同名 chmod を置いても見逃す (モジュール docstring に記載済み。
  実行時の担保は P-0035 の空 PVC 実測が担う)
