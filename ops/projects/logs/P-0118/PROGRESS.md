# P-0118 PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## セッション記録

### worker #1 (2026-08-23) — fixture テスト・dry-run ドリル・疎通依頼の投稿。実輸送の実測は人間の送信待ち

**やったこと**:

- **受入 (1) を green 化**: `ops/tests/test_telegram_veto.py` 新設 (9 tests)。
  telegram 形 note ({id, source:"telegram", received, body}、trim 無し・kind 無し = bridge の
  実保存形式) を **collect_feedback 経由で**通して 4 系統を固定:
  「止めて」→ stop_all / 「再開」→ resume_all / kind:task-request → task_requests /
  雑談 → review_needed。`TestRulesKeywordsDriveTelegramVerdicts` が rules.json の
  stop_keywords・resume_keywords **全量**を telegram DM の典型形 (1 行短文) で通し
  分類が決まること (=「telegram source でも参照されること」の直接証明) を見る。
  キーワードはハードコードせず rules.json を読む (test_triage.py と同じ読み込みパターン)
- **受入 (2) を green 化**: `ops/drills/telegram_veto_drill.py` 新設。
  `--check` は同梱 7 ケース (キーワードは rules.json から動的構築) との突合、
  `--input <file|->` は 1 行 1 メッセージを JSONL ({line, body, kind}) で出力。
  副作用ゼロ (triage.classify の純関数呼び出しのみ)。グローバル停止は実地に踏ませない
- **受入 (3) の器を作成**: `evidence.json` に dry-run (--check) 判定出力と
  疎通依頼の URL を記録。**feedback_note_url はまだ null** (下記)
- **実輸送の現状を実測 → question 型依頼を投稿**: ops-feedback ブランチの inbox には
  dashboard 由来 2 件のみで **telegram 由来 note は 0 件** (P-0107 merge 後も実メッセージ無し)。
  つまり spec の「1 通も届いていないなら依頼する」分岐が正。
  サンドボックスに DISCORD_WEBHOOK_URL / doppler が無く notify.py の Discord 直送は
  不可能だったため、CHARTER §5.5 の正規経路 `ops/post_issue_comment.py` で issue #56 へ
  投稿した: https://github.com/hikuohiku/homelab/issues/56#issuecomment-5383567514
  (「allowlist 登録済みアカウントから短文 1 通を送ってほしい」+ 48h 届かなければ
  bridge ログと TELEGRAM_BOT_TOKEN/TELEGRAM_ALLOWED_USER_ID 登録状態を調査する旨)
- **docs 追記**: `docs/telegram-stop-resume.md` — 止め方・再開・veto の文言表、
  叙述長文が発火しない仕様、届き確認は ops-feedback ブランチの inbox JSON、
  届かないときに疑う点 (allowlist 外 / bridge ログ / pod 待機)

**verify 実測 (すべて green)**: unittest 9 tests OK / drill --check rc=0 (7/7 ok) /
evidence test -s rc=0。加えて既存への影響なし: 関連 54 tests OK、discover 全 377 tests OK、
validate.py 0 error (warning 11 件は backlog refs の既存分)、check_doc_commands.py ok。

**分かったこと / 次への引き継ぎ**:

- **次のセッションの最初の仕事は feedback_note_url を埋めること**:
  `git fetch origin ops-feedback && git ls-tree -r --name-only origin/ops-feedback ops/feedback/inbox/`
  で source:"telegram" の note を探す → 見つかったら blob URL を evidence.json の
  `transport.feedback_note_url` に記入し、triage 分類結果も添える。
  人間が「止めて」系を送っていた場合でも **全停止を実地に踏ませない**
  (証跡は inbox 上の JSON だけで足りる、spec 明記)。48h 未着なら bridge 側の調査へ
- PROJECT.md の前提「collect_feedback が inbox JSON note には note の source フィールドを渡す」
  は**正確でない**: 実装 (facts.py handle(body, path, kind)) は **ファイルパス**を source として
  渡す (note["source"]="telegram" は分類に使われない)。分類は本文のみで決まる —
  だからこそ本テストが「パス経由で届いた telegram note も分類される」ことを固定する価値がある
