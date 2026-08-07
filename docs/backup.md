# バックアップ体制の棚卸し (2026-08-04, 2026-08-05 追記)

`ops/backlog.json` T-0013 の調査記録。**設定変更は行っていない。** リポジトリ内の記録を
横断的に確認しただけで、実機（Proxmox / PBS）には autopilot のクラウドサンドボックスから
到達できない（`ops/CHARTER.md` §5.2）ため、この調査だけでは分からないことが多く残る。

## 方針転換 (2026-08-05)

issue #56 (2026-08-05 04:40:59) で人間から新方針: **PBS は重すぎる。k8s より上を IaC で完全に
記述し、揮発しても大丈夫な状態にした上で、restic ベースのアプリ単位バックアップへ移行する。**
以下の「わからないこと」節（PBS のジョブ設定確認）は T-0034 として起票していたが、この方針転換に
伴い **dropped** にした（PBS に依存しない復旧経路を作る話に置き換わったため）。以後の一次情報源は
`ops/backlog.json` の T-0065〜T-0073 系列（IaC 完全性の棚卸し・実サイズ測定・restic 保存先用意・
アプリ別 backup CronJob・復元試験・PBS 退役判断）。

## IaC 完全性の棚卸し (T-0065, run #30)

「node01 が揮発しても Git/Doppler に無いものが残っていないか」を repo 横断で確認した。

- **PVC を持つのは immich / vaultwarden / coder-postgres / immich-postgres の 4 アプリのみ**
  （`immich-library`, `immich-postgres-data`, `vaultwarden-data`, `coder-postgres-data`）。
  いずれも実データで再現不可。immich の valkey persistence・machine-learning cache persistence は
  キャッシュ/ジョブキュー用途で再現可能なためバックアップ対象外でよい
- **ArgoCD / Dex / external-secrets / tailscale-operator は PVC/StatefulSet を一切持たない。**
  Pod を殺して ArgoCD が resync すればフルに復元可能なことを確認した（「持っていないことの確認」完了）
- **ArgoCD 自身の設定（RBAC・OIDC 連携・agent アカウント宣言）は `apps/argocd/values.yaml` に
  git 管理されており再現可能。** ただし `admin` ローカルアカウントの有効化制御が repo に無く、
  UI/CLI 経由でパスワードを変更していればその値は git にも Doppler にも無い（実害は薄い。Dex OIDC
  ログインが主経路のため）
- **新規発見（バックアップ対象の抜け）**: `apps/coder/templates/personal/main.tf` が Coder の
  Terraform プロビジョナー経由で workspace 作成のたびに `coder-<workspace-id>-home` という PVC を
  動的に作成している（`/home/coder` にマウント、dotfiles・`ghq` clone・code-server 設定等）。
  これは ArgoCD が同期する静的マニフェスト（`apps/coder/*.yaml`）には現れず、`apps/` を読むだけでは
  発見できない。**実データを持つ 5 つ目のバックアップ対象**として下の一覧に追加した

## バックアップ対象一覧（2026-08-05 時点、T-0065 で確定）

| PVC / データ | 用途 | backup CronJob タスク |
|---|---|---|
| `immich-library` | 写真/動画原本・サムネイル・変換済み動画 | T-0068（実装済み・credential 登録待ち） |
| `immich-postgres-data` | immich アセットメタデータ・CLIP埋め込み | T-0068 の immich 内蔵日次DBダンプ（`immich-library` 内）で代替。生 PVC 自体のバックアップは対象外 |
| `vaultwarden-data` | パスワードマネージャ本体（SQLite） | T-0069 |
| `coder-postgres-data` | Coder 制御プレーン（ユーザー・workspace・監査ログ） | T-0070（実装済み・credential 登録待ち） |
| `coder-<workspace-id>-home`（動的作成、`apps/coder/templates/personal/main.tf`） | workspace ごとの `/home/coder`（dotfiles・ghq clone 等） | T-0078（実装済み・cooldown中、credential 登録不要） |

## 実サイズの実測 (T-0066, run #50)

T-0080（`pvc-usage-reporter`）が稼働したことで、当初 needs-human だった実サイズ測定は human の
手を借りずに完了した。`ops/health/latest.json` の `pvc_usage`（2026-08-05T12:30:04Z 時点）:

