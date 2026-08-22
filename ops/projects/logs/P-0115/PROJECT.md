# P-0080 — 復旧を製品として測る — 全 backup 対象を隔離環境へ同時復元し、アプリが生き返るまでの壁時計 (RTO) を秒で出して宣言する

## 目的

backup CronJob 5 対象は配線済みだが、実際に戻したのは syncthing 1 件だけ (P-0047) で、
残り 4 件は「取れている」止まり。node01 全損演習 (P-0054) は採択されたものの 2026-08-12 の
人間の「止めて」で打ち切られた — 案の質の問題ではないので、より大胆な形での再提案が許される。
今回は名指し調査ではなく**全対象の同時復元演習**に踏み込み、「開始から生き返りまでの壁時計
(RTO)」という数字を初めて持ってくる。測られない限り「復旧できる」は信念のまま。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-22、`project/p-0080` の checkout で、リポジトリルートから実行)。

- [ ] `test -f ops/drills/restore_drill.py`
  — ドリル本体スクリプトが存在すること。実測 rc=1 (`ops/drills/` ディレクトリ自体が無い)。
- [ ] `python3 -m unittest ops.tests.test_restore_drill`
  — 復元計画・RTO 計算・report スキーマの判定ロジックにテストが存在し green であること。
    実測 rc=1 (モジュール自体が無い)。CI の ops job は
    `python3 -m unittest discover -s ops/tests -t .` なので置けば自動収集される。
- [ ] `python3 -c "import json; r=json.load(open('ops/projects/logs/P-0080/report.json')); assert len(r['targets'])>=5 and all(t.get('rto_seconds') is not None for t in r['targets'])"`
  — 実際に走った drill の実測 report が残り、5 対象以上が各々数値の RTO を持つこと。
    実測 rc=1 (FileNotFoundError。手で作れば通るので **worker が drill を実行しないまま
    架空の report.json を書けば通ってしまう — 通し方を間違えないこと**)。

**verify は DoD の下限であって DoD そのものではない。** 3 本とも「ファイルがあるか / 数値が
入っているか」しか見ない。実行ログと kubectl の実出力を `PROGRESS.md` に残すことが唯一の証拠になる。

## 設計方針

### 前提 (initializer が 2026-08-22 に実読した。調べ直さなくてよい)

- **5 対象の実態は「PVC の丸ごと」ではない**。restic リポジトリ
  `b2:$(RESTIC_B2_BUCKET):{vaultwarden,immich,coder-postgres,coder-workspace-homes,syncthing}`
  に入っているものが正体:
  - `vaultwarden-data` — SQLite 本体 (online backup API で一貫コピー済み db.sqlite3) +
    attachments 等
  - `immich-library` — ライブラリ本体 + immich 内蔵の日次 DB ダンプ
    (`backups/*.sql.gz`。immich-postgres-data 相当を含む)
  - `coder-postgres-data` — **PGDATA ではなく `pg_dump -Fc` ダンプ 1 ファイル**
    (initContainer がネットワーク経由で取得)。raw PGDATA を戻しても動かない
  - `coder-workspace-homes` — 動的 PVC `coder-<workspace-id>-home` の集合。
    snapshot は `--host <workspace-id>` タグ付きで 1 リポジトリ共有
  - `syncthing-data` — identity (`cert.pem`/`key.pem`) + 設定。index DB (`config/index-v2/`)
    は意図して除外済み (docs/backup.md「除外するものと、その理由」)
- **credential は新規登録不要**。各 namespace の `<app>-restic-backup-credentials`
  (append-only 鍵) で restore は通る — 復元は `readFiles` で足りる (P-0028 実測)。
  削除鍵を持ち出さない。
- **restore Job の既知の型**: `CHOWN` / `FOWNER` / `DAC_OVERRIDE` の 3 capability + restore 前
  `rm -rf` クリーンアップが必須 (docs/backup.md 復元試験の教訓。P-0047 が読まずに再踏んだ)。
  過去実測の所要: immich 16s / vaultwarden 9s / coder-postgres (pg_restore 検証のみ) 8s /
  workspace-home 31s / syncthing 27s — ただしこれらは「restore コマンドの所要」であって
  「生き返りまでの壁時計」ではない。
- **worker の実行環境** (`ghcr.io/hikuohiku/homelab-autopilot`): git / python3 (+py3-yaml) /
  curl / bash / restic / kubectl v1.35 入り。この spec には `kubectl-write` capability が
  宣言されており、予告済みプロジェクトの Job には writer SA が注入される — drill 用の一時
  オブジェクトを `kubectl` で直接作って消してよい (CHARTER §5 の書き込み禁止は capability
  宣言が上書く)。ただし **本番 namespace への適用は依然禁止**。