- テキスト単体では task-request 分流は決まらない (note のトップレベル kind を読むのは
  collect_feedback)。ドリルの出力で「実装依頼らしき文」が review_needed になるのは仕様どおり。
  note レベルの分流契約は test_telegram_veto.py が持つ
- `/tmp/opencode` は書けない (root 所有。P-0107 worker #3 の記録どおり再実測)。
  一時ファイルは `mktemp -d`
- issue #56 への投稿本文は triage 誤爆対策 (P-0027 の知見) を守った: 行頭に停止キーワードを
  置かない / `veto P-\d{4}` 形を含めない / 全体 50 文字超。マーカー付きなので heart の
  取り込み対象外 (CHARTER §6 手順2) だが防御は重ねた

### worker #2 (2026-08-23) — レビュー指摘の解消: 恒真テストの書き直し + ops-feedback 再確認 (telegram note 依然 0 件)

**やったこと**:

- **指摘 (2) を解消**: `test_classify_is_source_agnostic_by_design` (同一関数への同一引数の
  自己比較で恒真) を削り、`TestClassificationIgnoresSource.test_same_body_same_verdict_across_transports`
  として実検証に書き換えた。同一本文を (a) telegram note、(b) source:"ops-dashboard" の note、
  (c) issue コメントの **3 経路**で collect_feedback に通し、分類種別が期待値と 3 者一致することを
  検査する。期待値の同時断言があるので分類本体が壊れても倒れる (片側比較の罠を回避)。
  対象は stop / resume / veto (`veto P-0103`) / 雑談の 4 系統。
  補助として `telegram_note()` に source 差し替え用引数、`CommentGh` スタブと
  `verdict_kind()` (collect_feedback 戻り値 → 分類種別 1 つへ潰す) を追加
- **指摘 (1) のうち今できることを実施**: `git fetch origin ops-feedback` 再確認の結果、inbox は
  **依然 dashboard 由来 2 件のみ・telegram 由来 0 件** (worker #1 実測と不変)。
  evidence.json の transport.checked_at / next_action を更新。
  state は pending-human のまま。48h 期限は **2026-08-25T01:20Z** (投稿 2026-08-23T01:20Z 起算) で
  未到来なので bridge ログ調査には進んでいない — レビュー指摘 (b) の条件はまだ成立しない

**verify 実測 (すべて green)**: unittest 9 tests OK / drill --check rc=0 (7/7 ok) /
evidence test -s rc=0。discover 全 377 tests OK。

**分かったこと / 次への引き継ぎ**:

- **次のセッションの最初の仕事は worker #1 と同じく feedback_note_url を埋めること**。
  現在時刻が 2026-08-25T01:20Z を過ぎていたら依頼文記載の調査へ進む:
  `kubectl -n autopilot logs deploy/openclaw -c feedback-bridge` で受信エラー/401 の有無を見る、
  TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID の Doppler 登録状態を確認する。
  届いていれば blob URL・本文・triage 分類結果を evidence.json の transport セクションに
  記入して state を更新。「止めて」系の実送信でも全停止を実地に踏ませない (spec 明記)
- **validate.py が現在 1 error を出すが本プロジェクト起因ではない**: main 側の curriculum commit
  `d7ed4aa0` (2026-08-23T01:30Z 頃) が ops/projects/archive.jsonl に P-0125〜P-0132 を追記した一方、
  本ブランチはそれより前 (ea48d514) から分岐しているため「origin/main の内容と先頭一致しない」が
  出る。worker #1 時点は 0 error だったのがこの差分によるもの。ops/ の帳簿は触らないので放置 —
  merge 時に本ブランチ側は追記しかしていないので解消されるはず
- 新しい輸送経路を足す変更が入ったとき、このテスト群は「分類への source 引数追加」を
  TestClassificationIgnoresSource が検知する形になった。triage.classify 自体のシグネチャ変更は
  mypy 等では守られていない (型チェックなし) ので、実挙動でのみ見える

### worker #3 (2026-08-23) — 待機中の実測を前進: bridge 稼働の間接証明と「worker から kubectl 不可能」の確定

**やったこと**:

- **ops-feedback 再確認**: inbox は依然 dashboard 由来 2 件のみ・telegram note 0 件
  (02:05Z 実測)。48h 期限 **2026-08-25T01:20Z** 未到来のため指摘 (b) の条件は未成立
- **issue スレッドを実測** (GitHub API、AUTOPILOT_GITHUB_TOKEN 使用): 依頼投稿
  (5383567514, 01:23:30Z) 以降の人間返信なし。依頼が人間にまだ届いていない可能性も含め、
  以降のセッションはスレッド確認を習慣にするとよい (`gh` 無しの環境なので curl + API)
- **bridge 側が生きていることを間接観測で実測** (新規):
  `git fetch origin ops-health-report` → `ops/health/latest.json`
  (generated_at 2026-08-23T02:00:23Z) で openclaw アプリ Synced/Healthy、pod
  `openclaw-6f75b7d78-2drqz` の gateway / feedback-bridge **両コンテナが pod_metrics 計上**
  (feedback-bridge cpu 292419n = ポーリングループ稼働中)、pod_issues に openclaw 関連なし。
  未着の第一仮説「まだ送信されていない」が妥当になり、48h 待つ判断を実測で後押し。
  evidence.json の `transport.bridge_liveness` に記録済み
- **レビュー指摘 (b) の前提に穴があることを確定させ、evidence.json の next_action を書き換えた**:
  指摘の調査手順 `kubectl -n autopilot logs deploy/openclaw -c feedback-bridge` は
  **worker セッションから実施不可能**。apps/autopilot/rbac.yaml:83-84 の設計どおり runner には
  SA token が mount されず (`/var/run/secrets/kubernetes.io/serviceaccount` 無し)、
  kubeconfig も無い (kubectl 実測: localhost:8080 接続拒否)。KUBERNETES_SERVICE_HOST env のみ
  存在するので一見クラスタに届きそうに見えるのが罠。worker に可能な観測は
  ops-health-report ブランチと issue スレッドのみ。48h 超過後の正しい次手を next_action に明記:
  bridge 稼働中なら issue #56 経由で人間へ「ログ実行 or 送信済みかの確認」を依頼、
  bridge 落ちなら apps/openclaw 側修正を検討

**verify 実測 (すべて green)**: unittest 9 tests OK / drill --check rc=0 (7/7 ok) /
evidence test -s rc=0。

**分かったこと / 次への引き継ぎ**:

- **次のセッションの最初の仕事は変わらず feedback_note_url を埋めること**
  (`git fetch origin ops-feedback && git ls-tree -r --name-only origin/ops-feedback
  ops/feedback/inbox/`)。届いていれば blob URL・本文・triage 分類結果を evidence.json に記入。
  「止めて」系の実送信でも全停止を実地に踏ませない (spec 明記)
- 期限超過後は **kubectl を再試行しない** (必ず失敗する)。evidence.json の
  `transport.next_action` と `transport.bridge_liveness` に調整済みの計画があるのでそれに従う
- ops-health-report ブランチの latest.json は worker にとって唯一のクラスタ観測点。
  generated_at を必ず見る (古い場合は reporter 自体の死を疑う)
- readiness は startupz (channel 障害でも Ready を外さない設計、apps/openclaw/deployment.yaml
  コメント) のため、「ArgoCD Healthy」は Telegram チャネル自体の生死まで証明しない。
  チャネル断の最終確認はどうしても gateway ログ (= 人間 or heart の助け) が必要

## 発見 (curriculum へ)

- レビュー指摘の調査手順が、runner サンドボックスの権限モデル (rbac.yaml の
  「worker はトークン automount 無しで API に触れない」) と不一致のまま出てくることがある
  (本件: kubectl logs による bridge 調査を worker に指示したが実行不可能だった)。
  対応方向の候補: レビュー prompt 側で「worker 実行環境から可能な手順か」の自問を足す /
  heart 側に診断用の log 読み出し窓口を用意する / runner Job への pods/log read 付与を
  検討する (現状は意図的な隔離なので変更には substrate の規則が乗る)。本プロジェクトでは
  evidence.json の next_action に回避策を書くだけで対処した

