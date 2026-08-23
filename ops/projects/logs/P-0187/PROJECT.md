# P-0187 — 新鮮で腐っているかもしれない — 全 restic リポジトリの実データを --read-data-subset で数ヶ月回転読みし、黙った bit rot を取り出す当日ではなく最初の月で捕まえる

## 目的

バックアップの守りは鮮度 (P-0157)・復元速度 (P-0080/114/115)・改変不能性 (P-0168) と順に厚くなったが、
**保存されたデータそのものを読み返して壊れていないか確認した者はいない** (apps/ 配下に
restic check / read-data-subset は 1 行も存在しないことを本日実測)。freshness 計測は「snapshot の
記録が更新された」ことしか見ず、B2 は 11 ナインを保証しない一般オブジェクトストアなので、データ破損は
最後の復元試験以降いつでも入り得て、発見は取り出す当日になる。`--read-data-subset` なら B2 download cap
(P-0128 台帳の実測あり) に配慮しつつ全データを約 3 ヶ月の回転で読み切れる。#49 型「失敗すら起こらない
劣化」の最後の未開拓地。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0187` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f ops/tools/restic_integrity.py`
  — 回転選択ロジックの正本が指定パスに存在すること。
  実測 rc=1 (ファイル未存在)。
- [ ] `python3 -m unittest ops.tests.test_restic_integrity`
  — 決定論的で再実行可能な回転選択ロジックが unittest で固定されていること。
  実測 rc=1 (`ModuleNotFoundError: No module named 'ops.tests.test_restic_integrity'`、FAILED errors=1)。
- [ ] `test "$(grep -rl 'read-data-subset' apps --include='*.yaml' | wc -l)" -ge 4`
  — apps 配下に `--read-data-subset` を実際に使う integrity 定義 (CronJob) が入った yaml が
  4 個以上あること。
  実測 rc=1 (該当 0 ファイル)。注記: initializer サンドボックスの grep は BusyBox で `--include`
  非対応のため、find+xargs と全文検索ツールの 2 経路で「0 ファイル」を裏取りした。CI (GNU grep)
  ではこのコマンド列がそのまま成立する。
- [ ] `grep -qiE 'integrity' apps/ops-health-reporter/report.py`
  — 結果の health レポートへの畳み込み配線が存在すること。
  実測 rc=1 (該当文字列なし。report.py の収集キー実測: applications / pod_issues / pvcs / nodes /
  pod_metrics / node_metrics / pvc_usage / download_budget / externalsecrets / autopilot)。

**verify は DoD の下限であって DoD そのものではない。** verify が直接見ないもの —
(1) 回転が実際に決定論的で再実行可能か (同じ日に再走すれば同じスライスを読む。verify 2 は
テストの存在しか見ない)、(2) download cap を消費し過ぎないスケジュール分散の根拠が manifest
コメント等に残っているか (verify 3 は文字列の数しか数えない)、(3) 累積カバー率の計算が
嘘をついていないか (Job 履歴は消えるので長期記憶の設計次第) — は機械検査不能なので、worker が
PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- **restic リポジトリの実在一覧** (各 CronJob の `RESTIC_REPOSITORY` 実測):
  `b2:<bucket>:{vaultwarden, immich, coder-postgres, coder-workspace-homes, syncthing}` の 5 本。
  spec 列挙の「autopilot-core 相当」に対応する restic リポジトリは**実在しない**
  (`apps/autopilot-core/` には PVC があるが restic backup が無い)。実在する 5 本が対象。
  verify #3 の閾値 4 は「vaultwarden / immich / syncthing / coder の 4 namespace 分の yaml」に一致し、
  workspace-homes 分を足せば 5 ファイルになる
- **既存パターン**: 各アプリの `restic-backup-cronjob.yaml` は「1 ファイルに backup + retention の
  2 CronJob」。integrity は第 3 の CronJob として同ファイル群に足すのが流儀。credential は既存の
  backup 用 append-only 鍵 Secret (`<app>-restic-backup-credentials`) で足りる (check は読み取りのみ)。
  image は `restic/restic:0.19.1` pin、`concurrencyPolicy: Forbid`、`automountServiceAccountToken:
  false`、memory limits は付けない (substrate: 実測の裏付けなしに付けない)
- **回転の仕組み**: `restic check --read-data-subset=N/T` の N/T 形式は pack 集合を T 分割した第 N スライスを
  読む (restic 公式仕様)。**パーセント形式はランダム抽出であり回転にならない** — N/T 形を使う根拠。
  T=3・月次実行なら「約 3 ヶ月で一周」が spec の数値どおりそのまま成立。N は「エポック日からの経過日数
  ベース」等の決定論的関数から導き、同じ日の再実行は同じスライスになる (再実行可能)。この選択関数を
  `ops/tools/restic_integrity.py` の純関数として出し、ops/tests 流儀 (合成入力で両方向固定) の unittest で
  「同じ入力→同じ出力」「周期ごとに全スライスを巡る」「範囲外の日付で壊れない」を固定する
