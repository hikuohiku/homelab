# P-0142 — 単一ノードで最も高い固定費を見限れるか — PBS (qemu/112) が守っているものを実測で読み、退役の可否を器が決める

## 目的

pbs (qemu/112) は Terraform 管理外で node01 の RAM (8 GiB dedicated) とディスク (64 GB) を
食い続ける手動管理 VM。退役の前提だった「coder workspace home backup の復元試験」は restic 側で
完了済み (T-0071/T-0117、docs/backup.md) なのに、**PBS 側の実体 — 何を・いつまで・何世代守って
いるか — を読んだ者は一度もいない** (T-0072 も「PBS 自身が何のジョブを持っているか未確認」のまま
棚上げ)。Proxmox MCP (read-only) で PBS の稼働状況と backup ジョブを実測し、restic/B2 側の対象
一覧と突き合わせて「PBS にしか無いもの」の有無を確定する。退役の可否の判断は器が下し、人間には
`qm shutdown 112` からの実行コマンドだけを渡す。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0142` の checkout で、リポジトリルートから実行)。

- [ ] `test -f docs/pbs-retirement.md && grep -qE '112|PBS' docs/pbs-retirement.md`
  — 退役手順書が実在し、対象 VM (112/PBS) について言及していること。実測 rc=1 (ファイル未存在)。
- [ ] `python3 -c "import json; d=json.load(open('ops/projects/logs/P-0142/pbs-inventory.json')); assert d.get('jobs') is not None and d.get('verdict') in ('retire','keep','partial')"`
  — 実測インベントリに jobs 一覧 (取得失敗時は空配列 + その記録) と verdict
  (`retire`/`keep`/`partial` のいずれか) が入っていること。実測 rc=1 (ファイル未存在)。
- [ ] `grep -qE 'qm shutdown|切り戻し|rollback' docs/pbs-retirement.md`
  — 手順書が停止コマンド (`qm shutdown`) から始まり、切り戻し条件を明記していること。
  実測 rc=2 (ファイル未存在)。

**verify は DoD の下限であって DoD そのものではない。** dod (2) の「『PBS にしか無いもの』の
有無を表にする」、dod (3) の「切り戻し条件を書く」、dod (4) の pbs.tf.ignore コメント更新は
verify が見張らない — PROGRESS.md に証跡を残すこと。逆に言えば **verdict を `partial` や
`keep` にしても verify は通る**。「可なら docs/pbs-retirement.md を書く」の「可」の判断と
根拠を PROGRESS.md に残すこと (不可なら手順書は書かない。この場合 verify 1・3 項目目は
green にならないままプロジェクトが完結しうる — spec dod (2) の「その結論も成果」)。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した。調べ直さなくてよい)

- `terraform/proxmox/pbs.tf.ignore` に PBS VM の構成記録がある: 4 cores / 8192 MiB dedicated /
  64 GB (local-lvm, raw)、SeaBIOS、PBS ISO 4.1-1 から導入。Terraform 管理外 (`.ignore` 拡張子)。
  **誤って `.tf` に戻さないこと** (稼働中バックアップサーバーを破壊するリスク、ファイル冒頭の警告)。
- 同ファイルに T-0072 (2026-08-06) 由来の退役手順草案がある: backup ジョブ実在確認 →
  `qm shutdown 112` → 1〜2 週間観察 → `qm destroy 112` → 関連ドキュメントを past tense 化。
  ただし草案当時「PBS 自身の backup job 設定・保存済みバックアップの一覧」は未確認のまま
  (= 本プロジェクトが埋める穴)。また「この in-cluster 実行環境に Proxmox credential が無い」
  (2026-08-07 実測) という記述がある — **MCP 経由で取れるかは worker が最初に確認すべきこと**。
  取れなければその事実自体が成果 (下記「決めてあること」参照)。
- restic/B2 側の対象一覧は `docs/backup.md` が一次情報源: 対象は 6 つ
  (`immich-library` [内蔵 DB ダンプ同梱] / `immich-postgres-data` [内蔵ダンプで代替] /
  `vaultwarden-data` / `coder-postgres-data` / `coder-<workspace-id>-home` / `syncthing-data`)。
  単一 B2 バケットにパス末尾で分離 (`vaultwarden`/`immich`/`coder-postgres`/
  `coder-workspace-homes`/`syncthing`)、保持は全件 `--keep-daily 7 --keep-weekly 4
  --keep-monthly 6`。復元試験は T-0071 (immich 16秒/vaultwarden 9秒/coder-postgres 8秒)・
  T-0117 (workspace home 31秒)・P-0047 (syncthing, sha256 突き合わせまで) で全部完了済み。
- **P-0080→P-0115 (RTO 復元演習系) は本ブランチに未 merge** (2026-08-23 実測:
  `project/p-0115` ブランチは存在するが main 未統合、`ops/drills/` も logs も本 checkout に無い)。
  突き合わせは docs/backup.md を根拠にやること。P-0115 が先に merge されたら補足に使ってよいが、
  **それを待って止まってはいけない**。
- T-0066 の「B2 合計 1 GiB 未満」実測 (2026-08-05) は一回きりで、B2 側の現サイズは不明。
  本プロジェクトでは再計測しない (やらないこと参照)。
- verdict の語彙: `retire` = PBS にしか無い守りが無く退役可 / `keep` = PBS にしか無い守りがあり
  退役保留 / `partial` = 条件付き (例: 特定対象だけ退役し一部維持、または観察期間後に再判断)。
- 人間に渡してよいのは「物理/管理コンソールの手作業」だけ (CHARTER §4)。`qm shutdown` 以降の
  実行は本プロジェクトでは一切しない。

### 決めてあること

- **`ops/projects/logs/P-0142/pbs-inventory.json`**: `{"captured_at", "source", "vm", "jobs",
  "restic_targets", "pbs_only", "verdict", "reason"}` を最低限持つ。`jobs` は null にしない
  (verify の要求)。**Proxmox MCP で取れた事実だけを載せ、取れなかったら `source` に
  `"unreachable"` 等を残して `jobs: []` + 失敗の記録を添える。推測値を埋めない**
  (substrate「推測を書かない」と同じ流儀)。
  - 層の区別に注意: Proxmox VE 側の vzdump backup job (/cluster/backup 相当。スケジュール・
    対象・retention) と、PBS 自身の中の sync/prune/verify job は別物で、後者は PVE 側 API からは
    見えない。MCP で読める範囲と読めない範囲を inventory に明記すること
    (「何世代守っているか」が読めなかった場合、それは verdict を保守側に倒す材料になる)。
- **突き合わせ表** (dod 2): restic 対象 6 行 × 「PBS が守っているか」の列。PBS にしか無いものが
  1 行でもあれば `verdict` は `keep` または `partial` になり、「どう埋めるか」を結論として
  PROJECT ログ (PROGRESS.md) に書く。node01 VM 全体のイメージ単位バックアップが PBS の唯一の
  機能だった場合は IaC 再適用で代替可能 (T-0072 の既存判断) なので `retire` 側に寄る。
- **`docs/pbs-retirement.md`** (verdict が retire/partial のとき書く): 既存 docs の文体
  (docs/node01-storage.md / docs/pveproxy-tls.md 参照) で、(a) 実測サマリ、(b) `qm shutdown 112`
  から始まる人間実行の手順 (T-0072 草案を実測で更新したもの。確認コマンド付き)、(c) 停止後の
  観察期間と確認ポイント、(d) **切り戻し条件** — `qm destroy` 前なら `qm start 112` で可逆、
  destroy 後は pbs.tf.ignore の記録 + ISO からの再構築になること、どんな事象が起きたら戻すか
  (例: VM レベルバックアップの必要性発覚 / B2 バケット喪失) — を必ず書く。
  verdict が `keep` のときは手順書を作らず、保留理由と「これが揃えば可になる」条件を
  PROGRESS.md に残す (verify 1・3 が green にならない完結を許容する。上記のとおり)。
- **`terraform/proxmox/pbs.tf.ignore` のコメント更新** (dod 4): 「確認できていないこと」節を実測
  結果で置き換える。resource 本体・経緯節は触らない。`.ignore` のまま維持。

## やらないこと

- **`qm shutdown 112` / `qm destroy 112` の実行**。spec 明記どおり手順を書くだけ (実行は人間)
- **terraform への復帰・apply**。pbs.tf.ignore はコメント更新のみで、`.tf` には戻さない
- **restic/CronJob/B2/Doppler 側への変更**。backup体制自体には触れない (突き合わせは読むだけ)
- **T-0066 の B2 サイズ再計測・P-0115 の RTO 再演習**。別プロジェクトの論点。B2 credential は
  エージェント環境に無い (substrate 実測) のでそもそも届かない
- **SOPS 暗号化ファイル・クラスタ書き込み・main 直 push** (CHARTER §5)
- **CLAUDE.md / README.md / Plans.md の pbs 記述の past tense 化**。退役が実際に実行された後の
  別 PR (T-0072 草案ステップ 5 の位置づけ)
- **ops/backlog.json / state.json / journal の編集**。autopilot が直接 push する領域で
  コンフリクトする (CLAUDE.md)
