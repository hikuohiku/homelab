# P-0211 — 観測者の観測者を作る — health reporter の静かな死を外部 watchdog が数える

## 目的

heart の沈黙は外部 watchdog (P-0027, `.github/workflows/watchdog.yml`) が見張るが、
**ops-health-reporter 自身が死んでも誰も鳴らさない**。reporter が止まると ops-health-report
ブランチの更新が途絶え、autopilot と人間の共有する健全性情報が古いまま凍りつつ見た目は平然と
更新され続けるため、「既知事象だから」と誤読される土壌になる (substrate 2026-08-23 訂正の遠因)。
既に走っている watchdog に鮮度検査 1 ステップ足すだけで閉じる穴。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing** (2026-08-23、`project/p-0211` の
checkout でリポジトリルートから実行)。

- [ ] `python3 -m unittest ops.tests.test_health_freshness`
  — 判定ロジックの unittest が存在して通ること。現在は `ops/tests/test_health_freshness.py`
    が無く FAILED (errors=1) 実測。CI (`ci.yml` ops job) は
    `python3 -m unittest discover -s ops/tests -t .` なので、ファイルを置けば自動で拾われる。
- [ ] `python3 ops/check_health_freshness.py ops/tests/fixtures/health/stale-latest.json --max-age-hours 3; test $? -eq 3`
  — 古い `generated_at` を持つ fixture を食わせると **rc=3 ちょうど**。CLI の形は verify が規定:
    JSON パスは位置引数、閾値は `--max-age-hours` (時間単位)。現在はスクリプト不在で rc=2、
    `test $? -eq 3` が落ちる実測。fixture も自作する (`ops/tests/fixtures/` に既存流儀あり)。
- [ ] `grep -q check_health_freshness .github/workflows/watchdog.yml`
  — watchdog.yml への配線が在ること。現在 rc=1 実測。
    **`.github/` は CODEOWNERS 人間レビュー必須パスのため、この PR の merge は人間待ちになる**
    (予告済み。auto-merge を通そうとする工夫はしない)。

## 設計方針

### 前提 (調べて分かったこと)

- 書き手は `apps/ops-health-reporter/report.py`: CronJob 30 分毎 (`cronjob.yaml` schedule `*/30`)、
  `generated_at` を `%Y-%m-%dT%H:%M:%SZ` で `ops/health/latest.json` に書き、ops-health-report
  ブランチへ push (実測: `git show origin/ops-health-report:ops/health/latest.json` で
  `"generated_at": "2026-08-23T15:30:05Z"` を確認)。latest.json は最新 1 点のみの上書き
  (substrate)。
- 雛形は同ファイル群の heartbeat 組: `ops/check_heartbeat_fresh.py` + watchdog.yml heartbeat job。
  流儀に合わせる — stdlib のみ・import 時副作用なし・判定は純粋関数に I/O は `main()` へ・
  **fail-closed** (ファイル無し/壊れ JSON/generated_at 読めない → stale 扱い) ・
  `--now` 引数でテスト可能に・workflow 側は `git show FETCH_HEAD:` で取得し fetch 失敗でも
  止めず fail-closed に渡す。
- workflow の通知先は issue #56 コメント + Discord incident。heartbeat job と同じ
  `${{ github.token }}` (`issues: write` 済み) + repo secret の Discord webhook で足りる。
  器内の `ops/post_issue_comment.py` / `notify.py` は使わない (watchdog は器の外)。
- 閾値の既定値は「CronJob 30 分毎」に対して余裕を持った値をスクリプト側モジュール定数にする
  (rules.json には health reporter 用の鍵は無く、追加は CODEOWNERS 保護パス触りの別論点)。

### 作るもの

1. `ops/check_health_freshness.py` — 新設。位置引数に latest.json パス、`--max-age-hours N`
   (既定は定数)、stale で rc=3 / fresh で rc=0 / 引数誤りで rc=2。docstring に存在理由と rc 表。
2. `ops/tests/test_health_freshness.py` — 純粋関数の unittest。fresh/stale 境界・fail-closed
   (欠落/破損/未来時刻)・`--max-age-hours` の解釈を固定。
3. `ops/tests/fixtures/health/stale-latest.json` — verify 2 用の fixture (古い generated_at)。
4. `.github/workflows/watchdog.yml` — 既存 heartbeat job に**ステップ追加のみ**
   (job の増築ではなく)。ops-health-report ブランチから latest.json を fetch → 同チェック →
   rc=3 なら issue #56 コメント + Discord incident + job fail。

## やらないこと

- **`apps/ops-health-reporter/` 本体の変更。** report.py / cronjob.yaml には触れない。
  watchdog は外から既にある産出物を読むだけ (VISION「器を太らせる前に、器を使い切る」)。
- **既存 heartbeat freshness ステップの改変。** 追加のみで、P-0027 の動作を変えない。
- **`ops/rules.json` への鍵追加。** CODEOWNERS 保護。閾値はスクリプトのモジュール定数に置く。
- **health 内容 (ArgoCD Degraded の中身等) の判定。** 今回は「ブランチが新鮮か」だけ。
  内容の解釈は reporter と既存観測経路の仕事。別プロジェクトに分離 (1 PR 1 論点)。
- **Discord 通知経路の器側変更。** `ops/heart/notify.py` には触れない。workflow から直接叩く。
- **auto-merge を通す工夫。** `.github/` は人間レビュー必須でそれが正しい (CODEOWNERS の
  設計決定 #7)。抜け道を探さない。