| PVC | 実使用量 |
|---|---|
| `immich-library` | 約 332 MiB |
| `immich-postgres-data` | 約 302 MiB |
| `vaultwarden-data` | 約 4.6 MiB |
| `coder-postgres-data` | 小さい（immich と同程度以下） |

**合計 1 GiB 未満。** T-0066 の why で懸念していた「300GB か 1TB か」という規模の想定は的外れで、
node01 の実ディスク容量（約48.9GiB、T-0079）の 2% にも満たない。T-0067（restic 保存先アカウント）の
判断はこれで確定する: **Backblaze B2 の無料枠（10GB）に収まり、月額コストは実質ゼロ。** Hetzner Storage
Box との比較検討は不要。宣言値（PVC の `requested`）と実測値は 2 桁近く乖離しており、判断は実測が
入るまで宣言値に依存すべきではない。

## vaultwarden の restic バックアップ (T-0069, 実装済み・credential 登録待ち)

`apps/vaultwarden/restic-backup-cronjob.yaml` に2つの CronJob を実装した。

- **`vaultwarden-restic-backup`**（毎日 03:40 JST。node01 の `time.timeZone = "Asia/Tokyo"` により
  k3s の CronJob スケジュールはホストのローカル時刻＝JST で評価される。UTC 換算では前日 18:40）:
  `/data` を直接 restic に渡すと
  `db.sqlite3-wal`/`-shm` と本体の不整合を持ち込む（vaultwarden 公式 wiki の注意）ため、
  initContainer で SQLite の Online Backup API（Python 標準ライブラリの
  `sqlite3.Connection.backup()`。CLI の `.backup`/vaultwarden 内蔵の `VACUUM INTO` と同じ
  仕組みで稼働中でも一貫性のあるコピーが取れる）を使い db.sqlite3 だけ一貫コピーしてから、
  本体コンテナが「PVC（db.sqlite3系・icon_cache を除く）+ 一貫コピー済み db.sqlite3」の
  2 パスをまとめて1スナップショットにする。`rsa_key*.pem`（JWT 署名鍵、失うと全セッション
  無効化）・`attachments/`・`config.json` は PVC 側からそのまま含まれる。initContainer は
  `python:3.12-alpine`（このリポジトリで既に監視対象の image。pvc-usage-reporter と共用）を
  使い、apk 等でのパッケージ追加インストールは行わない（構築セッションのレビューを受け、
  当初案の `alpine` + `apk add sqlite` はネットワーク先のパッケージリポジトリへの到達に
  毎回依存してしまうため置き換えた。vaultwarden 内蔵の `/vaultwarden backup` コマンドは
  ソース確認の結果 `db.sqlite3` と同じディレクトリにしか出力できず読み取り専用マウントと
  両立しないため不採用）
- **`vaultwarden-restic-retention`**（毎週日曜 04:00 JST、UTC 換算では前日 19:00）: `restic forget --keep-daily 7
  --keep-weekly 4 --keep-monthly 6 --prune`
- 保存先は Backblaze B2（`b2:<bucket>:vaultwarden`）。immich（T-0068）・coder-postgres
  （T-0070）も同じ bucket・同じ credential でパス末尾だけ変える設計にした
- **復元時の注意**: 復元前に vaultwarden コンテナを止め、既存の `db.sqlite3-wal`/`db.sqlite3-shm`
  を削除してから復元後の `db.sqlite3` を配置すること（stale な WAL が残ると起動時に不整合を起こしうる）

## immich の restic バックアップ (T-0068, 実装済み・credential 登録待ち)

`apps/immich/restic-backup-cronjob.yaml` に2つの CronJob を実装した。

