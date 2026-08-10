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

### セッション 2 (2026-08-10) — レビュー指摘 2 件の解消

前回のレビューで差し戻された 2 件を潰した。**どちらも「マニフェストの記述が実測を先回りしていた」
種類の指摘**で、直したうえで**全 Job を通しで再実行して実測を取り直した**。

#### 指摘 1: 災害復旧経路を殺す init-permissions

docs の「本番に適用するときの手順」item 3 が `chown -R … && chmod 0700 $PGDATA` を指示しつつ、
item 5 で init-bootstrap（PGDATA が空のときだけ initdb する経路）を残していた。PVC 新規作成直後は
PGDATA が無いので chmod が exit 1 → `&&` 連結の init-permissions ごと失敗 → 後続の init-bootstrap が
一度も走らない → PGDATA を作る主体がいないまま `Init:CrashLoopBackOff` から自然回復しない。
**予行演習では一度も踏まれていない経路だった**（Job 3 は常に build が作った既存 pgdata に対してしか
走っていない）のに、docs は「複製上で検証済み」と書いていた。

直した内容:

- docs item 3 を `[ -d … ]` で守る形に書き換え、新節「災害復旧経路を殺さないこと」を追加。
- Job 3 の `init-permissions` を同じ形に修正。
- **Job 4 `p-0035-rehearsal-bootstrap` を新設**（別の空 PVC `p-0035-rehearsal-fresh` を使うので
  他の 3 本と順序関係を持たず並行に走る）。中で対照実験 → 修正形 → #257 の init-bootstrap →
  本番と同じ argv の main container、を一続きで通す。

実測（pod `p-0035-rehearsal-bootstrap-lntm6`、2026-08-10T05:57:57Z〜、Job 21s）:

```
=== control: the OLD init-permissions form against an empty PVC ===
old_form_rc=1
chmod: /var/lib/postgresql/data/pgdata: No such file or directory
CONFIRMED: the old form exits 1, so init-bootstrap would never run
--- 修正形 ---
PGDATA does not exist yet; leaving chmod to the bootstrap path
PGDATA created by initdb: mode=2700 owner=26:999
bootstrap succeeded in 4s
--- 本番と同じ argv + 本番と同じ probe ---
/var/run/postgresql:5432 - accepting connections
ready_rc=0
2026-08-10 05:58:03.284 UTC [9] LOG:  database system is ready to accept connections
```

**副産物 2 つ**（docs に書いた）:

- initdb が作る PGDATA は `0700` ではなく **`2700`**。親に fsGroup が付けた setgid を継承するため。
  postgres のパーミッション検査は group/other のビットしか見ないので **2700 でも通る**（実際に起動した）。
  つまり空 PVC の経路では chmod は要らない。だから「存在するときだけ chmod」で正しい。
- 災害復旧直後の DB はカタログが**最初から vchord 1.1.1 / vector 0.8.3**（更新経路だと vector は
  0.8.0 のまま残るのと対照的）。`vchordrq.probes` は未設定なので、ダンプを流し戻して immich が
  インデックスを作り直したら更新経路と同じく `ALTER DATABASE … SET vchordrq.probes = 1` が要る。

#### 指摘 2: sentinel の順序保証が成立していなかった

ヘッダは「build は毎回 PGDATA と sentinel を消してから始める」と書いていたが、実際の削除は build の
**main container の先頭**で、その手前に initContainer が 2 つ（chown -R と 18MB のダンプ copy）あった。
reproduce / verify の待ち側は Pod 起動直後 t=0 から同じ sentinel を見るので、PVC に前回の残骸がある
状態で apply すると数秒で古い sentinel を拾い、build が `rm -rf` している最中の PGDATA に対して
postgres を起動しうる（単一ノードなので RWO PVC を 3 Pod が同時にマウントできる）。

直した内容 — **2 段構えにした。片方だけでは窓が塞がらない**:

1. build の**最初の** initContainer `reset-state`（root）が、他の何よりも先に sentinel と PGDATA を消す。
   build の main container は「掃除済みであること」を assert するだけに変えた（汚れていたら exit 1）。
2. 待ち側 (`wait-for-build` / `wait-for-upgrade`) を **「sentinel の不在を一度観測してから、
   その後の出現を待つ」** 2 フェーズに変えた。1 だけだと「待ち側が reset-state より先に 1 回目の
   判定をする」窓が残るため。

