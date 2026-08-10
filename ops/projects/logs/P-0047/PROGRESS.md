# P-0047 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

**実測の証拠はここに貼る。** verify 6 本のうち backup 成功 (DoD 2) と復元一致 (DoD 3) を
見張っているものは 1 つも無い。コマンドと実出力を貼らなかった分は、存在しなかったことになる
(復元試験の実出力そのものは `restore-drill.md` が本体で、ここには結論と経緯を書く)。

## セッションログ

### s1 (2026-08-10) — DoD 1〜5 を全部通した。verify 6 本 green

**やったこと** (commit 3 本):

1. `c2fce818` DoD 1 — `apps/syncthing/restic-external-secret.yaml` (append-only 鍵 +
   削除鍵の 2 本) と `apps/syncthing/restic-backup-cronjob.yaml` (backup 3:55 JST /
   retention 日曜 4:50 JST) を追加、kustomization に配線、`ops/inventory.json` に
   `syncthing-restic-image`、`check_version_sync.py` の restic GROUP に 5 本目を登録。
   PROJECT.md の設計方針どおりで、変えた点は無い
2. `5d4b85fc` DoD 4 — `ops/tests/test_backup_coverage.py`。CI の既存 discover が拾う
3. (このコミット) DoD 2/3/5 — 実測、`restore-drill.md`、`docs/backup.md`

**実測の結論** (実出力の全文は `restore-drill.md`。ここには結論だけ):

- backup: Job 17 秒 (restic 本体 6 秒)、snapshot `8608514d`、**6 files / 14.577 KiB**、
  残留 lock 無し。**append-only 鍵で `restic init` から通った**
- restore: 使い捨て PVC へ `restic restore latest`、**代表ファイル 6 本すべて原本と sha256 完全一致**。
  所有権 (1000:1000) とパーミッション (config/ は 0700) まで復元された。
  **append-only 鍵だけで復元できた** — 削除鍵は持ち出していない
- ファイル数: `restic ls -l latest` の 6 件 = 復元 6 件。原本 10 件との差 4 件は意図した
  `--exclude`。**今回は稼働中の書き込みによるズレは出ていない**
  (backup 対象 6 ファイルの mtime は 2026-08-06 で止まっており、動いているのは除外した
  index-v2 の 3 ファイルだけ)

**分かったこと / 罠**:

- **復元 Job には `CHOWN` / `FOWNER` / `DAC_OVERRIDE` が要る。**backup 側の
  `DAC_READ_SEARCH` だけを流用して 2 回落ちた。restic は所有権を 1000:1000 に戻すので CHOWN、
  chown した後は root でも所有者ではないので `utimensat` に FOWNER。どちらが欠けても
  **中身は全部書けるのに restic は `Fatal` で終わる**。
  → **この知識は `docs/backup.md` の T-0117 節に既に書いてあった。先に読んでいれば 2 回の
  失敗は要らなかった。次のセッションは手を動かす前に docs/backup.md の該当アプリ節を読むこと**
- **除外パターンは実ファイル名を見てから書いた**のが正解だった。syncthing 2.x の index DB は
  `config/index-v2/{main.db,main.db-shm,main.db-wal,.tmp}` で、`/var/syncthing/index-v2` では
  **ない**。推測で書いていたら黙って何も除外されず、torn な SQLite を毎晩取り続けていた
- **merge 前に実測する方法**: manifest を `kubectl apply` で一時投入した。ArgoCD の tracking
  ラベルが付かないので prune の対象にならず、既存アプリの同期に影響しない。
  `just preview` は root `apps` の auto-sync を止める副作用があるので採らなかった
  (セッションが落ちたら auto-sync が止まったまま残る)
- **`autopilot-writer` は syncthing namespace の secrets に get/list/delete のいずれも
  権限が無い。** そのため ExternalSecret の `deletionPolicy: Retain` で残った Secret 2 本を
  消せなかった。**放置してよいことは実測で確認済み** — ExternalSecret を作り直したら
  残っていた Secret をそのまま引き取って 2 秒で `SecretSynced`/`Ready=True` になった
  (merge 後に起きることの再現)。これ以外の後片付けは全部済んでいる (Job・drill PVC・
  CronJob・ExternalSecret を削除し、namespace を実施前の状態に戻した)
- テストは修正前 (`9e234811`) の worktree に対して回して、
  「apps/syncthing/ は PVC syncthing-data を宣言しているのに restic backup CronJob が無い」を
  **実際に検出することを確認した**。合成入力の純関数テストと合わせて、両方向を固定してある

**次のセッションへの一言**:

- **verify 6 本は自分で回して全部 green を確認した** (wrapper の再実測を待つ)。
  ops/validate.py・check_version_sync.py・check_app_list_sync.py・
  `unittest discover -s ops/tests -t .` (59 tests) も green
- **merge 後に確認すること** (この実測は merge 前の `kubectl apply` なので、ArgoCD 経由で
  同じものが生えることはまだ未確認): `kubectl get cronjob -n syncthing` が 2 本、
  `kubectl get externalsecret -n syncthing` が 2 本とも `SecretSynced`/`Ready=True`、
  翌 3:55 JST 以降の日次 Job が Complete していること。`restore-drill.md` §8 にも書いた
- レビューで差し戻されたら、まず `restore-drill.md` を読むこと。実出力はそこが本体

**発見 (このプロジェクトの仕様外。curriculum へ渡す)**:

- **index DB を除外した状態から復元して syncthing を起動し、再スキャンでインデックスが
  再構築されることは未確認。**T-0140 未着手で同期フォルダが空のため、再スキャンすべき実データが
  無い。実データ移行後に確認すべき (docs/backup.md にも「未確認」と明記した)
- **復元試験の規模が小さい**: 確認できたのは 6 ファイル・14.577 KiB。T-0140 の後、
  実データ規模での再試験が要る
- **syncthing に `pvc-usage-cronjob.yaml` が無い** (immich/vaultwarden/coder にはある)。
  20Gi 宣言の PVC の実使用量が `ops/health/latest.json` に出てこない。今回はスコープ外
- **`test_backup_coverage` の死角**: helm がレンダリングする PVC と Terraform が動的に作る
  `coder-<workspace-id>-home` は静的スキャンに映らない。CI の ops job に kustomize/helm が
  無いため埋められない (docstring と docs/backup.md に明記済み)。CI に kustomize を入れる
  判断は別論点
- **`ops/validate.py` の既存 warning 2 件** (backlog T-0035 の refs 切れ、todo が 0 件) は
  今回とは無関係。heart の領分なので触っていない
