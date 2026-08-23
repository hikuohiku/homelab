# P-0193 PROGRESS

## 現在の状態

受入 4 項目 **すべて green** (セッション 5, 2026-08-23 再実測)。レビュー指摘 2 件
(heart 警報配線の未実装 / reporter の rc=2 代役レコード取り扱い) はセッション 4 で解消済み。
装置が HEAD コードで動くことは結合予行演習で再実証済み (セッション 5)。
DoD 本体として残っているのは **in-cluster 初回実行の実績** のみ (merge 待ち)。

- [x] 1. `ops/tools/dashboard_smoke.py` 存在 + py_compile — 実サイトで全 12 検査合格を実測済み
- [x] 2. `python3 -m unittest ops.tests.test_dashboard_smoke` — 33 本 OK (セッション 2)
- [x] 3. `grep -q 'dashboard_smoke' apps/ops-health-reporter/report.py` — 畳み込み完了 (セッション 3)
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

### セッション 3 (2026-08-23)

やったこと:

1. **常設ジョブ** `apps/ops-dashboard/dashboard-smoke-cronjob.yaml` 新設
   (SA + Role/RoleBinding + ランナー ConfigMap + CronJob、apps/ops-dashboard の
   kustomization へ配線。namespace は autopilot — 観測対象の Service
   `ops-dashboard.autopilot.svc` と同じ)。
   - CronJob `dashboard-smoke`、schedule `40 9 * * *` (毎日 09:40 JST 評価。
     :00/:05/:25/:30 の既存 collector と分単位でずらした)。`backoffLimit: 0`
     (記録はプロセス内で完了するため retry の利益が無く、prod への chromium
     再起動負荷だけが倍る)、activeDeadlineSeconds 300 (正常時実測 約 7 秒)
   - イメージは autopilot と同じ digest pin (`93a898cf…`)。chromium はこの
     イメージにしか無い。**check_version_sync.py の「autopilot image digest」
     グループに第 4 箇所として登録済み** — digest 更新 PR でここが抜けると CI が落ちる
   - ランナー (YAML 埋め込みスクリプト) が本体を subprocess で起動し、結果 JSON を
     専用 ConfigMap `dashboard-smoke` の report.json キーへ書き戻す。exit code は
     本体のそれを Job へそのまま伝える: 0=合格(記録のみ) / 1=不合格(記録してから
     落ちる → Failed Pod は pod_issues 収集という既存経路にも乗る) / 2=装置故障
     (本体が JSON を書けないため ok=False・failed_checks=[]・tool_error 付きの
     代役レコードを書いてから落ちる。「ページの嘘」と「装置の壊れ」を区別できる形)
2. **reporter 畳み込み**: `collect_dashboard_smoke()` + `_dashboard_smoke_summary()`
   を report.py に追加し、`latest.json` / history jsonl の `dashboard_smoke` キーへ。
   status は ok / fail (failed_checks に名前+detail 200 字切詰め) / stale
   (26h = 日次 1 回分 + 2h マージンより古い。鮮度判定を fail より優先) /
   no_data (未稼働・破損)。生 checks と screenshot フィールドは載せない
   (history jsonl 膨張止め)。notes にも説明を追記
3. **rbac.yaml**: reporter 側 configmaps get の resourceNames に `dashboard-smoke` 追加
4. スモーク本体のコピー `apps/ops-dashboard/dashboard_smoke.py` を新設
   (kustomize の load 制限でディレクトリ外参照ができないため)。
   `ops/check_dashboard_smoke_script_sync.py` 新設 + ci.yml の consistency checks
   へ配線。canonical との drift を CI が検出する
5. unittest 2 本新設: `test_report_dashboard_smoke.py` (18 本。AST 抽出方式で
   collect_dashboard_smoke/_summary を固定: ok/fail/stale 境界両方向・skew 負値・
   破損系は全て no_data・API パス断言) と `test_dashboard_smoke_runner.py`
   (8 本。YAML 埋め込みランナーから extract_block_scalar で抽出し
   load_result/fallback_result を固定)。合計 ops/tests 348 本 + heart/tests 196 本 green

分かったこと / 設計判断:

