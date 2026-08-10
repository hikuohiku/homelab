# immich-postgres (cloudnative-vectorchord) 16.9-0.4.3 → 16.14-1.1.1 更新手順

P-0035 の記録。**本番の PVC には一切触れず**、本番の日次ダンプから作った複製に対して
更新手順を通しで 1 回成功させた。ここに書いてあるログはすべて 2026-08-10 の実測で、
推測は「推測」と明記した箇所だけ。

**本番の更新はこのプロジェクトではやっていない。** 不可逆なので、この台本を使って別プロジェクト
として立てる（下の「本番に適用するときの手順」がその入力）。

- 予行演習のマニフェスト: [`ops/projects/logs/P-0035/upgrade-rehearsal-job.yaml`](../ops/projects/logs/P-0035/upgrade-rehearsal-job.yaml)
  （`apps/` に置いていない = ArgoCD は同期しない。手で apply する）
- 対象タグ: `ghcr.io/tensorchord/cloudnative-vectorchord:16.14-1.1.1`
  （2026-08-10 に GHCR の tags API で確認した時点で、これが 16 系の最新でもある。
  16 系の並びは `16.9-0.5.2` → `16.10-1.0.0` → `16.11-1.1.0` → `16.12-1.1.0` →
  `16.13-1.1.1` → `16.14-1.1.1`）

## これまでの経緯

| いつ | 何をした | どうなった |
|---|---|---|
| #244 | `16.14-1.1.1` に上げた（`command` 無し） | `exec: "-c": executable file not found in $PATH` で CrashLoopBackOff → #247 で revert |
| #257 (833922fe) | `command: ["postgres"]` を明示し、initdb ブートストラップ initContainer を追加 | 既存 PGDATA に対して postgres が起動と同一秒に exit 1 → run #77 で revert。**pods/log 権限が無く FATAL は未取得**、note には推測だけが残った |
| P-0035 (このドキュメント) | 本番の複製を作り、FATAL を実ログで取得。原因を確定し、更新手順を複製上で通した | 下記 |

## 予行演習の作り方（DoD 1: 複製）

本番 `immich-postgres-data` には読み取りも含めて触っていない。素材は immich 内蔵の日次ダンプで、
`immich-library` PVC を **readOnly** でマウントして取り出しただけ。

```
dump_name=immich-db-backup-20260810T020000-v3.1.0-pg16.9.sql.gz
dump_bytes=18851235
dump_sha256=cbe98bdbf48b04d6cdf70a8164169611f8893bc8386d60e61f526be716da7c83
```

使い捨て PVC (`p-0035-rehearsal-data`, 5Gi, local-path) に旧イメージ `16.9-0.4.3` で
`initdb` → `createdb` → ダンプ流し込み。結果:

```
initdb_rc=0 elapsed=2s
restore_rc=0 restore_seconds=30
db_size=144 MB
server_version=16.9 (Debian 16.9-1.pgdg120+1)
PGDATA on disk = 279M
```

複製の中身（＝本番の中身）:

```
    extname    | extversion              index       |  amname
---------------+------------      -------------------+----------
 cube          | 1.5               public.clip_index | vchordrq
 earthdistance | 1.2               public.face_index | vchordrq
 pg_trgm       | 1.6
 plpgsql       | 1.0              smart_search rows = 19   clip_index = 128 kB
 unaccent      | 1.1              face_search  rows =  2   face_index =  88 kB
 uuid-ossp     | 1.1
 vchord        | 0.4.3
 vector        | 0.8.0
```

**注意（無害）**: psql 16 はダンプ中の `\restrict` / `\unrestrict`（pg_dump 17.6+/18 が出力する
メタコマンド）を知らないので `invalid command \restrict` を 2 行出す。これはメタコマンドが
無視されるだけで、復元されるデータには影響しない（`restore_rc=0`、上の行数・サイズで確認）。

## FATAL の実文言（DoD 2）

複製に対し、#257 と同じ argv（`command: ["postgres"]` + `args: [-c, shared_preload_libraries=vchord.so]`）で
`16.14-1.1.1` を起動した。**失敗は 2 段重なっていた。**