- immich server v2.7.5 には "Database Backup" 機能がデフォルトで有効（`server/src/config.ts`
  の `backup.database`: `enabled: true`、毎日 02:00 **UTC** 実行、`keepLastAmount: 14`）で、
  これは k8s の CronJob ではなく immich-server コンテナ内で動くアプリケーション自身のタイマーの
  ため、node01 の JST 設定の影響を受けない（`backup_listing` のファイル名・`mtime` が実際に
  UTC 02:00 で揃っていることを 2026-08-06 実測で確認済み、T-0125）。この節の他の CronJob（下記）とは
  時刻の基準が異なる点に注意
  pg_dump 相当のフルダンプ（pgvector/embeddings テーブル含め除外なし）を `.tmp` へ書いてから
  rename する形で `UPLOAD_LOCATION/backups/*.sql.gz` に確定させる（サブエージェントが v2.7.5
  タグの実ソース `database-backup.service.ts` を直接確認して裏取り済み）。`UPLOAD_LOCATION`
  は `immich-library` PVC そのもの（`apps/immich/values.yaml` の `immich.persistence.library`）
  のため、**`immich-library` PVC を restic で1本取るだけでライブラリ本体と DB ダンプ
  （immich-postgres-data 相当のデータを含む）が同時に取れる。** coder-postgres（T-0070）の
  ような専用 `pg_dump` initContainer は不要だった
- **`immich-restic-backup`**（毎日 02:45 JST、immich 自身のダンプ完了を待つため45分後に開始。
  UTC 換算では前日 17:45）:
  `immich-library` PVC をそのまま restic backup する
- **`immich-restic-retention`**（毎週日曜 03:45 JST、UTC 換算では前日 18:45）: `restic forget --keep-daily 7
  --keep-weekly 4 --keep-monthly 6 --prune`
- 保存先は vaultwarden/coder-postgres と同じ Backblaze B2 バケット、パス末尾のみ `immich`
  （`b2:<bucket>:immich`）
- **復元時の注意**: `immich-library` PVC をリストア後、`backups/` 配下の最新 `.sql.gz` を
  `gunzip` して `psql`（`--clean --if-exists` 付きダンプなので既存オブジェクトを DROP しつつ
  流し込める想定）で immich-postgres へ復元する。復元時のバージョン整合（ダンプ生成時の
  immich/postgres バージョンとファイル名の `v{serverVersion}-pg{postgresVersion}` を突き合わせる）
  を事前に確認すること
- **内蔵ダンプの継続監視**（issue #56, 2026-08-05 12:35:47 のレビュー対応）: 「内蔵の日次 DB
  ダンプが `UPLOAD_LOCATION/backups/` に落ちている」という T-0068 の前提はソースコードでの
  確認にとどまり、実機では未検証だった。これが崩れていると写真だけあって DB が無いバックアップ
  になり、実際に復元するまで気づけない。`apps/immich/pvc-usage-cronjob.yaml`（T-0080 の
  pvc-usage-reporter、3 namespace 共通スクリプト）に `BACKUP_LISTING_DIR` を設定（immich のみ）
  し、`backups/` 直下のファイル一覧（name/bytes/mtime）を `pvc-usage-report` ConfigMap の
  `backup_listing` キーに書き戻すようにした。vaultwarden/coder はこの env を設定しないため
  スクリプトは共通のまま影響を受けない（`ops/check_pvc_usage_script_sync.py` で3ファイル一致を
  引き続き検証）。ops-health-report の `pvc_usage`（immich エントリ）を読むたびに
  `backup_listing.files` の有無と最新 `mtime` を確認すること（`ops/CHARTER.md` §2 に手順追記）

## coder-postgres の restic バックアップ (T-0070, 実装済み・credential 登録待ち)

`apps/coder/restic-backup-cronjob.yaml` に2つの CronJob を実装した。vaultwarden（T-0069）と
同じ設計思想（稼働中の DB を直接 restic に渡さず、一貫性のあるダンプを先に作る）を
PostgreSQL 向けに適用したもの。

- **`coder-restic-backup`**（毎日 03:10 JST、UTC 換算では前日 18:10）: PostgreSQL は稼働中の PGDATA を restic で
  直接舐めてはいけない（サーバを止めない限り一貫したスナップショットにならないと
  PostgreSQL 公式ドキュメントが明記）ため、initContainer で `pg_dump -Fc`（カスタム形式、
  `pg_restore` で復元）を使いネットワーク経由で論理ダンプを取り、emptyDir 経由で本体の
  restic-backup コンテナが1スナップショットにまとめる。initContainer は
  `apps/coder/postgres.yaml` と同じ `postgres:17.10` イメージを使う（pg_dump はサーバと
  同バージョンのクライアントを使うのが安全なため。新しい image pin を増やさず、
  `ops/inventory.json` の `coder-postgres` エントリの `mirrors` としてバージョン同期を
  `check_version_sync.py` の CI で追う）。PVC には触れず DB へのネットワーク接続のみのため、
  vaultwarden の sqlite-snapshot initContainerと違い `DAC_READ_SEARCH` は不要
