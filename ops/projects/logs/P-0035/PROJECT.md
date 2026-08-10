# P-0035 — immich-postgres の vchord 更新を、本番データの複製で最後まで予行演習して決着させる

## 目的

`immich-postgres` (cloudnative-vectorchord) は 16.9-0.4.3 に据え置かれたまま、2 回上げて 2 回 revert している
(#244→#247、#257→run #77)。最後の失敗は **「pods/log 権限が無く FATAL の実文言を一度も見ていない」** ところで
止まっており、inventory の note には推測しか残っていない。今は `kubectl-write` capability で全 namespace の
pods/log と使い捨て PVC/Job が使えるので「見えないから触れない」理由は消えた。
**本番を一切触らず、本番の最新ダンプから作った複製に対して更新手順を通しで実行し、次のプロジェクトが
再生するだけで済む台本にする**のがこの案。3 回目を「また試す」にしない。

## 受入チェックリスト

initializer が実測した結果、**5 項目とも現時点で failing**
(2026-08-10、`project/p-0035` の checkout で、リポジトリルートから実行)。

- [ ] `test -f docs/immich-postgres-upgrade.md`
  — 台本と実測をまとめた文書が存在すること。実測 rc=1 (`docs/` には `backup.md` /
    `node01-storage.md` / `terraform-plan-in-ci.md` の 3 本しか無い)。
- [ ] `grep -q 'FATAL' docs/immich-postgres-upgrade.md`
  — **実ログの引用があること**。DoD (2) の「推測でなく実ログ」を機械的に見張る番人。実測 rc=2 (ファイル無し)。
- [ ] `grep -q '16.14-1.1.1' docs/immich-postgres-upgrade.md`
  — 対象タグを名指ししていること。実測 rc=2 (ファイル無し)。
- [ ] `test -f ops/projects/logs/P-0035/upgrade-rehearsal-job.yaml`
  — 再生可能な Job マニフェストが `ops/` 側に残ること (`apps/` に置かない = ArgoCD が勝手に走らせない)。
    実測 rc=1 (`ops/projects/logs/P-0035/` ディレクトリごと無い。この commit で PROJECT.md / PROGRESS.md は
    作るが `upgrade-rehearsal-job.yaml` は作らない)。
- [ ] `python3 -c "import json,sys; t=[x for x in json.load(open('ops/inventory.json'))['targets'] if x['id']=='immich-postgres'][0]; sys.exit(0 if 'docs/immich-postgres-upgrade.md' in t.get('note','') else 1)"`
  — inventory の note から docs へポインタが張られていること。実測 rc=1 (現 note は 2026-08-06 の推測で終わっている)。

**この 5 項目は DoD の下限であって、DoD そのものではない。** とくに 2 項目目の `grep FATAL` は
文字列があれば通ってしまう。**もし複製上で FATAL が一度も再現しなかったら、それは「文字列を捏造してよい」
ではなく「本番の失敗原因は複製で再現しない別のもの (PVC の所有権・fsGroup・ノード固有の状態など) だった」
という重大な発見**であり、docs にはその事実と、代わりに観測した実ログ全文をそのまま書く。
verify を通すために起きていないことを書かない。

## 設計方針

### 前提 (initializer が実測・実読して分かったこと。調べ直さなくてよい)

- **現状の本番**: `apps/immich/postgres.yaml` は image `ghcr.io/tensorchord/cloudnative-vectorchord:16.9-0.4.3`、
  `args: [-c, shared_preload_libraries=vchord.so]`、`command` 無し、`securityContext` は
  runAsUser 26 / runAsGroup 999 / fsGroup 999、initContainer `init-permissions` (busybox で chown 26:999)、
  PGDATA は `/var/lib/postgresql/data/pgdata`。#257 の revert 後の姿。
- **ダンプの実在と場所** (2026-08-10 05:00 の `origin/ops-health-report:ops/health/latest.json` で実測):
  `immich-library` PVC の `backups/` 直下に immich 内蔵の日次ダンプがある。最新は
  **`immich-db-backup-20260810T020000-v3.1.0-pg16.9.sql.gz` (18,851,235 B)**。`--clean --if-exists` 付きの
  gzip 圧縮 SQL (docs/backup.md「immich の restic バックアップ (T-0068)」節)。
  PVC 実使用量は `immich-library` 357 MB / `immich-postgres-data` 374 MB。
- **上流の現時点最新は 16.14-1.1.1 そのもの** (2026-08-10、initializer が GHCR tags API で実測。
  16 系は `16.10-1.0.0` / `16.11-1.1.0` / `16.12-1.1.0` / `16.13-1.1.1` / `16.14-1.1.1` の順で、
  16.14-1.1.1 が最後)。DoD (2) の「および上流の現時点最新」は**同一タグに収束する**。
  着手時に一度だけ再確認すればよい:
  `T=$(curl -sf "https://ghcr.io/token?scope=repository:tensorchord/cloudnative-vectorchord:pull&service=ghcr.io" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")` →
  `curl -sf -H "Authorization: Bearer $T" https://ghcr.io/v2/tensorchord/cloudnative-vectorchord/tags/list?n=2000`。
- **再利用できる過去の実装 (書き直さない。`git show` で取り出してコピーする)**:
  - `git show 57dd4673:apps/immich/postgres-bootstrap-verify-job.yaml` — T-0111 で実機検証済みの
    手動ブートストラップ手順一式 (`initdb` → `pg_hba.conf` に `host all all all scram-sha-256` 追記 →
    `unix_socket_directories=/tmp` を明示した一時起動 → `createdb` → `shared_preload_libraries=vchord.so`
    で再起動 → `CREATE EXTENSION`)。使い捨て PVC + Job の型もここにある。
  - `git show 71f410b6:apps/immich/vchord-upgrade-job.yaml` — `ALTER EXTENSION vchord UPDATE;` +
    非 btree/gin/gist… インデックスを列挙して `REINDEX INDEX` する Job。DoD (3) の「更新手順」の骨。
  - `git show 833922fe -- apps/immich/postgres.yaml` — #257 で本番に入り、run #77 で落ちた構成
    (`command: ["postgres"]` + `init-bootstrap` initContainer)。**複製上で再現すべきはこの構成**。
- **run #77 が観測した事実と、残っている推測** (`ops/journal/2026-08.md` の run #77):
  `init-bootstrap` initContainer は「PGDATA already initialized, skip bootstrap」で正常終了しており、
  落ちていたのは **main container の `postgres` プロセスが起動と同一秒に exit 1**。`kubectl logs` は
  Forbidden で読めず、FATAL は未取得。journal に残る推測は「カタログ上の vchord が 0.4.3 のまま
  新しい `.so` をロードして落ちた」。
  **initializer が読んで気づいたもう 1 つの候補 (これも推測。実ログで決着させること)**:
  T-0111 の run #74 で、このイメージは `/var/run/postgresql` が無く既定の unix socket パスに
  書けずに起動失敗することを**実測している** (だから T-0111 の Job は `unix_socket_directories=/tmp` を
  明示した)。ところが #257 の main container は `postgres -c shared_preload_libraries=vchord.so` だけで
  `unix_socket_directories` を指定していない。**2 つの候補は排他ではない**ので、片方を確認したら
  もう片方も潰す (socket を直しても FATAL が続くか、を複製上で切り分ける)。
- **`kubectl-write` で何ができるか** (`apps/autopilot/rbac.yaml` の ClusterRole `autopilot-writer`):
  全 namespace の pods / **pods/log** / pods/exec / configmaps / **persistentvolumeclaims** / services /
  events / namespaces、apps の deployments 等、batch の jobs/cronjobs に対して `*`。
  **secrets と rbac は含まれない** — `immich-postgres-credentials` は読めない。
  この Job は `automountServiceAccountToken` 付きで `autopilot-writer` が注入されるので、
  セッション内から `kubectl` がそのまま通る (イメージに kubectl v1.35 あり、`ops/memory/substrate.md`)。
- **複製に本番のパスワードは要らない。** 使い捨て PGDATA は自分で `initdb --username=immich` して作り、
  パスワードも使い捨て値をハードコードする (T-0111 の Job と同じ流儀)。本番 Secret を参照しないこと自体が
  「本番に触れていない」ことの担保になる。
- **`kubectl` で作ったリソースは ArgoCD の prune 対象にならない** (tracking label が無いため)。ただし
  `apps/immich/` 配下に置いたら即 sync されるので、**マニフェストは `ops/projects/logs/P-0035/` にしか置かない**。

### 進め方

1. **複製を作る (DoD 1)**。使い捨て PVC (`immich-pg-rehearsal-data` 相当、5Gi、local-path) に、
   **旧イメージ `16.9-0.4.3`** で PGDATA を作る (旧イメージは `docker-entrypoint.sh` を持つので
   initdb 経路が生きている。手で initdb しても可)。`shared_preload_libraries=vchord.so` を有効にして起動し、
   `immich-library` を **`readOnly: true` でマウント**した容器から最新 `.sql.gz` を `gunzip -c | psql` で流し込む。
   これで **vchord 0.4.3 カタログ + 本番スキーマ/データ**の複製ができる。所要時間を測って記録する。
2. **壊れ方を実ログで捕まえる (DoD 2)**。1 の PGDATA に対して **`16.14-1.1.1`** を、#257 と同じ形
   (`command: ["postgres"]` + `args: [-c, shared_preload_libraries=vchord.so]`) で起動する。
   成功すると止まらないので `timeout 60 postgres …` のように時限を付け、**stdout/stderr を丸ごと残す**。
   `kubectl logs <pod> [-c <container>] [--previous]` で読む。**FATAL 行は 1 文字も要約せずに docs へ引用する。**
3. **原因を確定し、通しで 1 回成功させる (DoD 3)**。候補を 1 つずつ潰し、
   `command` の明示 / `unix_socket_directories` の要否 / `ALTER EXTENSION vchord UPDATE;` の要否と順序 /
   `REINDEX` の要否と所要時間を**複製上の実出力で**決める。`ALTER EXTENSION` は 0.4.3→…→1.1.1 の連鎖を
   postgres 自身が解決する (71f410b6 のコメント参照)。**各段の実出力と秒数を必ず控える** — これが台本の中身になる。
4. **台本を残す (DoD 4)**。`ops/projects/logs/P-0035/upgrade-rehearsal-job.yaml` に、1〜3 を
   **もう一度そのまま再生できる形**で書く。initContainer の連鎖 (chown → 複製作成 → 失敗再現 → 更新手順) で
   1 マニフェストに収めるのが素直。**PVC が Job をまたいで残るので、スクリプトは毎回 PGDATA を空にしてから
   始める** (T-0111 run #75 が実際に踏んだ罠)。
5. **文書と inventory (DoD 5)**。`docs/immich-postgres-upgrade.md` に FATAL 実文言・原因・確定した手順・
   所要時間・出典 (Job 名 / 実行日時 / どのログ) を書き、**本番更新は別プロジェクトで行うこと (DoD 6) を明記**する。
   `ops/inventory.json` の `immich-postgres` の `note` を「推測」から「実測とその出典」に書き換え、
   docs へのポインタ (`docs/immich-postgres-upgrade.md` の文字列を含めること) を張る。
   編集後に `python3 ops/validate.py` を回す (CI の ops job と同じもの。既存の warning 2 件は P-0035 と無関係)。
6. **後始末**。作った使い捨てリソース (Job / PVC) は**自分が作った `p-0035` 系の名前のものだけ**を削除する。
   何を消したかを PROGRESS に書く。消す前に docs に必要な出力を写し終えていること。

### 実装上の罠 (踏むと 1 セッション無駄になる)

- **`immich-postgres-data` は絶対にマウントしない** (読み取りも含めて禁止、DoD 1)。触ってよい本番 PVC は
  `immich-library` の **readOnly マウントだけ**で、これは spec が明示的に許している経路。
- Job の `.spec.template` は immutable。作り直すときは `kubectl delete job … --ignore-not-found` してから apply
  (または `kubectl replace --force -f`)。同名で apply し直すと `field is immutable` で止まる。
- RWO の `immich-library` を 2 つ目の Pod からマウントできるのは node01 が単一ノードだから。
  スケジュール先が 1 つしか無いことに依存している (ノードが増えたらこの手は使えない)。
- 使い捨て PVC は Job を消しても残る。1 の複製作成が途中で失敗した状態を次の実行が拾うと、
  「本番相当の複製」でない PGDATA に対して結論を出してしまう。**毎回 PGDATA を作り直す。**
- 一時ファイルは `mktemp`。固定パス `/tmp/…` は前セッションの残骸を拾う (実測済み)。
- セッション終了時に HEAD は `project/p-0035` のまま。wrapper が `git push origin HEAD:project/p-0035` を
  無条件に打つ (`ops/runner/runner.py`)。別ブランチに移らない。
- `ops/inventory.json` の編集は worker.md の「ops/ の帳簿を触らない」の**唯一の例外**
  (verify 5 項目目が名指しで要求している)。`backlog.json` / `state.json` / journal は触らない。

## やらないこと

- **本番の更新** (`apps/immich/postgres.yaml` の image タグ変更、`command` 追加、Deployment の差し替え)。
  DoD (6) が明示的に除外している。台本が揃った時点で**別プロジェクト**として立てる。その旨を docs に書くのが
  このプロジェクトの締め。
- **`apps/` 配下へのマニフェスト追加**。ArgoCD が勝手に sync/実行してしまう。置き場は
  `ops/projects/logs/P-0035/` だけ。
- **restic からの復元**。B2/restic の credential はエージェント環境に無い (`ops/memory/substrate.md`)。
  複製の素材は immich 内蔵の日次 `.sql.gz` に限る。
- **本番 Secret の読み取り**。`autopilot-writer` に secrets は無い。複製は使い捨てパスワードで作る。
- **postgres のメジャー更新 (16→17)**。`17.10-1.1.1` は GHCR に存在するが `pg_upgrade` が要る別の論点。
  気づいたことは PROGRESS の「発見」に 1 行書くだけ (16.9 系のまま vchord だけ進む `16.9-0.5.2` のような
  中間タグの存在も同様に、書き残すだけで手を出さない)。
- **immich 本体 / chart / server の更新**、`docs/backup.md` の書き換え、CHARTER・`ops/memory/` の改訂。
  1 PR 1 論点 (CHARTER §4)。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の更新**。heart の領分。