### FATAL 1 — PGDATA のパーミッション

```
2026-08-10 05:41:14.436 UTC [11] FATAL:  data directory "/var/lib/postgresql/data/pgdata" has invalid permissions
2026-08-10 05:41:14.436 UTC [11] DETAIL:  Permissions should be u=rwx (0700) or u=rwx,g=rx (0750).
```

起動と同一秒に exit 1。run #77 が見ていたのはこれ。

### FATAL 2 — unix socket ディレクトリ

`chmod 0700 $PGDATA` で 1 を潰すと、次がこれ:

```
2026-08-10 05:41:14.504 UTC [19] LOG:  starting PostgreSQL 16.14 (Debian 16.14-1.pgdg12+1) on x86_64-pc-linux-gnu, ...
2026-08-10 05:41:14.505 UTC [19] LOG:  listening on IPv4 address "0.0.0.0", port 5432
2026-08-10 05:41:14.505 UTC [19] LOG:  listening on IPv6 address "::", port 5432
2026-08-10 05:41:14.512 UTC [19] FATAL:  could not create lock file "/var/run/postgresql/.s.PGSQL.5432.lock": Permission denied
2026-08-10 05:41:14.514 UTC [19] LOG:  database system is shut down
```

## 原因の確定（DoD 3）

### 原因 A: `command` を明示したことで `docker-entrypoint.sh` の `chmod` が失われた

`fsGroup: 999` は kubelet が volume マウント時に再帰適用する（`fsGroupChangePolicy` の既定は
`Always`）。その結果、initdb が作った 0700 の PGDATA は **Pod が作り直されるたびに 2770 になる**。
別 Pod から `stat` した実測:

```
/var/lib/postgresql/data/pgdata mode=2770 owner=26:999
```

`16.9-0.4.3` は `ENTRYPOINT ["docker-entrypoint.sh"]` / `CMD ["postgres"]` を持ち、その
entrypoint が postgres を exec する前に `chmod 00700 "$PGDATA"` する。だから本番は 2770 に
されても毎回起動できていた。`16.14-1.1.1` は ENTRYPOINT を持たない（`CMD ["bash"]`）ので、
#257 が `command: ["postgres"]` を明示した時点でこの chmod が経路ごと消えた。

GHCR の image config を直接読んだ実測（2026-08-10）:

| tag | Entrypoint | Cmd | User |
|---|---|---|---|
| `16.9-0.4.3` | `["docker-entrypoint.sh"]` | `["postgres"]` | `postgres` |
| `16.14-1.1.1` | （無し） | `["bash"]` | `postgres` |

**これはバージョンの問題ではなく entrypoint 迂回の問題**であることを対照実験で確定した。
**旧イメージ `16.9-0.4.3`** に `command: ["postgres"]` だけを付けて同じ複製に当てると、
1 文字違わず同じ FATAL が出る:

```
2026-08-10 05:32:59.933 UTC [1] FATAL:  data directory "/var/lib/postgresql/data/pgdata" has invalid permissions
2026-08-10 05:32:59.933 UTC [1] DETAIL:  Permissions should be u=rwx (0700) or u=rwx,g=rx (0750).
```

つまり vchord のバージョンとは無関係。

### 原因 B: `/var/run/postgresql` が uid 26 から書けない

`16.14-1.1.1` のイメージ内の実測（uid 26 / gid 999 で `stat`）:

```
/var/run/postgresql mode=2775 owner=100:102
```

postgres は uid 26 / gid 999 で動くので、既定の unix socket パスに lock file を作れない。
T-0111 の run #74 が `unix_socket_directories=/tmp` を明示した理由と同じもので、
**#257 の main container はこれを指定していなかった**。

### 潰れた推測: 「カタログ 0.4.3 のまま新しい .so をロードして落ちる」

journal の run #77 に残っていたこの推測は **誤り**。パーミッションと socket を直すと、
カタログが 0.4.3 のままでも `16.14-1.1.1` は正常に起動する:

