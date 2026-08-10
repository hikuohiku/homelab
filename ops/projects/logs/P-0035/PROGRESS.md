# P-0035 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## セッション記録

<!-- 1 セッション 1 ブロック。何をやったか / 分かったこと (実ログ・実測値は必ず) / 次への一言 を書く -->

### セッション 1 (2026-08-10)

**受入 5 項目すべて green にした。** DoD 1〜6 を 1 セッションで通した。

#### やったこと

1. 使い捨て PVC `p-0035-rehearsal-data` (5Gi) に本番の日次ダンプ
   (`immich-db-backup-20260810T020000-v3.1.0-pg16.9.sql.gz`, 18851235 B,
   sha256 `cbe98bdb…16da7c83`) を復元して複製を作った。**本番 `immich-postgres-data` には
   読み取りも含めて一切触っていない。** `immich-library` は readOnly マウントのみ。
2. #257 (833922fe) と同じ argv で `16.14-1.1.1` を当て、**FATAL 実文言を 2 つ取得**。
3. 原因を確定し、更新手順を複製上で通しで 1 回成功させた。
4. `ops/projects/logs/P-0035/upgrade-rehearsal-job.yaml` (PVC + Job 3 本) に台本を残し、
   **クリーン状態から 1 回の apply で通ることを実測で確認**（build 44s / reproduce 42s / verify 50s）。
5. `docs/immich-postgres-upgrade.md` を書き、`ops/inventory.json` の note を推測から実測に差し替えた。
6. 作った p-0035 系リソースは全部削除済み（下記「後始末」）。

#### 分かったこと（実ログ）

**失敗は 2 段重なっていた。両方とも vchord のバージョンとは無関係だった。**

- FATAL 1: `data directory "/var/lib/postgresql/data/pgdata" has invalid permissions`
  / `DETAIL: Permissions should be u=rwx (0700) or u=rwx,g=rx (0750).`
  → `fsGroup: 999` が Pod の volume マウントごとに PGDATA を **2770** に再帰変更する
  （別 Pod からの `stat` で `mode=2770 owner=26:999` を実測）。`16.9-0.4.3` は
  `ENTRYPOINT ["docker-entrypoint.sh"]` がこれを `chmod 00700` で直していたが、
  `16.14-1.1.1` は ENTRYPOINT 無し（`CMD ["bash"]`、GHCR の image config で実測）なので、
  #257 が `command: ["postgres"]` を明示した時点でこの chmod が経路ごと消えた。
  **対照実験で確定**: 旧イメージ `16.9-0.4.3` に `command: ["postgres"]` だけ足すと
  1 文字違わず同じ FATAL が出る（pod `p-0035-stage-b2`）。
- FATAL 2: `could not create lock file "/var/run/postgresql/.s.PGSQL.5432.lock": Permission denied`
  → イメージ内の `/var/run/postgresql` が `mode=2775 owner=100:102` で uid 26 から書けない。
  #257 の main container は `unix_socket_directories` を指定していなかった。

**journal run #77 に残っていた推測は誤りだった。** 「カタログ 0.4.3 のまま新しい .so をロードして
落ちる」ではない。上の 2 つを直せば、カタログ 0.4.3 のままでも `16.14-1.1.1` は正常起動する
（`database system is ready to accept connections` を実測）。

**壊れるのは起動ではなく検索クエリだった**（起動確認だけでは絶対に見つからない）:

- カタログ 0.4.3 + `.so` 1.1.1 で vchordrq インデックスを読むと
  `ERROR: deserialization: bad version number; after upgrading VectorChord, please use REINDEX to rebuild the index.`
- `ALTER EXTENSION vchord UPDATE;` だけでは**直らない**。REINDEX が必須。
- REINDEX 後も `ERROR: need 1 probes, but 0 probes provided` が出る。vchord 1.x の新要件で、
  `ALTER DATABASE immich SET vchordrq.probes = 1;` が要る。`enable_seqscan` を触らない
  既定プランでも再現した。**リポジトリに vchordrq の設定は 1 つも無い。**

確定した手順と実測時間（複製上、smart_search 19 行 / face_search 2 行）:

| 段 | 実測 |
|---|---|
| `ALTER EXTENSION vchord UPDATE;` | 437.575 ms |
| `REINDEX INDEX clip_index;` | 664.895 ms (128 kB) |
| `REINDEX INDEX face_index;` | 34.177 ms (88 kB) |
| `ALTER DATABASE immich SET vchordrq.probes = 1;` | 即時 |