- **`coder-restic-retention`**（毎週日曜 04:10 JST、UTC 換算では前日 19:10）: `restic forget --keep-daily 7
  --keep-weekly 4 --keep-monthly 6 --prune`
- 保存先は vaultwarden と同じ Backblaze B2 バケット、パス末尾のみ `coder-postgres`
  （`b2:<bucket>:coder-postgres`）
- **immich-postgres は対象外**（cloudnative-vectorchord の拡張を含めた扱いが別途要るため
  T-0029 側で検討。上表参照）
- **復元時の注意**: `pg_restore` で復元する前に coder-postgres の既存データをクリアするか
  新規 DB に復元してから切り替えること。`coder server` は起動時に DB migration を自動適用する
  ため、リストア後に起動する `coder` のバージョンと migration の整合を確認する

## coder workspace home の restic バックアップ (T-0078, 実装済み・cooldown中)

`apps/coder/workspace-home-backup-cronjob.yaml` に実装した。他3コンポーネント（immich/
vaultwarden/coder-postgres）と異なり、対象 PVC (`coder-<workspace-id>-home`) は workspace の
作成・削除のたびに動的に増減するため、静的なマニフェストには書けない。

- **オーケストレータ方式**: `coder-workspace-home-backup` CronJob（毎日 03:30 JST、UTC 換算では
  前日 18:30）が
  python:3.12-alpine の Pod 1個を起動し、`app.kubernetes.io/name=coder-pvc` ラベル
  （`apps/coder/templates/personal/main.tf` が付与）で対象 PVC を毎回列挙、PVC ごとに
  使い捨ての Job（restic/restic イメージ、対象 PVC のみ readOnly マウント）を Kubernetes API
  経由で生成する。生成した Job は `ttlSecondsAfterFinished: 3600` で自動 GC されるため、
  オーケストレータ自身は Job の削除権限を持たない（`create` のみ）。専用 ServiceAccount
  `workspace-home-backup`（coder namespace 限定の Role: PVC の get/list、Job の create のみ）
- **credential は新規登録不要**: coder-postgres 用（T-0070）の `coder-restic-credentials`
  Secret（同じ Backblaze B2 バケット・同じ暗号化パスワード）をそのまま流用し、リポジトリパス
  末尾のみ `coder-workspace-homes` に変える設計
- **1リポジトリを workspace 間で共有**: 各 Job は `restic backup --host <workspace-id>` で
  ホストタグを付ける。**`coder-workspace-home-backup-retention`**（毎週日曜 04:30 JST、UTC 換算では
  前日 19:30）は
  `restic forget --group-by host --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune`
  でホスト単位に世代管理する。workspace が削除されてもそのホストの世代は自然に切り捨てられる
  （明示的な削除はしない）
- **複数 workspace 同時実行時のロック競合対策**: オーケストレータは PVC ごとに Job を作る際
  20秒間隔を空ける簡易な直列化のみで、完了を待って次へ進む仕組みではない。個人利用規模
  （workspace 数が少ない）を前提にした割り切り
- **cooldown 対象**: 新しい write 権限を持つ ServiceAccount（Job の create）・新しい
  resources/securityContext（実測の裏付けなし、既存の restic backup CronJob 群からの類推値）を
  持ち込むため、CHARTER §4「縛る変更には実測か裏付けが要る」の対象とし、この PR を作成した
  起動では merge していない
- **未実施（フォローアップ）**: T-0071（immich/vaultwarden/coder-postgres で完了済み）と同様の
  復元試験はまだ行っていない。デプロイ後の初回実行確認・復元試験・オーケストレータの実メモリ
  使用量確認は別タスクとして backlog に追跡する

## 復元試験（T-0071）

人間の新方針（issue #56, 2026-08-05 04:40:59「試したことのないバックアップは、バックアップでは
ありません」）どおり、restic バックアップから実際に復元し「データが読めること」を確認する検証。
本番の PVC には一切触れず、使い捨ての別 PVC/Job（`apps/immich/restic-restore-verify-job.yaml`）を
Git → ArgoCD 経由で apply し、`kubectl get pod`（`autopilot-reader` で許可されている read 専用
操作）で結果を確認する形で、autopilot 自身が人間の手を借りずに完結できた。

