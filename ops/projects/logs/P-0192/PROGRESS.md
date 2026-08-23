# P-0192 — PROGRESS

worker セッションごとに追記する。書式は自由だが、証跡 (コマンドと実測値) を残すこと。

## 初期状態 (initializer, 2026-08-23)

- PROJECT.md / PROGRESS.md を作成して commit。実装は未着手
  (`ask-evidence.json`・seeds.md 新節・`ops.tests.test_wish_seeds` のすべて未存在)
- verify 4 項目とも failing を実測済み (詳細は PROJECT.md 受入チェックリスト)
- worker の最初の確認事項: morning brief P-0174 の main merge 有無
  (initializer 時点では `origin/project/p-0174` のみで未 merge を実測済み)

## セッション 1 (worker, 2026-08-23)

### やったこと: verify 4 を green にした (`ops.tools.wish_seeds` 新設)

- `ops/tools/wish_seeds.py` + `ops/tests/test_wish_seeds.py` (16 tests) を新設。
  `python3 -m unittest ops.tests.test_wish_seeds` → **OK, 16 tests 自己実測**。
  全体退行も確認: ops/tests 305・ops/heart/tests 196 とも OK
- 送信側は P-0174 の `send_telegram` と同一形を写した (P-0174 未 merge のため)。
  差分は Telegram 応答 payload を返す点 (証跡の message_id を取るため)
- **二重送信の歯止め** (verify が直接見ない項目): `main()` は送信前に
  ask-evidence.json の存在を見て、あれば送らず rc=1。テストで固定済み
- 受信側 dry-run (DoD (2)): 実物サンプルと同じ欄構成の telegram note
  (kind 無し、`origin/ops-feedback` の `20260823-120317-1e88e232.json` を読むだけ
  で参照) を fixture 化し、`collect_feedback` が review_needed (通常 feedback) に
  落とすことを実証。**kind 対応付けの追加は不要だった** → telegram-adapter /
  facts.py には一切手を入れていない
- seed 昇格は PROJECT.md 作り方 4 の 3 系列 (返信あり / 沈黙 / veto 語混じり通常文)
  をテストで固定

### 分かったこと / 発見 (スコープ外なのでここに書くだけ)

- **triage 誤爆の構造リスク**: 自由文返事に「やめて」「止めて」が入ると heart が
  stop_all/veto として拾う (50 字以下だと部分一致で拾う)。募集への短い返事
  「全部やめて」が全停止を引き起こしうる。本モジュールは triage 通し後に
  review_needed だけを seed 昇格し、停止系に落ちた返信は「要確認」別掲にして
  被害の二重化を防いだが、**heart 側での誤停止自体は防げない** (curriculum が
  拾うべき論点として記録)
- worker サンドボックスには TELEGRAM_BOT_TOKEN も kubeconfig も無い
  (env 実測・`kubectl` は localhost:8080 で拒否)。このセッションからは送信不可能

### 次のセッションへ

1. **DoD (1) の送信はまだ未実施** (verify 1・2 は red のまま)。credential がある環境
   (autopilot ns Secret `telegram-adapter-credentials` = Doppler homelab/prd 由来) で、
   本ブランチ checkout のリポジトリルートから:
   `TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOWED_USER_ID=... python3 -m ops.tools.wish_seeds --send`
   → Telegram 応答から evidence ファイルまで自動で書く (verify 1・2 が green になる)
2. 返信待ち。ops-feedback inbox は読み取り専用で。返信が着いたら
   `render_seeds_section(notes)` の出力を seeds.md の H6 近くに貼り verify 3 へ。
   数日経って返信ゼロなら notes=[] で沈黙記録を生成して verify 3 を満たす
   (沈黙も観測。seeds.md への反映時に「締め時点」の日付を正直に書き換えること)
3. 送信本文は spec 固定文言のみ (`compose_ask()`)。装飾を足さないこと


## セッション 2 (worker, 2026-08-23)