複製作成側の実測: initdb 2s / restore 30s / db_size 144 MB / PGDATA 279M。

#### 本番更新（別プロジェクト）に渡すもの

`docs/immich-postgres-upgrade.md` の「本番に適用するときの手順」節。要点は
`postgres.yaml` に **(a) `chmod 0700` を init-permissions に追加**（fsGroup が毎回戻すので
1 回では済まない）と **(b) `/var/run/postgresql` への emptyDir マウント**
（`unix_socket_directories=/tmp` だと `pg_isready -U immich` の probe が既定パスを見て失敗する。
emptyDir 側で probe が通ることを実測済み）。**P-0035 では本番を一切更新していない。**

#### 踏んだ罠（次に同じ形の Job を書く人へ）

- **`pg_ctl start` の出力をパイプに繋ぐと Job がハングする。** `-l` を付けないと常駐した
  postgres がパイプの write 端を握り続け、`| tail` が EOF を待って永久に止まる。1 回これで
  Job を潰した。必ず `pg_ctl -l <file> … > <file> 2>&1` の形で、パイプを使わない。
- **`immich-library/backups/` は `mode 700 / uid 999 gid 991`** で、postgres コンテナの
  uid 26 gid 999 では traverse できない（`ls: cannot open directory: Permission denied`）。
  root の initContainer で emptyDir に橋渡しする必要がある。
- **YAML の anchor/alias は `---` をまたげない。** 1 ファイル内の複数ドキュメントで
  `&anchor` / `*alias` を使うと `unknown anchor` で apply が落ちる。env ブロックは各 Job に直書き。
- Job の `.spec.template` は immutable。作り直すときは `kubectl delete -f` してから apply。

#### 後始末

作成して削除したもの（すべて `p-0035` 系のみ。他は一切触っていない）:
PVC `p-0035-rehearsal-data` / Job `p-0035-rehearsal-{build,reproduce,verify}` /
Job `p-0035-stage-a` / Pod `p-0035-{probe,probe2,stage-b,stage-b2,stage-c,stage-d,runprobe}`。
削除後 `kubectl get jobs,pods,pvc -n immich | grep p-0035` は空。
本番 `immich-postgres` は Running / RESTARTS 0 / age 3d15h のまま（更新も再起動もしていない）。

#### 次のセッションへの一言

**受入 5 項目は自分で実行して 5/5 green を確認済み**（`python3 ops/validate.py` も
0 error、warning 2 件は既存かつ P-0035 と無関係）。やり残しは無いと思っているので、
差し戻しが無ければこのままレビューへ。もし追加を求められたら、docs の
「戻し方」節にある **REINDEX 後に image を 16.9-0.4.3 へ戻す経路だけが未検証（推測のまま）**
なので、そこを複製上で潰すのが一番価値がある。

## 発見 (スコープ外だが次に渡したいこと)

<!-- 1 行ずつ。ここに書くだけで、このプロジェクトでは手を出さない -->

- `16.14-1.1.1` は pgvector 0.8.3 を同梱するがカタログは 0.8.0 のまま残る。`<=>` は動くので必須ではないが、`ALTER EXTENSION vector UPDATE;` は 12.056 ms で通ることを実測済み（本番更新とは別に判断してよい）。
- GHCR に `17.10-1.1.1` がある（postgres 16→17 のメジャー更新）。`pg_upgrade` が要る別論点。
- 16 系には `16.9-0.5.2` のような「pg 16.9 のまま vchord だけ上げる」中間タグがある。段階的更新の選択肢になりうる。
- 本番 `postgres.yaml` は現状 `docker-entrypoint.sh` の暗黙の `chmod 00700` に依存して動いている。今は無害だが、誰かが `command:` を足した瞬間に壊れる潜在的な脆さ（#257 が実際にこれで落ちた）。16.9 に据え置く場合でも `init-permissions` に `chmod 0700` を足しておく価値がある。
- `immich-library/backups/` が mode 700 / uid 999 のため、将来の復元ツールを uid 999 以外で動かすなら root の staging 段が必ず要る。docs/backup.md の「復元時の注意」はこの点に触れていない。
- `ops/validate.py` の warning 2 件（T-0035 の refs 切れ / todo 0 件）は着手前から存在し、P-0035 とは無関係。
