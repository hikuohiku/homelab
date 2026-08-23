# P-0193 — 人間が見る画面は、壊れたことを画面が報告しない — headless chromium で Mission Control を毎日実際に描画させ、タイルの嘘 (矛盾シグナル・白画面・古さ) を器が先に見つける常設検眼

## 目的

critic (08-22) が見つけた「鼓動しています(緑) の隣に異常終了(赤)」は、ダッシュボードの嘘を人間の目が発見した実例だが、個別修正 (P-0154 案) はその 1 つの嘘にしか効かない。UI の静的解析では捕まらない「実際に描画したときだけ見える破綻」(JS エラー・空配列描画・API 失敗時の白画面) を検査する装置が存在しない。autopilot イメージには chromium が最初から入っている (substrate 実測) ので、headless 描画 + 断言の常設ジョブを新規依存ゼロで作る。P-0130 (週 1 回人間の代わりに critic-user が見る) と形が違い、**毎日機械が見る**。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0193` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f ops/tools/dashboard_smoke.py && python3 -m py_compile ops/tools/dashboard_smoke.py`
  — スモーク本体が存在し、構文として通ること。
  実測 rc=1 (`ops/tools/dashboard_smoke.py` 未存在)。
- [ ] `python3 -m unittest ops.tests.test_dashboard_smoke`
  — 断言ロジックが HTML fixture で矛盾ケース・正常ケースの両方向に固定されていること。
  実測 rc=1 (ModuleNotFoundError — テストモジュール未存在、FAILED errors=1)。
- [ ] `grep -q 'dashboard_smoke' apps/ops-health-reporter/report.py`
  — 結果フィールドが ops-health-reporter のレポートに畳み込まれていること。
  実測 rc=1 (言及なし)。
- [ ] `test -s ops/projects/logs/P-0193/smoke-result.json`
  — 初回実行の断言結果とスクリーンショットの記録が空でなく存在すること。
  実測 rc=1 (未存在)。

**verify は DoD の下限であって DoD そのものではない。** verify が直接見ないもの —
(1) 矛盾断言が critic 08-22 指摘の形状 (非 0 exit の赤表示とそれ以降の正常 beat 表示の共存等) を
実際に検出できるか、(2) 常設ジョブ化後に「失敗時のみ briefing/incident に乗り、成功は通知予算を
消費しない」が成立しているか、(3) スクリーンショットが実物として保存されているか —
は機械検査不能なので、worker が PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- 描画対象は Service `ops-dashboard` (namespace autopilot、port 80 → containerPort 3000)。
  Next.js 製。readiness/liveness probe はあるが、それは「HTTP 200 が返る」最小限しか見ない —
  今回作るのはその上の「描画後 DOM の意味的な整合」の検眼
- autopilot イメージ (`ghcr.io/hikuohiku/homelab-autopilot`) に chromium + Noto CJK 入り
  (substrate.md 実測、本環境でも /usr/bin/chromium を確認)。playwright 等は無く、
  report.py / syncthing_acceptance.py と同じ標準ライブラリ-only 流儀を守る —
  chromium は subprocess で起動し `--headless --dump-dom --screenshot --virtual-time-budget`
  で描画後 DOM と PNG を取る (Next.js はクライアント描画するため生 HTML ではなく
  レンダリング完了後で断言する)
- reporter への畳み込みは **download-budget (P-0128) と同じ契約**: 産出側 CronJob が自 namespace の
  専用 ConfigMap に `report.json` キーを書き、report.py が読んでレポートへ折る。
  注意: `apps/ops-health-reporter/rbac.yaml` の configmaps get は resourceNames 制限付きなので
  新しい ConfigMap 名の追加が必要 (spec `touches_apps: true` の本体はここと CronJob manifest)
- unittest 流儀は `ops/tests/test_*.py` + `python3 -m unittest` (CI が discover するので
  配線不要)。判定は純関数に分け、HTML fixture で「落ちること/通ること」両方向を固定するのが定石
  (P-0071/P-0105/P-0185 と同じ)
- コンテナ内 chromium の作法: sandbox なし (--no-sandbox)、user-data-dir は書き込み可能場所へ
  (readOnlyRootFilesystem なら emptyDir)。CronJob の schedule は `spec.timeZone` 未指定だと
  JST 評価 (substrate)。memory limits は実測の裏付けなしに付けない

### 作り方

1. `ops/tools/dashboard_smoke.py` — 標準ライブラリのみ。Service URL を headless chromium で描画し、
   (a) HTTP 200 とレンダリング完了、(b) 主要セクション (鼓動・プロジェクト一覧) の存在、
   (c) 明示的矛盾検査、を断言して exit code で合否を返す。矛盾検査は純関数化し、
   「赤の異常表示と緑の鼓動表示が共存」「セクションが空のまま描画成功」「generated_at が古い」
   を DOM 文字列から判定する
2. 初回実行の結果を `ops/projects/logs/P-0193/smoke-result.json` に記録して commit する
   (スクリーンショット実体は隣接ファイル。git 履歴を膨らませないため以後の更新は git 外)
3. 常設ジョブ — apps/ 配下に CronJob (autopilot ns、既存 autopilot イメージの digest pin、
   Job 同名再適用なら `Force=true,Replace=true` sync-options)。結果は専用 ConfigMap へ。
   成功は記録のみ、失敗だけが briefing/incident に乗る既存経路 (latest.json の異常フィールド →
   autopilot の briefing) に流れる
4. `apps/ops-health-reporter/report.py` + rbac.yaml — ConfigMap を読んで
   `dashboard_smoke` フィールドとして畳み込む (collect_* の 1 項目の失敗で全体を止めない思想に従う)
5. `ops/tests/test_dashboard_smoke.py` — 矛盾断言ロジックを HTML fixture で両方向固定

## やらないこと

- **ダッシュボード本体の修正・UI 書き換え** (P-0154 相当)。本プロジェクトは検眼装置であり、
  見つけた嘘を直すのは別の論点 (1 PR 1 論点)。P-0154 採択済みなら回帰防止装置として機能する
- **新規依存の追加・イメージの変更** (playwright/puppeteer 等)。why が新規依存ゼロに絞っている
- **通知経路の新設**。失敗は briefing/incident に乗る既存経路への畳み込みのみ。Discord 直送の
  新しい予算消費を作らない
- **スクリーンショット履歴の蓄積・画像 diff 等の拡張**。毎日の描画断言が先決
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の編集**。autopilot が直接 push する
  領域でコンフリクトする (CLAUDE.md)
