# P-0231 — 秘書に暦を覚えさせ、忘れる前に告げさせる

「忘れると困る日」の自前台帳を作り、Mission Control に常設節として載るまで通す。

## 目的

誕生日・支払日・定期行事のような「忘れると生活が困る日」を、エージェント自身の台帳 (ops/reminders.json) として持ち、48h 内の due を人間の言葉で告げられるようにする。P-0215 (不採択) は schema とレンダラで終わり人間の目に届く保証がなく、朝の brief (P-0174) も未実装のまま。今回は**確実に生きている人間向け表面 — Mission Control への常設掲載 — を成果の中心**に据える。新しい部品を増やさず既存器官 (validate / unittest / ops-state push / Next.js ダッシュボード) に最初の生活データを流し込む、「採択済みで眠っている器官を使い切る」実践。

## 受入チェックリスト

initializer 開始時点 (2026-08-23, ブランチ project/p-0231 @ 5877f715e) で **全 5 項目 failing を実測済み**。完了判定はこの通り。

- [x] `test -f ops/reminders.json && test -f ops/life/reminders.py`
      台帳本体 (main 管理) とレンダラの両ファイルが存在すること。(2026-08-23 worker 実測 GREEN)
- [x] `python3 -m unittest ops.tests.test_reminders`
      レンダラの unit テストが通ること。年 recurrence (誕生日など毎年くるもの) と境界日 (今日・明日・明後日の 48h 窓の端) を fixture で固定。(24 テスト GREEN)
- [ ] `git fetch origin ops-state -q && git show origin/ops-state:reminders.json >/dev/null 2>&1 || git show origin/ops-state:briefing/reminders.txt >/dev/null 2>&1`
      成果物が **実際に ops-state ブランチへ公開されている**こと。ローカルのファイル存在ではなく、push 済みのリモートブランチから読めることが基準 (「自己申告を信用しない」の機械版)。
      ※ 配線は完了 (heart ビートが briefing/reminders.txt を書く。beat 結合テスト付き)。**merge 後に heart が最初のビートを回した時点で green になる**。単一書き手 = heart の原則により、worker が直接 push して先に green にすることはしない
- [x] `grep -rq 'reminders' apps/ops-dashboard/app/src/`
      ダッシュボード側のコードが reminders を参照していること (= 読む経路が実在)。(src/lib/reminders.ts ほか GREEN)
- [x] `test -s ops/projects/logs/P-0231/render-sample.txt`
      レンダラの**実データによる**出力 1 通が logs に保存されていること (空文件不可)。fixture ではなく ops/reminders.json 本体からの生成。(実時刻・実台帳で生成済み)

## 設計方針

調べて分かった前提と、それから導く作り方。

- **schema と検査**: `ops/reminders.json` は `date` / `title` / `repeat` (year または none 相当) / `note` のエントリ配列。検査は `ops/validate.py` に `check_reminders()` を新設して `main()` に登録する (既存の `check_backlog()` / `check_inventory()` と同型。stdlib のみという縛りも同じ)。
- **レンダラ**: `ops/life/reminders.py` (stdlib のみ)。48h 内の due を抽出し 1〜3 行の日本語文面を作る純関数 + CLI。テストは `ops/tests/test_reminders.py` (`ops/tests/__init__.py` は既存)。`datetime.now` に依存する境界の試験は now を注入できる形にして fixture で固定する。
- **配信は heart の既存 ops-state push に乗せる** (新経路を作らない):
  - ops-state ブランチは main とは 2026-08-08 に分岐した以後別々に進んでおり (merge-base 5207f9f53)、**ops-state 側の `ops/` ツリーは古い**。現在も鮮度が保たれているのはルート直下の状態ファイル (heartbeat.json 等) だけ。
  - したがって台帳の単一情報源は main の `ops/reminders.json` とし、**heart が各ビートで sync_main 済みの checkout (repo_dir) から読んで state_dir に置く**のが正しい経路。`gitutil.commit_and_push_state()` が `add -A` で何でも載せるので、heart のビート処理に数行の追加で足りる。
  - 公開形式は verify が両方受け付ける。**推奨は描画済み断片 `briefing/reminders.txt`**: 文面生成ロジックを Python レンダラの一箇所に集約でき、ダッシュボードは `git show origin/ops-state:briefing/reminders.txt` をそのまま表示するだけになる (TS 側に due 計算を複製しない。「同じ事実が 2 箇所に書かれていない」CHARTER §1)。
- **ダッシュボード側**: `apps/ops-dashboard/app/src/lib/ops-state.ts` の `loadFromGit()` が `git show origin/ops-state:<path>` を並べて取得する既存パターンがあるので、そこに reminders を追加する。表示は `page.tsx` に「次の予定」節を**常設**で足す (データが空でも節は消さない。空のときの文面も含めて実装)。テストは同 app の流儀 `npm test` (tsx --test, fixture を tests/fixtures/ に置く) と `npm run lint` (tsc --noEmit) で検査。
- **初期データは仮置き 3 件まで**。実データを勝手に捏造せず、節の中に「暦の種を募集」の一文を添えて人間本人から育てる (P-0192 流儀: 沈黙も観測)。render-sample.txt はこの仮置きからの実出力でよい。
- 注意: `ops/heart/` 配下は人間レビュー必須パスで auto-merge 対象外 (CHARTER §5.5 の前例どおり)。

## やらないこと

- **朝の brief / Telegram への配信はしない。** P-0174 が未実装で notify.py の briefing は Phase 3 待ち — 届かない口に載せても成果にならないのが P-0215 不採択の原因。今回はダッシュボード掲載まで。
- **Radicale 等の新しいカレンダー部品・webcal フィードは持ち込まない** (棄却済み)。iCalendar 形式にもしない。
- **段階 3 (Gmail/Calendar の lethal trifecta 領域) には触れない。** 台帳は main の Git に乗るため、私的データ (実名の誕生日等の機微な個人情報) を初期データに入れない。仮置きは「支払日」「ごみ収集」のような一般語で埋める。
- **ops-state の第二の書き手を作らない** (単一書き手 = heart の原則)。ダッシュボードは読むだけ。
- **CronJob / 新常駐プロセスを増やさない。** 描画は heart ビート内で完結させる。
