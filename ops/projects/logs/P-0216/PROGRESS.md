# P-0216 PROGRESS

## 2026-08-23 initializer セッション

PROJECT.md を作成し commit した (受入 3 項目とも failing を実測)。実装は未着手。
以後の worker セッションはこの下に追記すること。

## 2026-08-23 worker セッション 1

実装一式を入れ、受入 3 項目すべて green を自分で実測した
(`budget.py --check` rc=0 / ci.yml grep rc=0 / docs の `download cap` 記載 rc=0)。

### やったこと

- `ops/b2/budget.py` 新設。apps/**/*.yaml を静的走査 (b2: リテラル or *restic*-credentials
  参照) して B2 消費者 CronJob 10 本を抽出し台帳表示。--check は
  (a) 最悪日の合計 vs cap×安全係数 0.8 — cap 実値は repo 外なので未指定なら unconfigured 沈黙
  (--cap-bytes / B2_DAILY_CAP_BYTES で有効化)、(b) 重い消費者 (≥256 MiB/回) の同一 UTC 曜日
  60 分以内の密集とリセット境界 ±2h 内の開始、(c) 台帳未登録の混入 + 台帳 stale エントリ、
  を実名付きで検査。rc=1 違反 / rc=2 解析不能 (fail-closed)
- schedule 分散: retention 5 本を「1 曜日 1 本」へ。全員 04:00 JST (= 前日 19:00Z)。
  immich 月 / vaultwarden 火 / coder 水 / workspace-home 木 / syncthing 金。
  日次 backup 帯は単一障害点なので触っていない。旧土曜夜の最悪日合計 2,720 MiB → 672 MiB
- `.github/workflows/ci.yml` の consistency checks に `python3 ops/b2/budget.py --check` を追加
- `ops/tests/test_b2_budget.py` 新設 (23 本)。cron 展開・JST→UTC 換算・各検査の両方向・
  実リポジトリ green を固定
- `docs/backup.md` に「B2 download cap の予算とスケジュール分散」節を新設し、推定値の根拠
  (LEDGER_RULES 同値 + 復元試験実測) と新旧スケジュール表を書いた。旧日程を書いた既存節には
  「→ P-0216 で変更」の注記のみ

### 分かったこと / 罠

- **JST cron の UTC 換算で曜日がずれる**: 「月曜 04:00 JST」は UTC では「日曜 19:00Z」。
  台帳表示もテスト期待値も UTC 換算後の曜日番号で書くこと (最初 [1..5] と書いて落ちた。
  正しくは [0..4])
- **閾値の単位ミスを実データで捕捉**: BOUNDARY_MARGIN_MINUTES*60 (分を時扱い) と書いてしまい、
  境界検査が全消費者に誤爆した。テストより先に本物の manifest で 1 回走らせるとこういうのに
  気づける
- evaluate() が印字すると unittest 出力も汚れるので、表示は main 側 (render_*) に分離した。
  純関数は stdout に何も出さないこと
- spec.timeZone を誰も書いていない (= kube-controller-manager TZ=Asia/Tokyo 評価) という
  現状の流儀自体が budget.py の暗黙前提。誰かが timeZone を書き始めたら rc=2 で落ちて気づく
  設計にしたが、そのときは換算部の更新が必要

### 次のセッションへ

- レビュー指摘があれば最優先で解消。現状、受入 3 項目 + ops suite 全体 (405 本) +
  budget 単体 23 本すべて green 実測済み
- **ruff F821 だけローカル未実行** (この環境に pip が無い)。CI 初回走行で引っかかったら
  未定義名の見直し
- cap の実値を人間が確認した場合、B2_DAILY_CAP_BYTES を CI に渡すかどうかは次の論点として
  curriculum 向け (今回のスコープ外)