### immich（完了、2026-08-05）

`restic restore latest` を使い捨て PVC (`immich-restore-verify`, 5Gi) へ実行し、成功を確認した。

| 項目 | 結果 |
|---|---|
| restore 結果 | 成功（`restore_rc=0`） |
| 復元にかかった時間 | **16 秒**（332 MiB、173 files/dirs） |
| 復元されたファイル数 | 82（バックアップ時と同数） |
| DB ダンプの整合性 | `immich-db-backup-20260805T020000-v2.6.3-pg16.9.sql.gz` の gzip マジックバイト確認 OK |

**この短時間（16秒/332MiB）は、PBS の VM 単位バックアップと比べた復元時間の判断材料になる**
（T-0072 の PBS 退役判断で使う）。

試行錯誤の記録（後続の同種検証・他コンポーネントへの流用時の参考）:

1. **初回**: `restore_rc=1` だが `file_count=82`・DB dump 整合性 OK。原因不明のまま診断用に
   `restic restore` の標準出力/標準エラー末尾を termination message に追加（#230）
2. **原因1（EPERM, lchown）**: `securityContext.capabilities.drop: ["ALL"]` により、コンテナが
   root で動いていても `CAP_CHOWN` が無く、restic がバックアップ時点の所有者へ `lchown`
   できなかった。`capabilities.add: ["CHOWN"]` で解消（#231）
3. **原因2（EPERM, utimes）**: 所有者は復元できるようになったが、今度はタイムスタンプ復元
   （`utime(2)`）に `CAP_FOWNER` が必要で失敗。`man 7 capabilities` の `CAP_FOWNER` 説明に
   `utime(2)` が明記されている。`capabilities.add` に `FOWNER` を追加して解消（#232）
4. **原因3（EACCES, lchown）**: capability 不足（EPERM）とは別に、`permission denied`
   （EACCES）が発生。原因は Job 再実行の間 PVC が消えずに残ること — 前回実行で一部
   ディレクトリが実際に backup 側の所有者へ chown され、その通常権限ビットが
   `CAP_DAC_OVERRIDE` を持たない root のアクセスを塞いでいた。`DAC_OVERRIDE` を追加し、
   かつ `restic restore` の前に `rm -rf /mnt/restore/*` で毎回クリーンアップするようにして
   解消（#233）

**教訓**: `securityContext.capabilities.drop: ["ALL"]` の Pod で restic restore（所有者・
パーミッション・タイムスタンプの復元を伴う）を実行するには、最低でも `CHOWN` / `FOWNER` /
`DAC_OVERRIDE` の 3 capability が要る。かつ target が persistent な PVC の場合、再実行時に
前回の残留状態（chown 済みディレクトリ等）が新たな権限エラーの原因になりうるため、
restore 前のクリーンアップを常に入れる。

検証専用の `immich-restore-verify` PVC/Job は確認が取れたため削除した
（このファイルの冒頭コメントに書いた運用どおり）。

### vaultwarden（完了、2026-08-06）

immich で判明した 3 capability（CHOWN/FOWNER/DAC_OVERRIDE）+ クリーンアップステップを
**初回実装から**織り込んだため、試行錯誤なく初回実行で成功した。

| 項目 | 結果 |
|---|---|
| restore 結果 | 成功（`restore_rc=0`） |
| 復元にかかった時間 | **9 秒**（約4.6 MiB、12 files/dirs） |
| 復元されたファイル数 | 4 |
| rsa_key*.pem（JWT 署名鍵） | 1 件、復元確認 |
| db.sqlite3 の整合性 | SQLite フォーマットマジックバイト確認 OK |

**教訓**: immich で判明した権限まわりの教訓（CHOWN/FOWNER/DAC_OVERRIDE + クリーンアップ）は
vaultwarden にもそのまま当てはまった。同じ `local-path` PVC + restic の組み合わせである限り、
コンポーネントが変わっても同じ 3 capability で足りると見てよい。

検証専用の `vaultwarden-restore-verify` PVC/Job は確認が取れたため削除した。