```
2026-08-10 05:41:25.539 UTC [9] LOG:  starting PostgreSQL 16.14 (Debian 16.14-1.pgdg12+1) ...
2026-08-10 05:41:25.555 UTC [9] LOG:  listening on Unix socket "/var/run/postgresql/.s.PGSQL.5432"
2026-08-10 05:41:25.603 UTC [9] LOG:  database system is ready to accept connections
```

### ただし壊れるのは「起動」ではなく「検索クエリ」だった

カタログ 0.4.3 + `.so` 1.1.1 の状態でサーバは上がるが、**vchordrq インデックスを読むクエリが
実行時に落ちる**。起動時ではなく検索時に出るので、起動確認だけでは見つからない:

```
ERROR:  deserialization: bad version number; after upgrading VectorChord, please use REINDEX to rebuild the index.
```

`ALTER EXTENSION vchord UPDATE;` を当てただけでは**まだ直らない**（オンディスク形式が変わっている）。
`REINDEX` が必須。

### さらに: vchord 1.x は `vchordrq.probes` を要求する

REINDEX 後も、`probes` を設定しないとプランニングの時点で落ちる。`enable_seqscan` を触らない
既定のプランでも再現した:

```
ERROR:  need 1 probes, but 0 probes provided
```

immich の `clip_index` / `face_index` は `lists = [1]` で作られており、0.4.3 には無かった要件。
`ALTER DATABASE immich SET vchordrq.probes = 1;` で解消する（設定後は `SHOW vchordrq.probes` = 1、
インデックス経由の検索が成功）。**リポジトリ内に `vchordrq` の設定は一切無い**
（`grep -rn vchordrq apps/` のヒットは `shared_preload_libraries=vchord.so` の 1 行のみ）ので、
本番更新ではこれを明示的に入れる必要がある。

> 実測の限界: 本番の `smart_search` は 19 行しか無く、immich 自身のクエリではプランナが
> seq scan を選ぶ可能性がある。その場合 `probes` 未設定でも表面化しないかもしれない。
> ただしデータが増えれば必ずインデックスが選ばれるので、**設定しない理由にはならない**。

## 確定した更新手順と実測時間（DoD 3）

複製上で通しで 1 回成功させた。各段の実測:

| 段 | コマンド | 実測 |
|---|---|---|
| 1 | `ALTER EXTENSION vchord UPDATE;` | **437.575 ms**（0.4.3 → 1.1.1。連鎖は postgres が解決する） |
| 2 | `REINDEX INDEX clip_index;` | **664.895 ms**（128 kB / 19 行） |
| 3 | `REINDEX INDEX face_index;` | **34.177 ms**（88 kB / 2 行） |
| 4 | `ALTER DATABASE immich SET vchordrq.probes = 1;` | 即時 |

REINDEX は `INFO: clustering: using 4 threads` を出しつつ 1 秒未満で終わる。**現在のデータ量では
ダウンタイムはほぼ無視できる。** 将来データが増えたら再測すること（この数字は 19 行 + 2 行のもの）。

更新後の状態（本番想定の形で起動した Job から）:

```
    extname    | extversion            vchordrq.probes = 1
---------------+------------           server_version = 16.14 (Debian 16.14-1.pgdg12+1)
 vchord        | 1.1.1                 pg_isready -U immich → /var/run/postgresql:5432 - accepting connections
 vector        | 0.8.0                 clip_index 経由の検索 → 5 行
                                       face_index 経由の検索 → 2 行
```

### `vector` 拡張について（任意）

`16.14-1.1.1` は pgvector 0.8.3 を同梱するが、カタログは 0.8.0 のまま残る。この状態でも
`<=>` を使うクエリは動く（上の実測）。上げる場合は `ALTER EXTENSION vector UPDATE;`
（別途 **12.056 ms** で完了することを確認済み。REINDEX は不要だった）。
**必須ではない**ので、本番更新とは分けて判断してよい。

## 本番に適用するときの手順（別プロジェクトの入力。DoD 6）

**このプロジェクトでは実行していない。** 以下は複製上で検証済みの内容をそのまま本番の形に
書き下したもの。

### `apps/immich/postgres.yaml` に必要な差分

