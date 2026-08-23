# P-0216 — B2 の download cap で backup が全滅した夜を二度と作らない — 全消費者の転送量を事前見積りで一枚にし、CI が「cap を食い潰すスケジュール」を落とすようにする

## 目的

2026-08-22 の一次原因は Backblaze B2 のアカウント単位 download cap 超過
(`ops/projects/logs/P-0111/root_cause.md` 実測)。P-0128 が作ったのは「誰が消費したか」の
**事後の帳簿と計器**であって事前の歯止めではない。消費者は restore drill・bit rot 読み
(P-0187)・週次 restic 健康診断 (P-0102 系) と今後確実に増え、個別には正しいジョブでも
**合計を見ている者がいない**限り cap を超える。cap は毎日 00:00 UTC リセットなので、
JST 夜に密集した重いジョブ同士が互いを殺す構造は放置すれば再演する — 台帳 1 枚と
「cap を食い潰すスケジュール」を落とす CI 検査で構造的に塞ぐ。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0216` の checkout でリポジトリルートから実行)。

- [ ] `python3 ops/b2/budget.py --check`
  — B2 消費者の台帳生成と 2 つの検査 ((a) 合計想定転送量が日次 cap × 安全係数内、
    (b) 重い消費者が同一時間帯・リセット境界付近に非密集) が rc=0 で通ること。
    実測 rc=2 (`ops/b2/budget.py` が存在しない)。
- [ ] `grep -q 'b2/budget.py' .github/workflows/ci.yml`
  — CI への配線があること。新しく B2 を使う Job が台帳未登録のまま混ざったら落ちる歯止め
    が実際に回る。実測 rc=1 (ci.yml に該当文字列なし)。
- [ ] `python3 -c "import sys; sys.exit(0 if __import__('pathlib').Path('docs/backup.md').read_text().count('download cap') >= 2 else 1)"`
  — 各消費者の実測転送量と判断根拠が `docs/backup.md` に記録されていること。
    実測 rc=1 (`download cap` の出現 0 回)。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した)

- **現時点の B2 消費者は manifest 上に backup 5 本 + retention 5 本**
  (`apps/{immich,vaultwarden,syncthing}/restic-backup-cronjob.yaml` +
  `apps/coder/restic-backup-cronjob.yaml` + `apps/coder/workspace-home-backup-cronjob.yaml`)。
  schedule は `spec.timeZone` 未指定のため JST 評価 = UTC 換算では**夜の単一帯に密集**:
  backup が毎日 17:45–18:55Z、retention が日曜 JST = **土曜 18:45–19:50Z に 5 本全部**。
  cap 超過した 08-10 と 08-22 はどちらも土曜であり retention 一斉稼働日と一致
  (root_cause.md「オープンな疑問」節)。つまり --check は現状で違反を実際に挙げるはずで、
  それが DoD(4) の分散対象になる
- **消費者ごとの推定値の種は既にある**: P-0128 の download-ledger 4 ファイルの
  `LEDGER_RULES` (backup ≈ 32 MiB / retention ≈ 512 MiB。「桁感であり実測ではない」と明記済み)。
  budget.py はここを出発点にし、docs 側で実測値に差し替え可能な形にする。
  restore 試験 5 件の実測サイズは既に docs/backup.md に記録がある
  (immich 332 MiB / workspace home 925 MiB 等)
- **cap の性質は確定済み**: アカウント単位・鍵の種類に無関係・usage counter は毎日
  00:00 UTC リセット (公式ドキュメント + p0111-cap-watch の 08-23T00:04Z 回復実測)。
  **cap の実値 (何 GB か) は B2 コンソールにしかなく repo 外** — P-0128 が
  `DEFAULT_DAILY_CAP_BYTES=None → unconfigured` で正直に沈黙する設計にした経緯がある。
  budget.py の閾値はモジュール定数として出所ごと書き、--check が常に green になる形を
  最初の設計点として決めること (verify #1 が rc=0 であることが受入条件)
- 「B2 を使う Job」の機械抽出は静的 scan で足りる: `RESTIC_REPOSITORY` の `b2:` プレフィックス
  または `*-restic-*-credentials` Secret 参照 (workspace-home の ConfigMap 埋め込み
  オーケストレータ内テンプレートも同じ特徴を持つ)。restore drill / 週次健康診断は
  **main には未稼働** (P-0102 は SUSPEND=True の別ブランチ設計、復元試験 Job は使用後削除の運用)
  なので、抽出対象は現存 manifest + 「新規混入を台帳未登録で落とす」登録必須化で将来分をカバーする
- CI 配線の定石は ci.yml consistency checks ブロックへの 1 行追加
  (`check_download_ledger_script_sync.py` など 8 本が並ぶ直近の前例)。
  PyYAML は CI の python 環境で利用可 (`test_backup_coverage.py` が依存)
- immich 内蔵 DB ダンプ (02:00 UTC のアプリ内タイマー) は k8s CronJob ではなく B2 を読まないため
  台帳対象外

### 作るもの

1. `ops/b2/budget.py` — manifest から B2 消費者を機械抽出し、「対象・schedule・想定 download 量・
   リセット境界 (00:00 UTC) からの位置」の台帳を 1 枚に出す。`--check` は (a) 合計 vs 日次 cap ×
   安全係数、(b) 重い消費者の同一時間帯・境界付近密集、(c) 台帳未登録の B2 消費者の混入、を検査し
   違反なら rc=1 で実名を挙げる。stdlib (+ 既存流儀どおり PyYAML 可)。
2. `.github/workflows/ci.yml` — consistency checks へ `--check` を追加するのみ。
3. **違反の解消** — 検査が挙げた対象 (現状なら土曜夜 retention の密集) をこの PR 内で schedule 分散
   させて落とす。触るのは開始時刻のみ。
4. `docs/backup.md` — 各消費者の想定/実測転送量と判断根拠の節を追記する。

## やらないこと

- **backup CronJob の廃止・保持世代・retention 方針の変更**。分散は「いつ動くか」の調整のみ。
  日次バックアップは単一障害点であり CHARTER 流儀どおり触らない
- **B2 API 叩き・新しい credential の要求・cap 実値の確認/引き上げ**。budget.py は repo 内
  manifest の静的分析に限定 (P-0128 と同じ線引き)。管理コンソール作業 = 人間専有 (CHARTER §4)
- **restore drill / 週次健康診断 (P-0102/P-0116/P-0114/P-0115/P-0187) の実装そのもの**。
  それらが台帳に載る枠組みを作るまで (1 PR 1 論点)
- **P-0128 の産出側 (download-ledger CronJob)・report.py・heart 警報の改変**。事後計器と事前歯止めは
  別論点。budget.py の推定値との突き合わせは docs への記録にとどめる
- **通知機構の新設**。落とすのは CI のみで、Discord/issue への新経路は作らない
  (VISION「器を太らせる前に使い切る」)