### coder-postgres（完了、2026-08-06）

immich/vaultwarden とは異なり、PVC ではなく単一ファイルの `pg_dump -Fc` ダンプを使い回さない
`emptyDir` へ復元する設計（`apps/coder/restic-restore-verify-job.yaml`、削除済み）。
「データが読めること」の確認は `pg_restore --list`（TOC 読み取り）と
`pg_restore -f /dev/null --no-owner --no-privileges`（全データブロックを実際に展開、ライブ
DB 接続不要）の2段で行った。

| 項目 | 結果 |
|---|---|
| restore 結果 | 成功（`restore_rc=0`） |
| 復元にかかった時間 | **8 秒** |
| pg_restore TOC 読み取り | `pg_restore_list_rc=0`（1075 エントリ） |
| pg_restore 全データ展開 | `pg_restore_full_rc=0` |

**教訓**: 着手時の予想（`pg_restore` はネットワーク経由の論理復元でファイルシステムの
所有者・パーミッション・タイムスタンプ復元を伴わないため CHOWN/FOWNER/DAC_OVERRIDE が
不要かもしれない）を確かめる前に、immich/vaultwarden との一貫性を優先して3 capability を
予防的に足した状態で実行し、初回で成功した。実際に不要かどうかの追試はしていない
（動いているものをわざわざ絞り込む理由が薄いため見送り）。

検証専用の `coder-postgres-restic-restore-verify` Job は確認が取れたため削除した。

### coder workspace home（完了、2026-08-07、T-0117）

T-0071（immich/vaultwarden/coder-postgres）とは別タスク。T-0078 で実装したオーケストレータ方式の
workspace home バックアップに対する初回の復元試験。2 workspace のうちデータ量が小さいと見込まれる
"test"（workspace_id: `7fdb7787-e2b7-4a6d-b54f-1640b5d9b587`）を対象に、CHOWN/FOWNER/DAC_OVERRIDE
+ クリーンアップを初回実装から織り込んだ `apps/coder/workspace-home-restic-restore-verify-job.yaml`
で検証し、試行錯誤なく初回実行で成功した。

| 項目 | 結果 |
|---|---|
| restore 結果 | 成功（`restore_rc=0`） |
| 復元にかかった時間 | 31 秒（約925 MiB） |
| 復元されたファイル数 | 3904 files/dirs（restic のサマリ値。ジョブ内の `find -type f` カウントは 3156） |

検証専用の `coder-workspace-home-restore-verify` PVC/Job は確認が取れたため削除した。

DoD(3)（オーケストレータ Pod の実メモリ/CPU 使用量確認）は、Pod が毎回 30 秒未満で完了・GC
されるため 120 秒間隔のループでは実行中に捕まえられず、DoD の「可能であれば」規定により見送った
（run #205 の journal に記録済み）。

**T-0071 は immich・vaultwarden・coder-postgres の3コンポーネント全て完了。**
これに依存していた T-0023（coder メジャー更新）・T-0027（immich メジャー更新）・T-0029
（immich postgres/vchord メジャー更新）は `blocked_by` が解消したため `todo` に戻した。

### 必要な Doppler 登録（T-0067）

`apps/vaultwarden/restic-external-secret.yaml` / `apps/coder/restic-external-secret.yaml` が
参照するキー。immich 実装時も同じキーを使い回す想定（バケットを分けたい場合は別途相談）。

| Doppler キー | 内容 |
|---|---|
| `RESTIC_PASSWORD` | restic リポジトリの暗号化パスワード（新規に決めて登録） |
| `RESTIC_B2_BUCKET` | Backblaze B2 のバケット名 |
| `B2_ACCOUNT_ID` | B2 application key ID（**実際には削除権限を持つ鍵**。retention CronJob の `forget --prune` と backup CronJob の両方がこのキーを共用しているため。append-only にはなっていない — 下記 T-0106 参照） |
| `B2_ACCOUNT_KEY` | 同上 application key |

登録後、`ops-health-report`（`pod_issues`）で `vaultwarden-restic-backup` CronJob の Job が
Failed になっていないことを確認できれば実際に動作したとみなせる（T-0097）。

## backup 専用 credential への分離 (T-0106, 2026-08-06)

