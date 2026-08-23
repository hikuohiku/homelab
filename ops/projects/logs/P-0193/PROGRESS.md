# P-0193 PROGRESS

## 現在の状態

受入 4 項目のうち **1・2・4 が green** (セッション 2, 2026-08-23)。残り 3 は failing。

- [x] 1. `ops/tools/dashboard_smoke.py` 存在 + py_compile — 実サイトで全 12 検査合格を実測済み
- [x] 2. `python3 -m unittest ops.tests.test_dashboard_smoke` — 33 本 OK (セッション 2)
- [ ] 3. `grep -q 'dashboard_smoke' apps/ops-health-reporter/report.py` — reporter 畳み込み未着手。**次のセッションの最優先** (下記「reporter 畳み込みへの引き継ぎ」)
- [x] 4. `smoke-result.json` 初回記録 — 実描画の断言結果 + PNG を commit 済み

## 実行ログ

### セッション 1 (2026-08-23)

やったこと:

1. `ops/tools/dashboard_smoke.py` 新設 (標準ライブラリのみ、syncthing_acceptance.py 流儀)。
   chromium は subprocess ではなく **CDP (`--remote-debugging-pipe`) で操作**する。
   `--dump-dom` / `--virtual-time-budget` は本ページでは使えない (罠セクション参照)。
2. 断言ロジックは純関数に分離済み: `evaluate_dom` / `check_rendering` /
   `check_sections` / `check_project_board` / `check_contradictions` / `check_freshness` /
   `parse_jst_stamp` / `visible_text`。合成 DOM で矛盾 5 形状 (warning 共存・チップ混在・
   HEART SIGNAL LOST 共存・観測なし+正常・古い心拍) の発火を手動確認済み。
   **unittest への固定はまだ** (項目 2)。
3. 実サイト (tailnet URL 経由) で本番実行 → 全 12 検査合格、rc=0、約 7 秒。
   `smoke-result.json` + `smoke-result.png` を commit。PNG は実物の Mission Control
   (鼓動チップ正常・ACTIVE CHANNELS 6・transcript 領域) を目視確認済み。
   プロジェクトボードは nav を JS クリックしてから取得する (DoD b の「プロジェクト一覧」)。

テスト作成の手引き (次セッションへ):

- `ops/tools/` には `__init__.py` があるので `from ops.tools import dashboard_smoke` で
  import できる。モジュール top-level は定数のみで副作用無し (report.py の SA token
  読みみたいな罠は無い)。cluster 外 import 可
- fixture で両方向固定する対象: `evaluate_dom` (正常/矛盾/白画面/古い心拍の 4 系統)、
  `parse_jst_stamp` (年越しの年推定 "12/31"→前年、閏日を含む %m/%d 解析は年を結合してから
  strptime する実装にしてある — Python 3.14 の DeprecationWarning 対策)、
  `visible_text` (script 内の Next.js flight data を除外できること)、
  `find_heart_chips` (`class="heart-chip "` に**末尾空白がある**実測形)、
  `check_freshness` の境界 (max_age_s ちょうど/超過)
- 実 DOM の較正済みサンプルは smoke-result.json と、このファイルの下の実測メモ参照

### セッション 2 (2026-08-23)

やったこと:

1. `ops/tests/test_dashboard_smoke.py` 新設 (33 本、全て標準ライブラリの unittest)。
   手引きどおり `from ops.tools import dashboard_smoke` で import (副作用無しを確認済み)。
   固定した契約:
   - 正常ページで `evaluate_dom` の検査名リスト 9 個を**並びごと**固定
     (reporter 畳み込みがこの名前に依存するため)。全 pass も断言
   - 矛盾 5 形状を両方向で: warning 共存 / HEART SIGNAL LOST 共存 /
     チップ混在 (ok+bad) / 観測なし+正常チップ / 古い心拍。それぞれ対応する
     check 名が FAIL になり ok が倒れること。加えて「bad チップのみなら
     no-mixed-heart-signals は鳴らさない」「チップ無しは共存検査対象外」という
     役割分担、「鮮度の失敗が矛盾検査に漏れない」独立性も固定
   - 白画面: rendering 4 検査が鳴るが render-complete は鳴らさない (loading マーク
     自体が無いため。役割分担の固定)
   - スピナ残置: render-complete のみ鳴る
   - `parse_jst_stamp`: 基本解析・年越し前年巻き戻し・5 分 skew を巻き戻さない・
     閏日 "02/29" を **warnings.simplefilter("error") 付きで**解析 (非推奨パスに
     触れたらテストが落ちる構造)・解釈不能ラベルは None
   - `visible_text`: flight data (`__next_f.push`) の中身が可視テキストにも
     矛盾検査にも乗らないこと。style 除外・charref 解決・壊れた HTML で例外を出さない
   - `find_heart_chips`: 実測形 `class="heart-chip "` (末尾空白) と
     `heart-chip--bad` の両方、改行跨ぎ (re.S)、無関係 div の無視
   - `check_freshness` 境界: max_age_s ちょうどは沈黙 (> でのみ鳴る)、+1 秒で鳴る。
     LAST HEART 欠落・解釈不能は fail
