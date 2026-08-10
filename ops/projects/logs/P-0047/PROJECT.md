# P-0047 — 丸腰の syncthing-data を守り、「棚卸しの網が新入りを捕まえない」ことを潰す

## 目的

`apps/*/restic-backup-cronjob.yaml` は immich / vaultwarden / coder の 3 件だけで、
`syncthing-data` (20Gi) には backup が 1 つも無い。原因は syncthing 単体の見落としではなく
**手順の穴** — T-0065 のバックアップ対象棚卸しは 2026-08-05、syncthing の新設は 2026-08-06 で、
棚卸しの後に増えたものは誰も拾わない。しかも syncthing は削除が同期で伝播しうる唯一のアプリで、
事故が最も「戻せない」形で出る。P-0028 で append-only 鍵の型は確立済みなので新規発明はゼロ。

## 受入チェックリスト

initializer が実測した結果、**6 項目とも現時点で failing**
(2026-08-10、`project/p-0047` の checkout で、リポジトリルートから実行)。

- [ ] `test -f apps/syncthing/restic-backup-cronjob.yaml`
  — backup CronJob の manifest が存在すること。実測 rc=1 (apps/syncthing/ は
    pvc / deployment / service / service-tailnet / ingress / application / kustomization の 7 ファイルのみ)。
- [ ] `grep -q 'restic-backup-cronjob.yaml' apps/syncthing/kustomization.yaml`
  — **kustomization に登録されていること**。ファイルを置いただけでは ArgoCD は同期しない。実測 rc=1。
- [ ] `kubectl kustomize apps/syncthing | grep -q 'kind: CronJob'`
  — 実際にレンダリングされること (YAML が壊れていない・resources の綴りが合っている)。
    実測 rc=1 (render 自体は成功するが CronJob が 1 つも出ない)。**worker のイメージには
    `kubectl` v1.35 が入っており内蔵 kustomize で通る。syncthing は helmCharts を使わないので
    `--enable-helm` 相当は不要。**
- [ ] `grep -q 'syncthing' docs/backup.md`
  — 人間向けの対象一覧に載ること。実測 rc=1 (557 行中に "syncthing" が 1 度も出ない)。
- [ ] `python3 -m unittest ops.tests.test_backup_coverage -v`
  — 穴の再発を機械化したテストが存在し、かつ green であること。実測 rc=1 (モジュール自体が無い)。
- [ ] `test -f ops/projects/logs/P-0047/restore-drill.md`
  — 復元試験の実出力が残ること。実測 rc=1。

**verify は DoD の下限であって DoD そのものではない。** 6 本のうち 4 本は「文字列/ファイルがあるか」
しか見ない。とくに **DoD (2) の backup 成功実測と DoD (3) の復元一致は verify が一切見張っていない**
ので、`PROGRESS.md` と `restore-drill.md` に**コマンドと実出力をそのまま貼ること**が唯一の証拠になる。
`test_backup_coverage` も、免除リストに syncthing を足せば通ってしまう — 通し方を間違えないこと
(下の設計方針 3)。

## 設計方針

### 前提 (initializer が 2026-08-10 に実測・実読した。調べ直さなくてよい)

- **人間待ちの credential は無い。** ClusterSecretStore `doppler` はクラスタスコープなので
  syncthing namespace からそのまま引ける。必要な Doppler キー
  (`RESTIC_PASSWORD` / `RESTIC_B2_BUCKET` / `B2_ACCOUNT_ID`/`_KEY` / 同 `_APPEND_ONLY`) は
  **既に 3 namespace が使っている登録済みのもの**で、新規発行も新規登録も要らない。
  `kubectl get externalsecret -n syncthing` は現在 0 件 (これから作る)。
  → このプロジェクトに `needs-human` の一歩は存在しない。詰まったら credential 以外を疑う。
- **同型の手本は immich が最も近い** (`apps/immich/restic-backup-cronjob.yaml`)。vaultwarden は
  SQLite の online backup API を使う initContainer が付いていて構造が重い。immich 版は
  `restic snapshots || restic init` → `restic backup <path>` の 1 コンテナだけ。
- **1 ファイルに backup と retention の 2 CronJob を書く**のが 3 アプリ共通の型。参照する Secret が
  違う (backup = `<app>-restic-backup-credentials` (append-only) / retention =
  `<app>-restic-credentials` (削除鍵))。ExternalSecret も 1 ファイルに 2 本
  (`apps/vaultwarden/restic-external-secret.yaml` がそのまま雛形。コメントの Requires 節も含めて写す)。