issue #56（2026-08-05 16:02:29）の指摘: 実際に `restic forget --prune` が B2 上のオブジェクトを
削除できることを確認した際、上記 `B2_ACCOUNT_ID`/`B2_ACCOUNT_KEY` が backup と retention の両方の
CronJob に共用されており、削除権限を持つ鍵であることが判明した（「append-only キーを推奨」という
当初の登録依頼どおりには登録されていなかった、または退避目的で意図的に共用された）。node01 が
侵害された場合、この単一の鍵でバックアップそのものを消せてしまう。「バックアップがある」ことと
「バックアップが守られている」ことは別、という指摘どおり。

**対処（manifest 側、この起動で完了）**: `apps/{vaultwarden,immich,coder}/restic-external-secret.yaml`
に backup 専用の ExternalSecret（`<app>-restic-backup-credentials`）を追加した。新しい Doppler キー
`B2_ACCOUNT_ID_APPEND_ONLY`/`B2_ACCOUNT_KEY_APPEND_ONLY` を参照する。retention CronJob
（削除が必須）は引き続き既存の `<app>-restic-credentials` を使う。

**現状は追加のみで、既存の backup CronJob の参照先はまだ切り替えていない。** 新しい Doppler キーが
まだ存在しないため、新しい ExternalSecret は登録が済むまで `SecretSyncedError` のまま Ready に
ならない想定だが、どの CronJob もまだこの新しい Secret を参照していないため、現行の日次バックアップ
には影響しない。

**人間への依頼（T-0106, needs-human）**: Backblaze の管理コンソールで、既存バケット向けの
新しい Application Key を発行する。Capabilities は `listBuckets`/`listFiles`/`readFiles`/
`writeFiles` のみ（`deleteFiles` を含めない = 真の append-only）にする。発行した keyID/
applicationKey を Doppler（`homelab/prd`）に `B2_ACCOUNT_ID_APPEND_ONLY` /
`B2_ACCOUNT_KEY_APPEND_ONLY` として登録する。

**登録後の切り替え（T-0120, blocked）**: `kubectl get externalsecret <app>-restic-backup-credentials
-n <app>` で Ready を確認した上で、4つの backup CronJob（`vaultwarden-restic-backup` /
`immich-restic-backup` / `coder-restic-backup` / `coder-workspace-home-backup` の動的 Job
テンプレート）の `B2_ACCOUNT_ID`/`B2_ACCOUNT_KEY` の `secretKeyRef.name` を新しい
`<app>-restic-backup-credentials` に切り替える。retention CronJob 側は変更しない。

## わかっていること（repo から）

- PBS (Proxmox Backup Server) VM が `qemu/112` として稼働中。手動管理
  （Terraform 管理対象外、`terraform/proxmox/pbs.tf.ignore` に構成を記録）
- pbs VM 自体のスペック: 4 vCPU / 8GB RAM / 64GB ディスク（SeaBIOS）
- `terraform/proxmox` は node01 (`qemu/113`) のみ管理。pbs 自身は Proxmox 側で手動運用
- k3s の永続化は `local-path` provisioner（node01 のローカルディスクを直接使用、PVC の
  `requests` は実容量を予約しない。`CLAUDE.md` / `docs/node01-storage.md` 参照）。
  つまり immich / vaultwarden / coder 等のアプリデータは node01 のディスク上に直接存在する。
  PBS がこれらを保護しているとすれば「VM 単位（node01 のディスクイメージ全体）」の
  バックアップのはずで、PBS の対象に node01 が含まれていなければアプリデータは
  一切バックアップされていないことになる
- リポジトリ内に Kubernetes レベルのバックアップ機構（Velero 等）は導入されていない
  （`apps/` 配下に該当マニフェストが無い）
- `Maintenance.md` / `CLAUDE.md` に、バックアップジョブのスケジュール・保持世代数・
  直近の成功/失敗・リストア検証の記録が無い

## わからないこと（実機アクセスが要る、PBS 時代の記録）

方針転換前に挙げていた確認事項。**T-0034 は dropped** につき、以下はもう追わない
（参考として残す）。