1. image を `16.14-1.1.1` に
2. `command: ["postgres"]` を明示（`args` の先頭 `-c` がコマンド名に解釈される #244 の再発防止）
3. **`init-permissions` initContainer に `chmod 0700` を足す**（原因 A）:
   ```sh
   chown -R 26:999 /var/lib/postgresql/data && chmod 0700 /var/lib/postgresql/data/pgdata
   ```
   fsGroup が毎回 2770 に戻すので、Pod が作り直されるたびに必要。**1 回直せば済む話ではない。**
4. **`/var/run/postgresql` に `emptyDir` をマウントする**（原因 B）:
   ```yaml
   volumeMounts:
     - name: run
       mountPath: /var/run/postgresql
   volumes:
     - name: run
       emptyDir: {}
   ```
   `unix_socket_directories=/tmp` でも起動はするが、その場合 `livenessProbe` /
   `readinessProbe` の `pg_isready -U immich` が既定パスを見て失敗するので、
   **emptyDir を被せる方を採る**（この形で `pg_isready` が通ることを確認済み）。
5. #257 の `init-bootstrap` initContainer（PGDATA が空のときだけ initdb する経路）は
   災害復旧用に引き続き要る。P-0035 では触っていない。

### DB 側に当てる手順

image 差し替えの**直後**に、上の表の 1〜4 を順に流す。イメージだけ替えて拡張を放置すると、
サーバは上がるが immich の検索が `deserialization: bad version number` で壊れる
（起動確認では見つからない）。

### 戻し方

`ALTER EXTENSION vchord UPDATE;` は単一ステートメントなので失敗すればカタログは 0.4.3 のまま。
`REINDEX` はテーブル本体に触らないので再実行で復旧できる。**ただし REINDEX を通した後に
image を 16.9-0.4.3 へ戻すと、今度は新形式のインデックスを旧 `.so` が読めなくなる**ので、
戻す場合は image を戻したうえで再度 `REINDEX` が要る（この経路は複製上で未検証。**推測**）。
最後の砦は T-0071 で復元確認済みの日次ダンプ。

## 予行演習の再生方法

```bash
kubectl apply -f ops/projects/logs/P-0035/upgrade-rehearsal-job.yaml
kubectl wait --for=condition=complete --timeout=30m \
  job/p-0035-rehearsal-build job/p-0035-rehearsal-reproduce job/p-0035-rehearsal-verify -n immich
for j in build reproduce verify; do
  echo "===== $j ====="; kubectl logs -n immich -l job-name=p-0035-rehearsal-$j --all-containers --tail=-1
done
kubectl delete -f ops/projects/logs/P-0035/upgrade-rehearsal-job.yaml   # 撤収（使い捨て PVC ごと消える）
```

3 つの Job は PVC 上の sentinel で順序を取るので 1 回の apply で順に走る。実測の所要時間は
build 44s / reproduce 42s / verify 50s。

### 出典

| 何 | どこ |
|---|---|
| 複製の作成・ダンプの sha256 | Job `p-0035-rehearsal-build` (pod `p-0035-rehearsal-build-7jjfl`), 2026-08-10T05:40Z |
| FATAL 1 / FATAL 2・移行手順の実測時間 | Job `p-0035-rehearsal-reproduce` (pod `p-0035-rehearsal-reproduce-wcgn4`), 2026-08-10T05:41:14Z〜 |
| 本番想定の形での起動確認・`pg_isready` | Job `p-0035-rehearsal-verify` (pod `p-0035-rehearsal-verify-6gf98`), 2026-08-10T05:41:25Z〜 |
| 旧イメージでの対照実験（FATAL 1 の再現） | Pod `p-0035-stage-b2`, 2026-08-10T05:32:59Z |
| image config の Entrypoint/Cmd | GHCR registry API (`/v2/tensorchord/cloudnative-vectorchord/blobs/<config digest>`), 2026-08-10 |
| `/var/run/postgresql` の mode/owner | Pod `p-0035-runprobe`（`16.14-1.1.1` を uid 26 で実行）, 2026-08-10 |

これらの Pod は撤収済み。再取得する場合は上の「再生方法」で同じログが出る。