- **append-only 鍵で backup も lock 除去も restore も通る** — P-0028 が本番 4 リポジトリで実測済み
  (restic の B2 backend は削除に `b2_hide_file` を使い `writeFiles` で足りる)。復元は `readFiles` で
  足りるので、**DoD (3) の restore も append-only 鍵のままでよい。削除鍵を持ち出さない。**
- **リポジトリパスは bucket の後ろの suffix で分ける** (`b2:$(RESTIC_B2_BUCKET):vaultwarden` 等)。
  既存は `vaultwarden` / `immich` / `coder-postgres` / `coder-workspace-homes`。→ `syncthing` を使う。
- **CronJob の schedule は JST で評価される** (node01 の `time.timeZone`、`spec.timeZone` は誰も
  書いていない)。既存の埋まり具合: backup 2:45 immich / 3:10 coder / 3:30 workspace-home /
  3:40 vaultwarden、retention は日曜 3:45 / 4:00 / 4:10 / 4:30。→ **backup `55 3 * * *`、
  retention `50 4 * * 0`** で衝突しない。
- **PVC の中身はまだ「syncthing 自身の config と identity」だけの可能性が高い。**
  T-0140 (旧 LXC 101 からの実データ移行) は `needs-human` のまま止まっており、人間の手元の
  ファイルはまだ流れ込んでいない。**これは着手を止める理由にならない** — `cert.pem`/`key.pem` は
  再発行すると既存ピアから別デバイスに見え、すでに十分「戻せない」データであり、そもそもこの
  プロジェクトの主眼は**データが来る前に受け皿を用意しておくこと**にある。ただし
  「ファイル数と sha256 が一致」の実測規模が変わるので、**手順 1 で実際の中身を数えて記録すること。**
- syncthing 2.1.3 は内部 DB が **SQLite (v2 系)** で、PVC (`/var/syncthing`) 上にある
  (`apps/syncthing/deployment.yaml` のコメント)。稼働中の Pod が書き続けている。
- CI の ops job は **ubuntu-latest + `python3` だけ**で回る (`.github/workflows/ci.yml:158` の
  `python3 -m unittest discover -s ops/tests -t .`)。**kustomize も helm も kubectl も無い。**
  PyYAML は使える (`ops/check_app_list_sync.py` が既に依存していて CI が通っている)。
- `ops/validate.py` の `check_autopilot_secret_allowlist` は **autopilot namespace 向けの
  ExternalSecret だけ**を見る。syncthing の ExternalSecret は対象外なので `rules.json` は触らない。

### 決めてあること (この方針で作る。変えるなら理由を PROGRESS.md に書く)

1. **追加するファイルは 2 つ**。`apps/syncthing/restic-external-secret.yaml`
   (`syncthing-restic-credentials` + `syncthing-restic-backup-credentials` の 2 本) と
   `apps/syncthing/restic-backup-cronjob.yaml` (`syncthing-restic-backup` +
   `syncthing-restic-retention` の 2 CronJob)。両方 `kustomization.yaml` の `resources` に足す
   (vaultwarden と同じ並び順: … restic-external-secret.yaml → restic-backup-cronjob.yaml)。
   retention の保持世代は既存 3 本と同じ `--keep-daily 7 --keep-weekly 4 --keep-monthly 6`。
2. **PVC は `readOnly: true` でマウントし、`runAsUser: 0` + `capabilities: drop ALL / add
   DAC_READ_SEARCH`** — PVC は PUID/PGID 1000 で書かれており、所有権に関わらず読む必要がある。
   immich/vaultwarden と全く同じ最小権限パターンを写す。**memory limits は付けない**
   (実測が無い。CPU limits だけ付ける — 既存 3 本と同じ)。`activeDeadlineSeconds` は
   immich/vaultwarden と同じ 3600。
3. **除外パターンは手順 1 で実物を見てから決める。** 内部 SQLite DB は稼働中コピーだと
   torn になりうる一方、**再スキャンで作り直せる派生キャッシュ**である。既定の方針は
   「`/var/syncthing` を丸ごと backup し、index DB のファイル群だけ `--exclude` する」。
   ただし **v2 の実ファイル名を確認せずに `--exclude` を書かない** — 綴りが違えば黙って
   何も除外されない (または除外しすぎる)。vaultwarden 方式 (online backup API の initContainer) は
   **採らない**: syncthing にとっての本体は同期ファイルと identity であって index DB ではなく、
   1 PR 1 論点を守る。決めた除外と理由を manifest のコメントと docs/backup.md に書くこと。