ヘッダの「再実行時の注意」もこの実装に合わせて書き直した（何を保証していて、なぜ 2 段必要かを明記）。

実測ログ（`p-0035-rehearsal-reproduce-jtlwp`）:

```
phase 1: wait until any stale sentinel is gone
absent at attempt 1
phase 2: wait for this run's build to finish
replica ready at 2026-08-10T05:58:32Z
```

#### 再実行の結果（全 4 Job green）

`kubectl apply -f …` 1 回で 4 本とも Complete。apply から全 Job complete まで **59s**
（build 48s / reproduce 52s / verify 59s / bootstrap 21s）。FATAL 1 / FATAL 2 とも 1 文字違わず再現し、
移行手順も通った。**時間はばらつく**ので docs の表は 1 回目と 2 回目を並記した:

| 段 | 1 回目 | 2 回目 |
|---|---|---|
| `ALTER EXTENSION vchord UPDATE;` | 437.575 ms | 239.738 ms |
| `REINDEX INDEX clip_index;` | 664.895 ms | 496.802 ms |
| `REINDEX INDEX face_index;` | 34.177 ms | 25.569 ms |

docs の「出典」表も、この再実行で取り直したログ（pod 名 4 本）に差し替えた。1 回目のログは
FATAL の初出として別行に残してある。`ops/inventory.json` の note にも指摘 1 の要点
（chmod を PGDATA の存在で守ること）を 1 文追記した。

#### 後始末

`kubectl delete -f …` で PVC 2 本 + Job 4 本を削除。`kubectl get jobs,pods,pvc -n immich | grep p-0035`
は空。本番 `immich-postgres` は Running / RESTARTS 0 / age 3d15h のままで、セッション開始時と同じ
（更新も再起動もしていない）。

#### 次のセッションへの一言

**受入 5 項目を自分で実行して 5/5 green、`python3 ops/validate.py` も 0 error**（warning 2 件は
着手前から存在し P-0035 と無関係）。レビュー指摘 2 件はどちらも実測で裏づけて閉じた。
やり残しは無いのでこのままレビューへ。

もしさらに追加を求められたら、**未検証で残っているのは docs「戻し方」節の 1 点だけ**:
REINDEX 後に image を `16.9-0.4.3` へ戻す経路は**推測のまま**（新形式のインデックスを旧 `.so` が
読めないはずなので image を戻したうえで再 REINDEX が要る、と書いてある）。複製上で潰せる。
その場合は build → reproduce まで走らせてから、旧イメージの Pod を同じ PVC に当てて
検索クエリのエラー文言を取ればよい（`upgrade-rehearsal-job.yaml` の Job 2 が雛形になる）。

## 発見 (スコープ外だが次に渡したいこと)

<!-- 1 行ずつ。ここに書くだけで、このプロジェクトでは手を出さない -->

- `16.14-1.1.1` は pgvector 0.8.3 を同梱するがカタログは 0.8.0 のまま残る。`<=>` は動くので必須ではないが、`ALTER EXTENSION vector UPDATE;` は 12.056 ms で通ることを実測済み（本番更新とは別に判断してよい）。
- GHCR に `17.10-1.1.1` がある（postgres 16→17 のメジャー更新）。`pg_upgrade` が要る別論点。
- 16 系には `16.9-0.5.2` のような「pg 16.9 のまま vchord だけ上げる」中間タグがある。段階的更新の選択肢になりうる。
- 本番 `postgres.yaml` は現状 `docker-entrypoint.sh` の暗黙の `chmod 00700` に依存して動いている。今は無害だが、誰かが `command:` を足した瞬間に壊れる潜在的な脆さ（#257 が実際にこれで落ちた）。16.9 に据え置く場合でも `init-permissions` に `chmod 0700` を足しておく価値がある。
- `immich-library/backups/` が mode 700 / uid 999 のため、将来の復元ツールを uid 999 以外で動かすなら root の staging 段が必ず要る。docs/backup.md の「復元時の注意」はこの点に触れていない。
- `ops/validate.py` の warning 2 件（T-0035 の refs 切れ / todo 0 件）は着手前から存在し、P-0035 とは無関係。
- 「initContainer の command を `&&` で 1 本に繋ぐと、片方が任意の状態で失敗したとき後続の initContainer ごと死ぬ」は immich-postgres 固有の話ではない。`apps/` 配下の他の initContainer にも同じ形が無いか一度 sweep する価値がある（P-0035 では見ていない）。
