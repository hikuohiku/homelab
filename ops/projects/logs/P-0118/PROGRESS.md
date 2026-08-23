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

## 発見 (curriculum へ)

- (なし — スコープ外の問題には触れなかった)