- **B2 download cap の前提** (P-0111 root_cause.md 確定分): アカウント単位・鍵の種類に無関係・
  毎日 00:00 UTC リセット。台帳 (P-0128) の推定は backup 1 回 ≈ 32 MiB、retention 1 回 ≈ 512 MiB。
  **注意: spec の参照先「P-0183 の台帳実測」は不採択案であることを archive.jsonl で実測した
  (`adopted: false`、`logs/P-0183/` は存在しない)。参照すべき実体は P-0128 が納品した download_budget
  台帳** (latest.json の `download_budget` キー + 各 ns download-ledger の LEDGER_RULES)。
  P-0183 案 why が引用した実測 (retention Job が 512MiB–1GiB/回、2026-08-22) も台帳由来の数字として
  使ってよい。repo ごとのデータ総量は repo 内 docs に無いため、読み量 (= 総量 ÷ T) の見積もりは
  台帳と B2 側の事実から worker が行い、根拠を manifest コメントに書くこと。総量に対して 1 スライスが
  大きすぎる場合は T を増やし実行頻度を上げる (合計カバー期間 ≈ 3 ヶ月を維持) のが spec の精神に沿う調整方向
- **スケジュール分散の材料** (全 CronJob schedule 実測、JST 評価 — `spec.timeZone` 明示は現状ゼロ):
  日次 backup は immich 02:45 → coder-postgres 03:10 → workspace-homes 03:30 → vaultwarden 03:40 →
  syncthing 03:55。**retention は全 5 本が日曜朝 (03:45–04:50) に集中**しており、これが 2026-08-22 の
  cap 超過全滅日の形。integrity は (a) apps 間で実行日を分散し、(b) 日曜朝の retention 群と重ねない、
  (c) 1 UTC 日の予算内に収まる時刻に置く、を満たせばよい
- **LEDGER_RULES への登録を忘れない**: restic を触る新 CronJob は各 ns の download-ledger の
  LEDGER_RULES env に登録しないと `unknown_jobs` として記録される (帳簿が「設定忘れを黙って 0 扱いに
  しない」設計、download-ledger-cronjob.yaml 参照)。integrity CronJob 名 : 推定 bytes (≈ repo 総量 ÷ T)
  を 4 namespace 分追加し、推定値の根拠をコメントに書く
- **失敗経路は既存のもの**: integrity 子 Job の失敗は既存 backup と同じく ArgoCD appTree health を
  Degraded にし (substrate 2026-08-23 訂正済み)、latest.json の `applications` → briefing/incident 経路に
  乗る。新しい警報機構を作らない。スクリプトは `set -eu` で restic の非ゼロ終了を握り潰さない
- **レポート畳み込みは P-0128 の産出側/集約側分割を踏襲**: 産出側 (integrity Job 自身) が結果
  {check 成否・読んだスライス番号/パック数・累積カバー率} を専用 ConfigMap へ書く。Job 履歴は
  successfulJobsHistoryLimit で消えるため ConfigMap 側が唯一の長期記憶 (download-budget と同じ発想)。
  report.py は import 時に SA token を読むため unit test から直接ロードできない — 集計純関数は
  別モジュールに分離するのが download_budget.py で確立済みの流儀。rbac.yaml の ClusterRole configmaps get
  は resourceNames 制約付きで、「resourceNames の追加分だけ権限が増える」旨コメント済み — 新 ConfigMap 名の
  追加が必要になる。verify #4 は 'integrity' を大文字小文字無視で grep するだけだが、キー名と collect 関数が
  自然にこれを満たす
- **正本とクラスタ内実行の二重管理は最初から織り込む**: 既存流儀では実行スクリプトは ConfigMap 埋め込み
  (download_ledger.py は 4 ファイル同一コピー + `ops/check_download_ledger_script_sync.py` が CI で drift 検査)。
  `ops/tools/restic_integrity.py` を正本に置き、ConfigMap 埋め込みコピーとの同期検査を新設するか
  (check_* 流儀の拡張)、埋め込み側を薄くして選択ロジックの実体を正本側に集めるかは worker が決める

## やらないこと

- **復元試験・復旧演習の実施** (P-0080/114/115 の領域)。この spec は「読んで壊れを探す」だけで、
  復元は別論点。1 PR 1 論点
- **autopilot-core への restic backup 新設**。backup 対象そのものの拡大は別論点。「相当」の解釈は
  実在するリポジトリで確定済み (前提参照)
- **retention (--max-repack-size 等) の転送料削減・retention スケジュールの変更**。P-0183 (不採択) の
  領域であり、integrity 側は日程を「避ける」側に回るだけで既存 retention には触れない
- **B2 コンソールでの cap 実値確認・引き上げ、cap 値の決め打ち設定**。管理コンソール作業 = 人間専有。
  台帳と同じく「判明している範囲の事実」だけで判断する
- **incident/briefing 以外の新しい警報経路の新設**。DoD (2) は「既存の incident/briefing 経路に乗る」
  が要件であり、新経路は過剰
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の編集**。autopilot が直接 push する領域で
  コンフリクトする (CLAUDE.md)
