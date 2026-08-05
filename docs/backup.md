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
| `immich-library` | 写真/動画原本・サムネイル・変換済み動画 | T-0068 |
| `immich-postgres-data` | immich アセットメタデータ・CLIP埋め込み | T-0068 と同時（T-0029 側で拡張検討） |
| `vaultwarden-data` | パスワードマネージャ本体（SQLite） | T-0069 |
| `coder-postgres-data` | Coder 制御プレーン（ユーザー・workspace・監査ログ） | T-0070 |
| `coder-<workspace-id>-home`（動的作成、`apps/coder/templates/personal/main.tf`） | workspace ごとの `/home/coder`（dotfiles・ghq clone 等） | **未起票**。T-0070 の対象外（別 PVC 群）のため、別途タスク化が必要 |

## vaultwarden の restic バックアップ (T-0069, 実装済み・credential 登録待ち)

`apps/vaultwarden/restic-backup-cronjob.yaml` に2つの CronJob を実装した。

- **`vaultwarden-restic-backup`**（毎日 03:40 UTC）: `/data` を直接 restic に渡すと
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
- **`vaultwarden-restic-retention`**（毎週日曜 04:00 UTC）: `restic forget --keep-daily 7
  --keep-weekly 4 --keep-monthly 6 --prune`
- 保存先は Backblaze B2（`b2:<bucket>:vaultwarden`）。immich（T-0068）・coder-postgres
  （T-0070）も同じ bucket・同じ credential でパス末尾だけ変える設計にした
- **復元時の注意**: 復元前に vaultwarden コンテナを止め、既存の `db.sqlite3-wal`/`db.sqlite3-shm`
  を削除してから復元後の `db.sqlite3` を配置すること（stale な WAL が残ると起動時に不整合を起こしうる）

### 必要な Doppler 登録（T-0067）

`apps/vaultwarden/restic-external-secret.yaml` が参照するキー。immich/coder-postgres 実装時も
同じキーを使い回す想定（バケットを分けたい場合は別途相談）。

| Doppler キー | 内容 |
|---|---|
| `RESTIC_PASSWORD` | restic リポジトリの暗号化パスワード（新規に決めて登録） |
| `RESTIC_B2_BUCKET` | Backblaze B2 のバケット名 |
| `RESTIC_B2_ACCOUNT_ID` | B2 application key ID（**append-only** キーを推奨。node01 が乗っ取られてもバックアップを消せないように） |
| `RESTIC_B2_ACCOUNT_KEY` | 同上 application key |

登録後、`ops-health-report`（`pod_issues`）で `vaultwarden-restic-backup` CronJob の Job が
Failed になっていないことを確認できれば実際に動作したとみなせる（T-0097）。

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

## これに依存しているタスク

- T-0029（immich postgres/vchord のメジャー更新、データを失いうる変更）・T-0023（coder
  v2.34.7 → v2.35.3、DB migration を伴う更新）・T-0027（immich メジャー更新）は
  CHARTER §4「データを失いうる変更」の手順どおり、バックアップの存在と復元手順が
  確かめられるまで着手しない。blocked_by は T-0071（restic ベースの復元試験）を指しており、
  それが通ってから再検討する