2. dashboard_smoke.py の docstring 内「別 PR で足す」を実態に合わせて更新。

分かったこと:

- fixture で最初、正常系が masthead 検査で落ちた。原因は合成 DOM に
  `MISSION CONTROL` 文言 (identity ブロック) を入れ忘れという単純ミス。
  実 DOM 断面は smoke-result.json と page.tsx (L354-399) を突き合わせて作れば足りる
- テスト実行は約 0.02 秒。chromium 不要なので CI/cluster 外どこでも回る

reporter 畳み込み (項目 3) への引き継ぎ:

- 契約は P-0128 (download-budget) と同じ「産出側が専用 ConfigMap に report.json キーを書く」。
  result dict の schema は `ops/tools/dashboard_smoke.py` の `run_smoke` 戻り値
  (schema:1, ok, checks[{name,status,detail}], failed_checks, screenshot{bytes,sha256})
- CronJob 側は `--out` で書いた JSON をそのまま kubectl create configmap する想定。
  `apps/ops-health-reporter/rbac.yaml` の configmaps get は resourceNames 制限付きなので
  新 ConfigMap 名の追加が必須 (PROJECT.md の注意どおり)

### 実測済みの罠 (次のセッションのあなたは再測しなくてよい)

1. **`--virtual-time-budget` はこのページで死ぬ**: transcript の EventSource (SSE) と
   10 秒ごとの /api/snapshot ポーリングが仮想時間を滞留させ、VTB=3000 は即空出力、
   VTB=10000 は 60 秒でも終わらない。`--dump-dom` は load 時点 (hydration 前) で
   ダンプされるし `--timeout=N` は固定待ちではなく上限値なので待ってくれない。
   chromium 151 (Alpine)。**CDP パイプ方式へ替えた理由。戻さないこと**
2. **CDP パイプの fd 向き**: 子プロセスが**コマンドを fd3 から読み、応答を fd4 へ書く**
   (実測確定)。`pass_fds` に 3 と 4 自身を含めないと close_fds に食われて
   "Remote debugging pipe file descriptors are not open" で起動死する
3. attach 対象は `type == "page"` で選ぶ (内蔵拡張の background_page が混ざる)
4. `heart-chip` の React 出力は `class="heart-chip "` (末尾に空白)。`--bad` 時は
   `class="heart-chip heart-chip--bad"`。正規表現は `[^"]*heart-chip[^"]*` で受ける
5. `LAST HEART` の表示は `MM/DD HH:MM:SS` (JST, 年なし)。年は現在年を仮置きし
   2 日以上の未来なら前年に直す (年末年始対応、`parse_jst_stamp` 実装済み)
6. `visible_text` は script/style をスキップする (outerHTML には Next.js flight data
   の `__next_f.push` が混ざり、除外しないと non-blank 検査が常に pass になる)
7. Python 3.14 で `%m/%d` 単独の strptime は DeprecationWarning (閏日曖昧性)。
   年を結合してから `%Y/%m/%d` で解釈する実装にしてある

### 発見 (仕様外。curriculum が拾うこと)

- 実行環境の `/tmp/opencode` は root 所有で worker から書けない。mktemp を使う既定ルールで問題無し
- `ops/state.json` の `dashboard.ops_dashboard_url` (tailnet URL) はクラスタ外の
  worker からも到達可能だった (HTTP 200 実測)。T-0130 の「tailnet に居る人間による
  確認が望ましい」注記は実質解消済みと考えられるが、確定させるなら人間確認を
- 初回記録は tailnet URL 経由 (クラスタ外) の実行。常設ジョブは Service DNS
  (`http://ops-dashboard.autopilot.svc`) 既定で動くはずだが、**in-cluster での
  実行実績はまだ無い**。CronJob セッションで初回実行時に確認すること
- スクリーンショットは 765x836 / 約 66KB。git には初回 1 枚のみ commit 済み
  (以後の更新は PROJECT.md の方針どおり git 外)

## 次のセッションへの一言

残りは項目 3 (reporter 畳み込み + rbac + CronJob) のみ。上の「reporter 畳み込み
(項目 3) への引き継ぎ」と PROJECT.md を読んでから着手すること。verify 4 項目中
1・2・4 は既に green なので、report.py への畳み込み (grep 対象) だけで全 green になる。
CDP 方式の罠は上に全部書いた。in-cluster 初回実行の実績だけまだ無い。