1. PBS に node01 (`qemu/113`) を対象にしたバックアップジョブが実際に設定されているか
2. 設定されている場合、スケジュールと保持世代数
3. 直近のバックアップジョブが成功しているか（失敗が放置されていないか）
4. リストアを実際に試したことがあるか（手順が機能する保証があるか）
5. pbs 自身（PBS VM そのもの）はバックアップ対象外のままで問題ないか。pbs を失うと
   node01 のバックアップも一緒に失う片系構成になっていないか

PBS の現況確認自体が要るなら T-0072（PBS 退役判断）の中で必要な分だけ見る。

## PBS 退役判断 (T-0072, 2026-08-06)

T-0071（復元試験）が immich・vaultwarden・coder-postgres の3コンポーネント全てで完了し、
いずれも数秒〜十数秒で復元できることを実測した（上記各節参照）。この結果と T-0065（IaC
完全性の棚卸し）を突き合わせて判断する。

**結論: 今はまだ PBS を退役しない。T-0078（coder workspace home PVC の backup）が
実装されるまで維持する。**

理由:

- T-0065 が確定した実データを持つ対象は5つ（`immich-library` / `immich-postgres-data`
  [immich 内蔵ダンプで代替] / `vaultwarden-data` / `coder-postgres-data` /
  `coder-<workspace-id>-home`）。このうち restic ベースの backup CronJob が実装・復元試験
  まで完了しているのは前者4つのみで、最後の1つ（コーダー workspace ごとの home PVC）は
  T-0078 がまだ `blocked` のまま未実装
- PBS が実際に node01 を対象にしたバックアップジョブを持っているか、workspace home の
  データを（意図せずであれ）保護しているかは「わからないこと」節のとおり未確認のまま。
  restic 側の代替が全データ対象に揃っていない状態で PBS を止めると、workspace home だけ
  バックアップが無い期間が生まれるリスクがある（確認していないものを「無い」とみなして
  安全側の判断をしない、CHARTER §4「high を戻せる形に落とす」と同じ理由）
- node01 VM 自体（VM イメージ全体）は Terraform（`terraform/proxmox`）+ NixOS image が
  完全に宣言的なため、VM 単位の復元は IaC の再適用で代替できる。PBS の VM 単位バックアップは
  この観点では既に冗長。残る唯一の価値は「T-0078 が塞ぐまでの workspace home の暫定的な
  保険」だけである

**T-0078 が完了し、workspace home も restic で復元試験まで確認できたら、5対象すべてが
アプリレベルの restic backup（復元 8〜16 秒、B2 の無料枠内）でカバーされる。その時点で
PBS の VM 単位バックアップは完全に冗長になり、退役してよい。** この退役の実行手順は
T-0116 として別途起票した（`blocked_by: T-0078`）。PBS VM 自体の停止・削除は Proxmox
管理コンソールでの操作が要り、autopilot の Proxmox credential は PVEAuditor（読み取り専用）
のため実行できない。人間の物理操作が要る点は T-0116 側で明記する。

**2026-08-07 追記（T-0117 完了、T-0116 再開）**: workspace home の初回 backup 成功
（`lastSuccessfulTime` 一致）と復元試験（`restore_rc=0`, 31秒, 3156ファイル, 925MiB）が
完了し、5対象すべてが restic backup + 復元試験でカバーされた。PBS の VM 単位バックアップは
理屈のうえでは冗長になったが、**PBS 自身が実際に何のジョブを持っているか（node01 を含む
VM 単位バックアップとして機能していたか）は依然未確認のまま**（上記「わからないこと」節、
方針転換で確認自体を打ち切っていた）。停止・削除の実行手順は
`terraform/proxmox/pbs.tf.ignore` に記載した。手順の最初のステップ（PBS 上の backup ジョブ
実在確認）は autopilot・構築セッションいずれも実行環境から PBS/Proxmox に到達できない
（autopilot にはこの実行環境に Proxmox credential が無い）ため、issue #56 で構築セッションに
確認を依頼した。

## これに依存しているタスク

- T-0029（immich postgres/vchord のメジャー更新、データを失いうる変更）・T-0023（coder
  v2.34.7 → v2.35.3、DB migration を伴う更新）・T-0027（immich メジャー更新）は
  CHARTER §4「データを失いうる変更」の手順どおり、バックアップの存在と復元手順が
  確かめられるまで着手しない。blocked_by は T-0071（restic ベースの復元試験）を指しており、
  それが通ってから再検討する