- **CI の ops job は ubuntu-latest + python3 だけ** (PyYAML は使える)。cluster 接続なしで
  `ops/tests/test_restore_drill.py` が green になる必要がある → 判定ロジック (計画生成 /
  RTO 計算 / report スキーマ検査) を純関数に分離し、合成入力で固定する。
- **夜間帯との重なりを避ける**: backup 2:45/3:10/3:30/3:40/3:55、retention 日曜
  3:45–4:50 (すべて JST)。drill は snapshot の読み取りしかしないが lock 競合の警告を出さない
  ため、**JST 02:40–05:00 の帯での実行を避ける**。

### 決めてあること (この方針で作る。変えるなら理由を PROGRESS.md に書く)

1. **`ops/drills/restore_drill.py` は stdlib python3 のオーケストレータ。** kubectl subprocess
   で `drill-*` 名の隔離 namespace に新規 PVC (local-path) + restore Job (restic/restic イメージ
   0.19.1、既存 backup と同じ pin) を apply し、全対象を同時に起動して共通の開始時刻から
   各対象の liveness 判定合格までの壁時計を秒で計る。使い捨て物を `apps/` に commit しない。
2. **liveness 判定はアプリ相当の最低限**: coder-postgres は `pg_restore` を新規 cluster
   (postgres:17.10、サーバと同バージョン) へ流し込んで `pg_isready`。vaultwarden は
   SQLite マジックバイト + `PRAGMA integrity_check`。immich-library は最新 `.sql.gz` の
   gzip 整合性 + ファイル数>0。workspace-home は `restic ls --host <id>` の件数と復元結果の
   突き合わせ。syncthing は `config.xml` の XML パース + cert/key 存在。判定基準は
   スクリプト内の定数として明示し、テストからも使う。
3. **RTO は「PVC 作成要求の時刻」から「liveness 合格の時刻」までの wall clock**。
   待ち時間 (image pull / PVC provision) も含めた誇張なしの数字にする。結果は
   `{"targets": [{"name", "namespace", "rto_seconds", ...}]}` 形で
   `ops/projects/logs/P-0080/report.json` に書いて commit する (これが verify #3)。
4. **docs/backup.md に「RTO 台帳」の節を足す。** 既存の節は書き換えず追記のみ。表は
   対象 / 実施日 / rto_seconds / 備考 (規模・特記)。次回以降の drill がこの表に行を足す形にする。
5. **後片付けまでが drill。** drill namespace ごと delete し、消したことを PROGRESS.md に
   書く。残骸は次の起動が「前回の中断」と誤認する。
6. **失敗対象があっても報告を隠さない。** ある対象が生き返らなければその `rto_seconds` は
   null にせず、verify #3 を通すためだけに成功と偽装しない — 失敗は事実と実出力を
   PROGRESS.md に残して終了し、判断 (分割・原因調査の起票) を引き継ぐ。

## やらないこと

- **node01 全損演習・データでない資産の棚卸し** (k3s CA / tailscale identity / Doppler 一覧 /
  docs/disaster-recovery.md)。これは P-0054 の領域で、今回の spec は明確に「backup 5 対象の
  復元」に絞っている (1 PR 1 論点)。
- **既存 backup / retention CronJob の変更**。schedule も保持世代も credential 参照も触らない。
- **本番 PVC・本番 namespace への一切の書き込み**。drill は `drill-*` namespace の新規 PVC のみ。
- **apps/ 配下への drill 用 manifest の commit**。ArgoCD 管理に入れると prune と immutable を
  踏む。使い捨ては script が生成して消す。
- **新しい Doppler credential の登録依頼**。append-only 鍵で完結する (前提節どおり)。
- **B2 側の設定変更** (ライフサイクル、Object Lock)。管理コンソール操作は人間専有 (CHARTER §4)。
- **RTO の常時監視・アラート化・dashboard / ops-health-reporter への組み込み**。今回は台帳
  (docs/backup.md) まで。気づいたら PROGRESS.md に 1 行書いて次へ渡す。
- **PBS 退役判断 (T-0116) の再検討**。RTO 数字が出ても PBS の扱いをここで変えない。
- **backup 対象自体の拡大** (immich-postgres-data の生 PVC 等)。現行の 5 対象を測るのが本題。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` / memory の更新**。heart が直接
  `main` に push するファイルでコンフリクトする (CLAUDE.md)。