- **産出先 ConfigMap は manifest に事前作成しない** (P-0128 の download-budget と
  ここが違う。理由: ArgoCD 全 Application が selfHeal: true であり、git 上の
  `data: {}` とジョブが書いたキーの drift が selfHeal でどう扱われるかを
  リポジトリ内の証拠だけでは確定できなかった。pvc-usage-reporter は最初から
  「manifest 宣言なし・ジョブが create」方式で稼働実績があり、ArgoCD 管理外に
  しておけば競合が構造的に起こらない。消されたら翌日の実行で再作成され、
  それまで reporter が no_data/stale で正直に見せる)
- Role は update を resourceNames `["dashboard-smoke"]` に絞った。create は名前を
  RBAC で絞れないので無制限 (pvc-usage-reporter 同様) だが、autopilot ns には
  heart のプロンプト ConfigMap (autopilot-config) があるため update の無差別許可は
  プロンプト注入経路になりうる — そこだけは硬く絞った
- 引き継ぎメモにあった「kubectl create configmap する想定」は純 API (GET
  resourceVersion → PUT / 無ければ POST) に変えた。get/update しか持たない SA だと
  kubectl apply (PATCH) も replace (--force は delete+create) も通らず、権限設計が
  緩むため。pvc_usage.py の put_configmap をそのまま流用できる形になった
- スクリーンショットは Pod ローカル emptyDir に出すだけでクラスタ外へ運ばない。
  JSON には bytes/sha256 のみ残り、畳み込み後の latest.json には載らない
  (画像履歴蓄積は「やらないこと」)

verify 自己実測: 4 項目すべて green。追加で kustomize build (ops-dashboard /
ops-health-reporter とも OK。helm を要する 5 app はローカルに helm 無しのため
スキップ — CI 側で実施される) / check_version_sync 含む consistency checks 7 本 /
yaml parse 全件 / ops/tests + heart/tests 計 544 テスト green。

残っていること (DoD 下限は超えたが仕様の本体として):

- **heart 側への警報配線は未実装。** DoD(3) の「失敗時のみ briefing/incident に乗る」
  を完全に成立させるには、`facts.budget_alert()` 相当の抽出
  (例: `facts.dashboard_smoke_alert(doc)` — status が fail/stale のときだけ中身を返し、
  no_data は budget の unconfigured/no_data と同じく沈黙させるのが妥当と考える) と
  heart.py への budget 流儀の追記 (cursors で同一日内再通知を落とす) が必要。
  P-0128 が「budget.status を latest.json に載せる (セッション 1) → facts.budget_alert
  で配線 (後続セッション)」の 2 段階でやったのと同じ順序。ops/heart/ は heart の
  領分なので、次セッションで spec の範囲として着手するか curriculum へ出すかから決めること
- **in-cluster 初回実行の実績はまだ無い** (merge 後に初回 CronJob 実行 →
  `kubectl get configmap dashboard-smoke -n autopilot` と次回 reporter run の
  latest.json `dashboard_smoke` キーで確認すること。Service DNS 到達性・chromium の
  非特権描画・TMPDIR=/tmp の user-data-dir はすべて初回実行で初めて実測になる)

### セッション 4 (2026-08-23)

レビュー指摘 2 件の解消が本セッションの全内容。

やったこと:

1. **heart 側への警報配線 (指摘 1 = DoD(3) の本体)**:
   - `ops/heart/facts.py` に `dashboard_smoke_alert(doc)` 新設。
     latest.json の `dashboard_smoke.status` が **fail / stale** のときだけ
     `{status, reason, failed_checks}` を返す。ok / no_data / 壊れ / キー無しは None。
     no_data を沈黙させるのは budget の unconfigured/no_data と同じ判断
     (鳴らせる状態になってから既存経路に乗る)。rc=2 代役レコード
     (tool_error 付き fail) も乗せる — 装置故障も人間に見せるべきで、区別は reason が担う
   - `ops/heart/heart.py` の beat に budget 流儀 (P-0128) どおりで配線:
     cursors キー `dashboard_smoke_alert` で同一 status・同一日内の再通知を落とし
     (budget_alert_due() を流用 — 中身は status/date の一般判定)、
     briefing-queue.jsonl へ `{"source": "dashboard-smoke (<status>)", "body": <reason>}` を追記、
     `notifier.send("incident", ...)` を送る。metrics.jsonl に `dashboard_smoke_status` 追加
   - **DoD(3)「失敗時のみ briefing/incident に乗り、成功は通知予算を消費しない」の成立証跡**:
     `ops/heart/tests/test_dashboard_smoke_alert_beat.py` (4 本) が実物の Heart.beat() を
     shadow で連続実行し実ファイルで固定 — fail の cursor 永続化・同日内再通知抑制・
     fail→stale の同日再発火・**ok/no_data/観測失敗では cursor も queue も触らないこと**
     (成功日は記録のみ) を検査。単体は `test_dashboard_smoke_alert.py` (6 本)
2. **reporter の rc=2 代役レコード取り扱い (指摘 2)**:
   `_dashboard_smoke_summary()` が fallback レコードの tool_error/tool_error_rc を
   読み捨て、「描画断言が不合格: 」(内訳空) の嘘文面になっていたのを修正。
   tool_error 由来への reason 分岐 + 切り詰め (200 字) 載せ。stale 最優先は不変。
   内訳が空の通常 fail にも「失敗検査の内訳が記録されていない」を明示。
   notes 文面も更新。テスト 6 本追加 (`SummaryToolErrorTest`)

分かったこと / 設計判断:

- budget_alert_due() を名前そのままで流用した (smoke 専用の due 関数は作らない)。
  中身は `(alert, prev, today)` の status/date 比較だけで budget 固有の何者も無く、
  抑制の両方向固定は test_budget_alert*.py がすでに持っている。heart.py 側に
  「名前は budget だが汎用」コメントを残してあるので、後続セッションが
  smoke 用コピーを作ろうとしたらまずこのコメントを読むこと
- reason は reporter が必ず文字列を書く契約だが、壊れていたら str() で捏造せず None。
  警報自体 (status ベース) は倒さない
- テスト合計: ops/tests 354 本 + heart/tests 206 本 = 560 本 green (前回比 +16)。
  verify 4/4 再実測 green

verify 自己実測: 4 項目すべて green。

残っていること:

- **in-cluster 初回実行の実績のみ** (merge 後に初回 CronJob 実行 →
  `kubectl get configmap dashboard-smoke -n autopilot` と次回 reporter run の
  latest.json `dashboard_smoke` キーで確認。手順は「セッション 3」参照)。
  heart 側配線は merge 後の最初の fail/stale ビートから自動で効く
  (初回ビートで cursor 未初期化 → 即鳴る。過去分の遡及通知ではないので問題無し)

### セッション 5 (2026-08-23)

レビュー指摘なし・受入 4 項目 green で起動。failing の受入項目が無く、in-cluster
初回実行は merge 待ちのため、**残る DoD 本体「初回実行」の成功率を上げる事前検証**
をした。装置側の変更は無し (スコープ維持)。

やったこと:

1. **HEAD コードでの結合予行演習を再実測**: tailnet URL
   (`https://ops-dashboard.tailae6c2.ts.net/`) に対し本体を実行 → rc=0、全 12 検査
   合格、result.json (schema:1, ok=true, url=http://ops-dashboard.autopilot.svc) +
   PNG 産出。LAST HEART '08/23 23:34:57' (29 秒前) でダッシュボード自体も健在。
   本体の最終変更は 24c914be6 (判定層 unittest 追加) で、初回実測 f15534d2c の
   コードから触られているため、これで「**装置は HEAD で動く**」が確定。
   mktemp ディレクトリで実施し後始末済み
2. **CronJob 初回実行の静的精査** (初回で失敗しそうな点の洗い出し):
   - DEFAULT_URL = `http://ops-dashboard.autopilot.svc` を確認 — ランナーが
     --url を渡さない設計と整合
   - kustomization.yaml は `generatorOptions.disableNameSuffixHash: true` 済みで、
     configMapGenerator 生成の `dashboard-smoke-tool` はハッシュ無し名でマウント
     される (ハッシュ付きなら Pod 起動死していた箇所。問題無しを確認)
   - **emptyDir /tmp への非 root 書き込み**: 本 CronJob に fsGroup は無いが、
     既存実績 (ops-health-reporter / version-watcher / download-ledger 各 app の
     CronJob が fsGroup 無し・UID 65534・emptyDir /tmp で稼働中) から other-write
     が開いており UID 10001 でも書ける。autopilot/deployment.yaml の
     fsGroup: 10001 は PVC (root:root 所有になりうる) 用で、kubelet 作成の
     emptyDir とは事情が違う — 追加不要と判断
   - **chromium の HOME 書き込み**: イメージが `USER 10001` + `HOME=/home/autopilot`
     を用意 (Dockerfile L79-84、npm/git 用)。fontconfig キャッシュ等も書ける
   - RBAC + put_configmap: pvc_usage.py 流儀 (GET→resourceVersion 付き PUT /
     無ければ POST)。update/get は resourceNames ["dashboard-smoke"] 絞り済み。
     ロジックは test_dashboard_smoke_runner.py 8 本で固定
3. テスト全件再実測: ops/tests 354 + heart/tests 206 = 560 本 green
   (`unittest discover -s ops` 全体だと 596 本 green — 差分 36 は ops 配下の
   他ディレクトリの test_*。両方 OK)

分かったこと / 設計判断:

- in-cluster 初回実行の**真の未知数は 3 つに絞れた**:
  (a) `seccompProfile: RuntimeDefault` 下での chromium 起動 (worker 環境は
      seccomp 制約なし相当。--no-sandbox 付きなので通常は動くが、初回まで確証なし)
  (b) ConfigMap 生成物 `/scripts/dashboard_smoke.py` としての実行 — 内容は drift
      検査 CI で canonical と同一保証だが、マウント経由での実行は初回
  (c) ランナーの k8s API 書き込み (PUT/POST) — pvc-usage-reporter 流儀だが、
      この SA での実績は初回
  いずれも merge 前に代替実測できない。初回実行時は rc=2 なら fallback レコードの
  tool_error (stderr 末尾 400 字) に原因が出る
- 判断: 発見節「tailnet URL 実質解消」は **curriculum 送り**とする。URL 到達性の
  機械的実測 (HTTP 200) は 2 セッション連続で確認したが、「tailnet に居る人間が
  見えること」そのものは人間しか確定できず、本装置は代替にならないため

verify 自己実測: 4 項目すべて green。

残っていること (変更なし):

- **in-cluster 初回実行の実績づけのみ** (merge 後)。観察ポイント:
  (1) `kubectl get jobs -n autopilot` の dashboard-smoke-* 成功、
  (2) `kubectl get configmap dashboard-smoke -n autopilot` の report.json、
  (3) 次回 reporter run の latest.json `dashboard_smoke` キー、
  (4) 落ちた場合は上の未知数 (a)(b)(c) のどれかを rc=2 の tool_error で切り分ける。
  heart 側警報は merge 後最初の fail/stale ビートから効く (追加作業無し)

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
- この worker 環境に pip が無く ruff (CI の F821 検査) だけローカル実行できなかった。
  新規 Python は既存流儀の標準ライブラリのみで、py_compile + unittest で代用検査した
  (CI で F821 が鳴ったら ops/ 配下の新規ファイルを疑うこと)

## 次のセッションへの一言

レビュー指摘なし・受入 4/4 green・テスト 560 本 green。装置側の追加作業は無し
(セッション 5 の結合予行演習で HEAD コードでの動作は再実証済み)。
**残るは in-cluster 初回実行の実績づけ 1 つだけ**: merge 後の CronJob 初回実行を確認する
(手順と観察ポイントは「セッション 5」末尾。落ちたら rc=2 の tool_error を
未知数 (a) seccomp / (b) ConfigMap マウント実行 / (c) k8s API 書き込みのどれかに切り分ける)。
Service DNS 到達性・emptyDir 非 root 書き込み・HOME 書き込みは静的精査で担保済み。
発見節「tailnet URL 実質解消」は curriculum 送りと判断済み。