### やったこと: 送信を実行する器 (GitOps one-shot Job) を新設 — apps/wish-seeds/

- **このサンドボックスからは送れないことを再実測** (env に TELEGRAM_* 無し /
  SA token 未 mount / API server 10.43.0.1:443 に資格無し)。CHARTER §3 の
  「manifest は先に書く」に従い、credential があるクラスタ側で実行される
  1 回限りの Job を GitOps 経路で用意した (PROJECT.md 作り方 1・2 の実装)
- `apps/wish-seeds/` 新設 (Application + Job + run_ask.py + wish_seeds.py 同期コピー)。
  root apps/kustomization.yaml に登録。Job は Force=true,Replace=true 付き・
  autopilot ns・Secret telegram-adapter-credentials 参照 (地図登録不要の既存宣言のみ)
- `run_ask.py` (Job 本体): **追加送信なしの 3 層歯止め** —
  (1) 証跡が main / project ブランチのどこかにある → skip (rc=0)、
  (2) pending マーカーだけがある → 送らず abort (rc=1 で騒ぐ。証跡無しの黙認は隠蔽)、
  (3) 書き込み先ブランチが無い → abort。送信直前に pending を Contents API で
  書くので「送信後に死んだ」再実行でも二重送信しない (fail-safe 側に倒した)。
  証跡は Telegram 応答の message_id を持って project ブランチへ書き戻される
- `ops/tools/wish_seeds.py` の triage import を render_seeds_section() 内へ遅延化。
  理由: Job コンテナは repo checkout 無しで run_ask.py → wish_seeds.py (ConfigMap コピー)
  を import するため、module level の ops.* import があるとそこで死ぬ。
  挙動変更なし (16 tests 全 green 再実測)。ConfigMap コピーとの同一性はテストが機械検査
- unittest 22 本新設 (`ops.tests.test_wish_seeds_job`)。**OK 自己実測**。
  全体退行も OK 実測: ops/tests (327) / ops/heart/tests (196) / ops/runner/tests
- CI 相当を自力実測: validate.py 0 error / check_*.py 7 種全部 ok /
  `kubectl kustomize apps/wish-seeds` rc=0・root は Application 16 個に増えたこと確認 /
  py_compile ok。ruff F821 はサンドボックスに ruff 無しで未実行 (CI 任せ)

### 分かったこと / 発見 (スコープ外なのでここに書くだけ)

- **ブランチが main 遅れだと validate.py が落ちる**: archive.jsonl の追記検査は
  「branch ⊇ main」を見る (#540 追分で先頭一致が崩れた)。rebase ではなく
  merge origin/main で解消した (wrapper の push が非 force のため。
  P-0107 worker #2 の rebase + force-with-lease は wrapper と噛み合わない恐れがある)
- `kubectl kustomize --enable-helm` は helm binary を要求する。argocd/dex/
  external-secrets/immich/tailscale-operator の 5 dir はサンドボックスでは
  render 不能 (helm 無いだけで main 由来の既存状態。stash 実測で確認)

### 次のセッションへ

1. **verify 1・2 はまだ red (期待どおり)**。綱は引いたので、あとは Job を一度走らせる:
   cluster アクセスのあるセッション / 人間が `just preview apps project/p-0192`
   (新規アプリのためルートを向ける。justfile コメントの手順) → wish-seeds Job が走り、
   送信 1 通 + 証跡がこのブランチへ書き戻される → `just preview-reset apps`。
   以後 verify 1・2 は自動で green。merge 後に ArgoCD が Job を作り直しても、
   証跡が main にあるので skip して二重送信しない
2. ブランチに `ops/projects/logs/P-0192/ask-pending.json` が現れるのは正常
   (送信開始マーカー。完了後も残すのが設計)。消さないこと
3. 証跡が乗ったら返信待ち (セッション 1 の記載どおり。inbox 読み取り専用、
   render_seeds_section → seeds.md、沈黙は締め日付を正直に書いて記録)
