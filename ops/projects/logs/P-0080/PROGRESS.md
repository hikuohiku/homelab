# P-0080 PROGRESS

(何も進んでいない。initializer は PROJECT.md の作成のみを行った — 2026-08-22)

## 2026-08-22 session (worker, 後半)

**重要: このセッションより前にも worker 起動があったが、何も commit せずに消えた。**
`ops/drills/restore_drill.py` (916 行)・`ops/tests/test_restore_drill.py` (47 tests)・
失敗した `report.json` が未追跡ファイルとして残っていた。本セッションでそれらを救出して
commit した。前セッションの思考は一切読めないので、以下は残骸からの復元 + 自分の実測。

### 前起動の失敗 run の復元記録 (report.json と cluster の実出力から)

- 2026-08-22T14:32 UTC に drill を実行。6 unit (5 対象 + workspace-home 2 分割) が
  ほぼ同時に `Stat(<config/>)` の段階から B2 403 で全滅し、どれも 1 バイトも復元できず
  activeDeadline (25分) までリトライを繰り返した。全 unit `rto_seconds: null`。
  スクリプト自体のバグではない (env 順序の初回バグ修正済みの痕跡はコメントにあり)。
- 失敗後、前起動は `drill-probe` namespace に手探りの probe Job (`restic snapshots`)
  を立てたが、これも 403 連打で deadline 失敗し、namespace を残したまま消えた。
  **本セッションで drill-probe ごと削除済み (2026-08-22 15:58 UTC)。drill-* namespace は 0 個。**

### 根本原因 (本セッションで raw B2 API を叩いて実証)

cluster 内 (drill-probe ns の ESO 同期 Secret = append-only 鍵) から B2 API を直接呼んだ:

- `b2_authorize_account` → **200 OK**。capabilities `listBuckets/readFiles/listFiles/writeFiles`
  (想定どおりの append-only 鍵)。bucket-restricted (`hikuoh***`)。
- `b2_list_file_names` (Class C) → **全 5 プレフィックスで OK** (syncthing/vaultwarden/
  immich/coder-postgres/coder-workspace-homes の `config` が見える)
- download (Class B / 帯域) → **HTTP 403 `download_cap_exceeded`**:
  `"Cannot download file, download bandwidth or transaction (Class B) cap exceeded.
  See the Caps & Alerts page to increase your cap."`

つまり credential・bucket 名・repo パスは全部正しく、**B2 アカウントの download 上限に
達しているだけ**。夜間 backup (08-22 JST 未明分) は 4 本とも Complete なので upload 側は無事。
docs/backup.md T-0067「B2 の無料枠 (10GB) に収まり月額ゼロ」= **無料枠アカウント**であり、
無料枠の download 目安は 1GB/day。

### なぜ上限に達したか

復元対象の合計が大きい: workspace general 2.973 GiB + test 925 MiB + immich 340 MiB +
他 3 対象 ≈ **4.2 GiB / 全体同時復元 1 回分**。14:32 より前に少なくとも 1 回走った
(コメントの env 順序バグ修正の痕跡、__pycache__ タイムスタンプ) 試行で予算を溶かし、
以後すべての試行 (14:32 の本命 run も含む) が最初の 1 リクエストから 403 になった。

### 構造的な発見 (これ自体が P-0080 の成果の一部)

**現在の B2 無料枠では「全体同時復元」は物理的に完走できない。** 1 回の完全復元に ~4.2 GiB
の egress が要るのに日次上限はその 4 分の 1 程度。真の node01 全損時に RTO は「復旧操作の
速さ」ではなく **B2 の帯域上限の回復待ち (数日)** で決まる。これは台帳に載せるべき数字
(RTO の下限条件) であり、解消は人間の管理コンソール作業 (cap 引上げ / 有料化) しかない
(CHARTER §4)。「発見」として curriculum に渡す。

### 今セッションでやったこと

1. 前起動の未 commit 成果物を検査・救出 (verify #1/#2 green を確認して commit)
2. **phase 0 preflight を追加**: 本命の restore 群を起こす前に最小リポジトリ (syncthing)
   だけで `restic snapshots` を試す使い捨て Job を立て、403 download_cap_exceeded を検出したら
   全体を即中断する。これがないと毎回 6 unit × 25 分のリトライに時間を溶かす (実測済みの罠)。
   `is_download_cap_error()` / `build_probe_job()` を純関数として切り出し、実測エラー文面
   2 形式でテスト固定 (tests 47 → 53)
3. drill-probe namespace を後片付け (上記)

### 次セッションへの引き継ぎ (これしか読まないので必読)

1. **まず preflight を通るか見る**: B2 の download cap は日次で回復する。回復時刻は不明
   (無料枠のカウンタは米国時間の深夜と思われるが未確認)。回復前なら `python3
   ops/drills/restore_drill.py` は phase 0 で数分以内に中断するので、それを確認したら
   **何もせず終えてよい**。むやみに再実行しない (Class B transaction の予算も共通で溶ける)
2. **回復していても単純再実行は失敗する可能性が高い**: 全体同時で ~4.2 GiB > 日次枠。
   中途半端に成功してまた予算を溶かすループを避けること。**人間による cap 引上げ /
   有料化が先** (PROGRESS の「構造的な発見」を curriculum / 人間に渡すのが正順)
3. verify #3 (`report.json` の 5 対象 × rto_seconds) は現状 failing のままで正しい。
   架空の数字で通してはいけない (PROJECT.md 方針 6)。失敗 run の report.json は
   commit 済み — 成功 run で上書きする
4. drill 実行時刻は JST 02:40–05:00 以外に (preflight がガードする)
5. 一時ファイルは mktemp 使え (固定パス /tmp の罠は既知)