4. **テストは「PVC を宣言しているのに backup CronJob が無い app ディレクトリ」を落とす形にする。**
   `ops/tests/test_backup_coverage.py`、stdlib + PyYAML のみ、`apps/**/*.yaml` を静的に読む
   (`/charts/` は除外)。実装の骨:
   - `kind: PersistentVolumeClaim` の doc から (app ディレクトリ, PVC 名) を集める
   - その app ディレクトリに `kind: CronJob` かつ `restic` を含む manifest があり、かつ
     **その manifest が同ディレクトリの `kustomization.yaml` の `resources` に載っている**ことを要求する
     (ファイルを置いただけで配線し忘れる、が今回まさに verify #2 が見張っている失敗形)
   - **免除は理由付きの定数 dict で持ち、免除の側も腐らせない**。免除するのは今のところ 2 つ:
     `autopilot/autopilot-data` (器の作業領域。消えて失われるのは思考記録と実行中 Job の中間状態
     だけ、と `apps/autopilot/pvc.yaml` 自身が宣言している) と
     `immich/immich-postgres-data` (immich 内蔵の日次 DB ダンプが `immich-library` 内にあり、
     そちらが backup 対象。docs/backup.md の一覧に明記済み)。
     **免除エントリが指す PVC が repo に存在しなくなったらテストを落とす** — 免除リストが
     現実と切れたまま残るのが、今回潰そうとしている穴と同じ形だから
   - **既知の死角を docstring に書く**: helm chart がレンダリングする PVC (immich chart の
     valkey 等) と、Terraform が動的に作る `coder-<workspace-id>-home` は静的スキャンに映らない。
     CI の ops job に kustomize/helm が無い以上ここは埋められない。**「映らない」と書くこと自体が
     次の棚卸しへの引き継ぎになる** ので、伏せずに書く
   - テストが本当に落ちることを一度確かめる (免除も CronJob も無い状態を一時的に作るか、
     テスト内で合成した仮想ツリーに対して判定関数を呼ぶ形にする)。**判定はファイル走査と
     純関数に分け、純関数側を合成入力で固定するのが望ましい** (実 repo だけを見るテストは
     「今たまたま通っている」と「正しい」を区別できない)
5. **inventory と check_version_sync に登録する** (DoD (1) 後半)。`ops/inventory.json` の
   `targets` に `syncthing-restic-image` (`kind: image` / `current: 0.19.1` /
   `file: apps/syncthing/restic-backup-cronjob.yaml` / `match: "restic/restic:"` /
   `upstream: github:restic/restic` / `policy: auto` / `note` / `observability_impact`。
   `check_inventory` が id・kind・name・current・file・upstream・policy の非空と file の実在を検査する)。
   併せて `ops/check_version_sync.py` の GROUP
   「restic/restic backup CronJob image tag」に 5 本目の target を足し、**GROUP 名の
   inventory id 列挙も更新する**。タグは既存 4 ファイルと同じ **0.19.1** に揃える
   (揃っていないと CI が落ちる。ここでバージョンを上げない — 1 PR 1 論点)。
6. **実測の順序 (DoD 2・3)。飛ばすと「取れているつもり」で終わる。**
   1. **原本を数える**: `syncthing-data` を `readOnly` でマウントする使い捨て Job を 1 個作り、
      `find /mnt -type f | wc -l`・代表ファイル (`config.xml` / `cert.pem` / `key.pem`) の
      `sha256sum`・`du -sh` を出力する。ここで実ファイル名を見て手順 3 の `--exclude` を確定する。
   2. PR → CI → merge → ArgoCD sync。**`kubectl get cronjob -n syncthing` で実際に生えたことと、
      `kubectl get externalsecret -n syncthing` が 2 本とも `SecretSynced` / `Ready=True` に
      なったことを確認してから**次へ進む。確認せずに手動 Job を起こすと古い/無い定義のまま
      「成功した」と誤認する。
   3. **backup の実測**: `kubectl create job -n syncthing syncthing-restic-backup-manual-20260810
      --from=cronjob/syncthing-restic-backup` → `kubectl logs -n syncthing job/...`。
      rc・所要時間・追加されたスナップショット ID・残留 lock の有無を記録する。
      **スケジュール実行 (JST 3:55) と重なる時間帯を避ける** (`concurrencyPolicy: Forbid` は
      手動 Job には効かない)。
   4. **復元の実測**: 使い捨て PVC を `kubectl apply` で作り (例 `syncthing-restore-drill`、
      `local-path`、原本サイズ + 余裕。**`apps/` に commit しない** — ArgoCD 管理外の一時物)、
      `syncthing-restic-backup-credentials` を読む Job で
      `restic restore latest --target /restore` → 復元側の `find -type f | wc -l` と
      代表ファイルの `sha256sum` を出力する。
   5. **突き合わせ**: 代表ファイルの sha256 は原本と**完全一致**しなければならない。
      ファイル数は **`restic ls latest` が示すスナップショット自身の件数と復元結果を突き合わせる**
      — 稼働中の syncthing が書き続けるので、手順 1 で数えた原本の件数とはズレうる
      (ズレたらそれは事故ではないが、**ズレた事実と理由を書く**。黙って一致したことにしない)。
   6. **後片付け**: 手動 Job・drill Job・drill PVC を `kubectl delete` する。手動 Job は ArgoCD
      管理外なので prune されない。**消したことを PROGRESS.md に書く** (消し忘れは次の起動が
      「前回の中断」と誤認する残骸になる)。
   7. `restore-drill.md` に**コマンドと実出力をそのまま**貼る (要約しない。ここが唯一の証拠)。
7. **docs/backup.md への追記** (DoD 5)。「バックアップ対象一覧（2026-08-05 時点、T-0065 で確定）」の
   表に `syncthing-data` の行を足し、**その表が「2026-08-05 時点」であること自体が今回の穴の
   原因だった**ことを 1 行添える (実態とドキュメントの乖離を放置しない — CHARTER §1)。
   併せて「syncthing の restic バックアップ (P-0047)」節と「復元試験」節への追記を書く。
   書くのは何をどう取るか / 除外したものと理由 / 実測結果 / 戻し方。推測は「未確認」と明記する。

### ロールバック

追加のみの変更なので、revert PR 1 本で CronJob と ExternalSecret が消えるだけ。**既存データは
一切失われない** (PVC には readOnly でしか触らない)。B2 側に書かれたスナップショットは残るが、
append-only 鍵では消せないので放置してよい (数 MB〜。消したくなったら削除鍵を使う使い捨て Job)。
この 2 点を PR 本文のロールバック手順にそのまま書くこと。

## やらないこと

- **T-0140 (旧 LXC 101 からの実データ移行)**。`needs-human` のまま。ここでやるのは受け皿の用意で、
  中身を運ぶのは別論点。移行の可否をこのプロジェクトの前提条件にしない。
- **既存 3 アプリの backup/retention CronJob の変更**。schedule も保持世代も credential も触らない。
- **restic イメージタグの更新** (`check_version_sync.py` の restic GROUP)。0.19.1 に揃えるだけ。
  新しいタグが出ていても上げない (1 PR 1 論点)。
- **`.github/` の変更**。CI の既存 discover (`ops/tests`) が自動で拾う形に**寄せる**のが DoD (4) の
  眼目。新しい job を足すと ruleset の必須チェック追加が人間専有で、merge 待ちで止まる (P-0027)。
- **syncthing 用の `pvc-usage-cronjob.yaml` の新設**。実使用量の観測は別論点
  (気づきは PROGRESS.md に 1 行だけ書いて次へ渡す)。
- **`ops/rules.json` の変更**。人間レビュー必須パスであり、今回触る理由が無い
  (allowlist 検査は autopilot namespace 向けの ExternalSecret にしか効かない)。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` / CHARTER・VISION・`ops/memory/` の更新**。
  heart が直接 `main` に push するファイルでコンフリクトする (CLAUDE.md)。棚卸し漏れの再発防止を
  memory へ昇格させたくなっても、ここではやらない — PROGRESS.md に書いて consolidation に渡す。
- **B2 側の設定** (バケットのライフサイクル、Object Lock、鍵の capability 変更)。管理コンソール
  操作は人間専有 (CHARTER §4)。
- **drill 用 PVC / 手動 Job の manifest を `apps/` に commit すること**。ArgoCD の管理対象に
  入れると prune と `.spec.template` immutable の両方を踏む。使い捨ては使い捨てのまま
  `kubectl` で作って消す。
